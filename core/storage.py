"""All file storage. Every write is atomic (tmp file + rename) so a power cut
on the Tinkerboard can never leave a half-written JSON file."""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR

log = logging.getLogger("nubtk.storage")

STUDENTS_DIR = DATA_DIR / "students"
ROSTER_DIR = DATA_DIR / "roster"
ATTENDANCE_DIR = DATA_DIR / "attendance"

STUDENT_CSV_HEADER = [
    "SL", "Student ID", "Name", "Section", "Course Code", "Course Title",
    "Discord Username", "Server Nickname", "Status", "Verified At", "Discord ID",
]


# ---------------------------------------------------------------- primitives
def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding=encoding, newline="") as f:
        f.write(text)
        f.flush()
        try:
            os.fsync(f.fileno())
        except OSError:
            pass
    os.replace(tmp, path)


def load_json(path: Path, default):
    path = Path(path)
    if not path.exists():
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error("Could not read %s (%s) - keeping a .broken copy", path, e)
        try:
            path.replace(path.with_name(f"{path.name}.broken-{int(datetime.now().timestamp())}"))
        except OSError:
            pass
        return default


def save_json(path: Path, data) -> None:
    atomic_write_text(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n")


def write_csv(path: Path, header: list, rows: list) -> None:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    # utf-8-sig so Excel opens it correctly
    atomic_write_text(path, buf.getvalue(), encoding="utf-8-sig")


def append_audit(settings, event: str, **fields) -> None:
    rec = {"time": settings.now_str(), "event": event, **fields}
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(DATA_DIR / "audit_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.error("audit write failed: %s", e)


# ---------------------------------------------------------------- server ids
class ServerIds:
    """Remembers the IDs of channels/roles the bot created (no hard-coded IDs)."""

    def __init__(self):
        self.path = DATA_DIR / "server_ids.json"
        self.data: dict = load_json(self.path, {})

    def get(self, key: str):
        v = self.data.get(key)
        return int(v) if v else None

    def set(self, key: str, value: int) -> None:
        if self.data.get(key) != str(value):
            self.data[key] = str(value)
            save_json(self.path, self.data)

    def save(self) -> None:
        save_json(self.path, self.data)

    def keys_with_prefix(self, prefix: str) -> list:
        return [k for k in self.data if k.startswith(prefix)]


# ---------------------------------------------------------------- students
class StudentStore:
    """Master file: data/verified_records.json  (key = discord id)
    Auto-generated: data/students/<COURSE>_<SECTION>.json and .csv"""

    def __init__(self, settings):
        self.settings = settings
        self.path = DATA_DIR / "verified_records.json"
        self.records: dict = load_json(self.path, {})

    # reads
    def get(self, discord_id):
        return self.records.get(str(discord_id))

    def all_active(self) -> list:
        return [r for r in self.records.values() if r.get("status", "active") == "active"]

    def find_by_student_id(self, sid: str) -> list:
        return [r for r in self.records.values() if r.get("student_id") == sid]

    def section_students(self, sec: str, include_left: bool = False) -> list:
        rows = [
            r for r in self.records.values()
            if (r.get("section") == sec or sec in (r.get("extra_sections") or []))
            and (include_left or r.get("status", "active") == "active")
        ]
        return sorted(rows, key=lambda r: r.get("student_id", ""))

    def course_students(self, course: str, include_left: bool = False) -> list:
        out, seen = [], set()
        for sec in self.settings.course_sections(course):
            for r in self.section_students(sec, include_left):
                if r.get("discord_id") not in seen:  # a student can be in 2 sections (extra_sections)
                    seen.add(r.get("discord_id"))
                    out.append(r)
        return out

    def search(self, query: str) -> list:
        q = query.strip().lower()
        hits = []
        for r in self.records.values():
            hay = " ".join(str(r.get(k, "")) for k in ("student_id", "name", "discord_name", "nickname", "discord_id")).lower()
            if q in hay:
                hits.append(r)
        return sorted(hits, key=lambda r: (r.get("section", ""), r.get("student_id", "")))

    # writes
    def upsert(self, record: dict) -> None:
        self.records[str(record["discord_id"])] = record
        self.save()

    def update(self, discord_id, **fields) -> dict | None:
        rec = self.get(discord_id)
        if rec is None:
            return None
        rec.update(fields)
        self.save()
        return rec

    def remove(self, discord_id) -> dict | None:
        rec = self.records.pop(str(discord_id), None)
        self.save()
        return rec

    def save(self) -> None:
        save_json(self.path, self.records)
        try:
            self.export_files()
        except Exception:  # never let an export problem break verification
            log.exception("Export of course/section files failed")

    def export_files(self) -> list:
        """Writes one JSON + one CSV per (course, section). Returns written paths."""
        s = self.settings
        written = []
        for sec in s.sections:
            students = self.section_students(sec, include_left=True)
            for course in s.section_courses(sec):
                info = s.courses[course]
                base = STUDENTS_DIR / f"{course}_{sec}"
                active = [r for r in students if r.get("status", "active") == "active"]
                payload = {
                    "course_code": info["code"],
                    "course_title": info["title"],
                    "section": sec,
                    "semester": s.semester,
                    "updated_at": s.now_str(),
                    "total_active": len(active),
                    "students": [
                        {
                            "student_id": r.get("student_id"),
                            "name": r.get("name"),
                            "status": r.get("status", "active"),
                            "discord_id": r.get("discord_id"),
                            "discord_name": r.get("discord_name"),
                            "nickname": r.get("nickname"),
                            "verified_at": r.get("verified_at"),
                        }
                        for r in students
                    ],
                }
                save_json(base.with_suffix(".json"), payload)
                rows = [
                    [i, r.get("student_id"), r.get("name"), sec, info["code"], info["title"],
                     r.get("discord_name"), r.get("nickname"), r.get("status", "active"),
                     r.get("verified_at"), r.get("discord_id")]
                    for i, r in enumerate(students, 1)
                ]
                write_csv(base.with_suffix(".csv"), STUDENT_CSV_HEADER, rows)
                written += [base.with_suffix(".json"), base.with_suffix(".csv")]
        return written


# ---------------------------------------------------------------- pending approvals
class PendingStore:
    def __init__(self):
        self.path = DATA_DIR / "pending.json"
        self.items: dict = load_json(self.path, {})

    def add(self, data: dict) -> str:
        pid = secrets.token_hex(4)
        self.items[pid] = data
        self.save()
        return pid

    def get(self, pid: str):
        return self.items.get(pid)

    def pop(self, pid: str):
        item = self.items.pop(pid, None)
        self.save()
        return item

    def find_by_member(self, discord_id):
        for pid, item in self.items.items():
            if item.get("discord_id") == str(discord_id):
                return pid, item
        return None

    def save(self) -> None:
        save_json(self.path, self.items)


# ---------------------------------------------------------------- roster (official list, optional)
def roster_path(sec: str) -> Path:
    return ROSTER_DIR / f"{sec}.csv"


def load_roster(sec: str) -> dict:
    path = roster_path(sec)
    if not path.exists():
        return {}
    out = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.reader(f):
            if row and row[0].strip().isdigit():
                out[row[0].strip()] = row[1].strip() if len(row) > 1 else ""
    return out


def parse_roster_text(text: str, id_len: int) -> dict:
    """Accepts CSV/TXT with one student per line: any order of ID and name."""
    id_re = re.compile(rf"(?<!\d)(\d{{{id_len}}})(?!\d)")
    out = {}
    for line in text.splitlines():
        m = id_re.search(line)
        if not m:
            continue
        rest = (line[: m.start()] + " " + line[m.end():])
        rest = re.sub(r"[,;\t|]", " ", rest)
        rest = re.sub(r"(?<![A-Za-z])\d+(?![A-Za-z])", " ", rest)
        name = re.sub(r"\s+", " ", rest).strip(" .-")
        out[m.group(1)] = name
    return out


def save_roster(sec: str, entries: dict) -> Path:
    path = roster_path(sec)
    write_csv(path, ["student_id", "name"], sorted(entries.items()))
    return path
