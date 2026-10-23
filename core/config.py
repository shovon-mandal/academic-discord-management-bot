"""Loads config.json (safe to commit) and .env (secrets, never commit)."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
LOG_DIR = BASE_DIR / "logs"
BACKUP_DIR = BASE_DIR / "backups"

load_dotenv(BASE_DIR / ".env")


def _parse_ids(raw: str) -> set:
    return {int(p) for p in re.split(r"[,\s]+", raw or "") if p.isdigit()}


class Settings:
    def __init__(self, path: Path | None = None):
        self.path = Path(path) if path else BASE_DIR / "config.json"
        self.reload()

    # ------------------------------------------------------------------ load
    def reload(self) -> None:
        with open(self.path, encoding="utf-8") as f:
            raw = json.load(f)
        self.raw = raw

        # secrets / machine specific (.env)
        self.token = os.getenv("DISCORD_TOKEN", "").strip().strip('"').strip("'")
        gid = os.getenv("GUILD_ID", "").strip()
        self.guild_id = int(gid) if gid.isdigit() else 0
        self.admin_ids = _parse_ids(os.getenv("ADMIN_IDS", ""))

        # general
        self.server_name = raw.get("server_name", "Class Server")
        self.semester = raw.get("semester", "")
        self.teacher = raw.get("teacher", "Teacher")
        self.prefix = raw.get("command_prefix", "!")
        self.tz = timezone(timedelta(hours=float(raw.get("utc_offset_hours", 6))))

        # verification
        self.id_length = int(raw.get("student_id_length", 11))
        self.nickname_format = raw.get("nickname_format", "{last4}_{section}_{name}")
        self.verified_role_name = raw.get("verified_role", "Verified")
        self.section_role_format = raw.get("section_role_format", "Sec-{section}")
        self.duplicate_policy = raw.get("duplicate_id_policy", "admin_approval")
        self.roster_check = bool(raw.get("roster_check", False))
        self.lock_nickname = bool(raw.get("lock_nickname_change", True))
        self.welcome_ping_delete_after = int(raw.get("welcome_ping_delete_after_minutes", 30)) * 60
        self.id_slowmode = int(raw.get("id_channel_slowmode_seconds", 5))
        self.course_group_categories = bool(raw.get("course_group_categories", True))

        backup = raw.get("daily_backup", {}) or {}
        self.backup_enabled = bool(backup.get("enabled", True))
        self.backup_time = str(backup.get("time", "23:30"))
        self.backup_keep = int(backup.get("keep_last", 14))

        self.courses: dict = raw["courses"]
        self.sections: dict = raw["sections"]
        self.channels: dict = raw["channels"]
        self._validate()

    def _validate(self) -> None:
        if not self.sections:
            raise ValueError("config.json: 'sections' is empty")
        for sec, info in self.sections.items():
            if not re.fullmatch(r"[A-Za-z0-9]{1,6}", sec):
                raise ValueError(f"config.json: bad section name '{sec}' (letters/digits only)")
            for c in info.get("courses", []):
                if c not in self.courses:
                    raise ValueError(f"config.json: section {sec} uses unknown course '{c}'")
        for key, info in self.courses.items():
            lab = info.get("lab")
            if lab and lab not in self.courses:
                raise ValueError(f"config.json: course {key} points to unknown lab '{lab}'")
        if self.duplicate_policy not in ("admin_approval", "block", "allow"):
            raise ValueError("config.json: duplicate_id_policy must be admin_approval / block / allow")
        if not re.fullmatch(r"\d{1,2}:\d{2}", self.backup_time):
            raise ValueError("config.json: daily_backup.time must look like 23:30")

    # ------------------------------------------------------------------ time
    def now(self) -> datetime:
        return datetime.now(self.tz)

    def now_str(self) -> str:
        return self.now().strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------ sections / courses
    def section_role_name(self, sec: str) -> str:
        return self.section_role_format.format(section=sec)

    def section_courses(self, sec: str) -> list:
        return list(self.sections[sec].get("courses", []))

    def section_has_lab(self, sec: str) -> bool:
        return any(self.courses[c].get("type") == "lab" for c in self.section_courses(sec))

    def is_evening(self, sec: str) -> bool:
        return bool(self.sections[sec].get("evening", False))

    def section_color(self, sec: str) -> int:
        try:
            return int(str(self.sections[sec].get("color", "#99aab5")).lstrip("#"), 16)
        except ValueError:
            return 0x99AAB5

    def course_sections(self, course: str) -> list:
        return [s for s in self.sections if course in self.section_courses(s)]

    def theory_course_for(self, course: str) -> str:
        if self.courses[course].get("type") == "lab":
            for key, info in self.courses.items():
                if info.get("lab") == course:
                    return key
        return course

    def default_course(self, sec: str) -> str:
        courses = self.section_courses(sec)
        for c in courses:
            if self.courses[c].get("type") != "lab":
                return c
        return courses[0]

    def course_groups(self) -> dict:
        """theory course -> {'courses': [theory, lab], 'sections': [...]}"""
        groups: dict = {}
        for sec in self.sections:
            for c in self.section_courses(sec):
                theory = self.theory_course_for(c)
                g = groups.setdefault(theory, {"courses": [], "sections": []})
                if c not in g["courses"]:
                    g["courses"].append(c)
                if sec not in g["sections"]:
                    g["sections"].append(sec)
        for g in groups.values():
            g["courses"].sort(key=lambda c: self.courses[c].get("type") == "lab")
        return groups

    def codes_label(self, courses: list) -> str:
        """['CSE1205','CSE1206'] -> 'CSE 1205/1206'"""
        codes = [self.courses[c]["code"] for c in courses]
        if not codes:
            return ""
        first_prefix = codes[0].split()[0]
        out = [codes[0]]
        for code in codes[1:]:
            parts = code.split()
            out.append(parts[-1] if parts and parts[0] == first_prefix else code)
        return "/".join(out)

    def short_for_section(self, sec: str) -> str:
        return self.courses[self.default_course(sec)].get("short", "")

    def section_label(self, sec: str) -> str:
        label = f"{sec} · {self.short_for_section(sec)}"
        if self.is_evening(sec):
            label += " (Evening)"
        return label

    def section_category_name(self, sec: str) -> str:
        custom = self.sections[sec].get("category_name")
        if custom:
            return custom
        name = f"📘 {sec} · {self.codes_label(self.section_courses(sec))} · {self.short_for_section(sec)}"
        if self.is_evening(sec):
            name += " · Evening"
        return name[:100]

    def course_group_category_name(self, theory: str, group: dict) -> str:
        short = self.courses[theory].get("short", "")
        return f"🌐 {self.codes_label(group['courses'])} · {short} · {', '.join(group['sections'])}"[:100]

    def section_courses_text(self, sec: str) -> str:
        return " + ".join(f"{self.courses[c]['code']} {self.courses[c]['short']}" for c in self.section_courses(sec))

    # ------------------------------------------------------------------ normalisers
    def normalize_section(self, text: str | None) -> str | None:
        if not text:
            return None
        t = re.sub(r"(?i)^\s*(sec(tion)?)[\s_\-:.]*", "", text.strip()).strip().upper()
        lookup = {s.upper(): s for s in self.sections}
        return lookup.get(t)

    def normalize_course(self, text: str | None) -> str | None:
        if not text:
            return None
        t = text.strip().upper().replace(" ", "")
        if t in self.courses:
            return t
        digits = re.sub(r"\D", "", t)
        if not digits:
            return None
        for key, info in self.courses.items():
            if re.sub(r"\D", "", info["code"]) == digits:
                return key
        return None
