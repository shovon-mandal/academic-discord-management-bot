"""Parsing of student messages like '2C 11250121964 John Doe' (any order)."""
from __future__ import annotations

import re

LABEL_WORDS = {
    "name", "full", "official", "my", "is", "id", "student", "studentid", "student_id",
    "section", "sec", "roll", "number", "hi", "hello", "sir", "assalamualaikum",
}


def clean_name(raw: str, strip_labels: bool = False) -> str:
    t = re.sub(r"<[@#][!&]?\d+>", " ", raw or "")
    t = re.sub(r"[^A-Za-z.\-' ]", " ", t)
    words = [w.strip(".-'") for w in t.split()]
    words = [w for w in words if w]
    if strip_labels:
        words = [w for w in words if w.lower() not in LABEL_WORDS]
    name = " ".join(words)
    if name and (name.isupper() or name.islower()):
        name = name.title()
    return name[:60].strip()


def parse_verification_text(content: str, sections, id_len: int = 11):
    """Returns (section | None, student_id | None, name)."""
    text = re.sub(r"<[@#][!&]?\d+>", " ", content or "")

    alts = "|".join(re.escape(s) for s in sorted(sections, key=len, reverse=True))
    sec_re = re.compile(rf"(?<![A-Za-z0-9])(?:sec(?:tion)?[\s_\-:.]*)?({alts})(?![A-Za-z])", re.I)
    lookup = {s.upper(): s for s in sections}

    section = None
    m = sec_re.search(text)
    if m:
        section = lookup.get(m.group(1).upper())
        text = text[: m.start()] + " " + text[m.end():]

    student_id = None
    m = re.search(rf"(?<!\d)(\d{{{id_len}}})(?!\d)", text)
    if m:
        student_id = m.group(1)
        text = text[: m.start()] + " " + text[m.end():]

    text = re.sub(r"\d+", " ", text)
    return section, student_id, clean_name(text, strip_labels=True)


def build_nickname(fmt: str, student_id: str, section: str, name: str, limit: int = 32) -> str:
    fields = {"last4": student_id[-4:], "id": student_id, "section": section}
    without_name = fmt.format(name="", **fields)
    room = max(1, limit - len(without_name))
    nick = fmt.format(name=name[:room].strip(), **fields)
    return nick[:limit]
