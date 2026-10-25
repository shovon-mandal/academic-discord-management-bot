"""Draw a section routine as a clean table image (PNG) — like the department sheet, but easier to read.

Needs Pillow. If Pillow is missing, `available()` is False and the bot falls back to a text table.
Fonts: Poppins (SIL Open Font License) bundled in assets/fonts.
"""
from __future__ import annotations

import io
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # noqa: BLE001
    Image = None

FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

DAY_SHORT = {"Saturday": "Saturday", "Sunday": "Sunday", "Monday": "Monday", "Tuesday": "Tuesday",
             "Wednesday": "Wednesday", "Thursday": "Thursday", "Friday": "Friday"}

# colours
BG = (247, 248, 252)
CARD = (255, 255, 255)
INK = (28, 32, 48)
MUTED = (104, 112, 135)
LINE = (226, 229, 238)
HEAD_TXT = (255, 255, 255)
DAY_TINTS = [(240, 244, 255), (255, 255, 255)]
MINE_BG = (255, 243, 205)
MINE_BAR = (240, 173, 0)
LAB_TXT = (13, 128, 112)


def available() -> bool:
    return Image is not None and (FONT_DIR / "Poppins-Regular.ttf").exists()


def _f(weight: str, size: int):
    return ImageFont.truetype(str(FONT_DIR / f"Poppins-{weight}.ttf"), size)


def _fit(draw, text, font, width):
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip(" :-") + "…"


def _star(draw, cx, cy, r, fill):
    import math
    pts = []
    for i in range(10):
        a = math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(a), cy - rr * math.sin(a)))
    draw.polygon(pts, fill=fill)


def _table(draw, x, y, w, rows, cols, accent, teacher, fonts, title):
    """Draw one table block; returns new y."""
    f_head, f_cell, f_bold, f_title = fonts
    # block title
    draw.text((x, y), title, font=f_title, fill=INK)
    y += 46
    # header
    hh = 46
    draw.rounded_rectangle((x, y, x + w, y + hh), radius=10, fill=accent)
    draw.rectangle((x, y + hh - 10, x + w, y + hh), fill=accent)
    cx = x
    for name, cw, _key in cols:
        draw.text((cx + 14, y + hh / 2), name, font=f_head, fill=HEAD_TXT, anchor="lm")
        cx += cw
    y += hh
    rh = 44
    prev_day, tint_i, n = None, 1, 0
    for it in rows:
        if it["day"] != prev_day:
            tint_i ^= 1
            if prev_day is not None:
                draw.line((x, y, x + w, y), fill=(200, 205, 220), width=2)
        mine = teacher and it["teacher"] == teacher
        bg = MINE_BG if mine else DAY_TINTS[tint_i]
        draw.rectangle((x, y, x + w, y + rh), fill=bg)
        if mine:
            draw.rectangle((x, y, x + 6, y + rh), fill=MINE_BAR)
        n += 1
        cx = x
        for _name, cw, key in cols:
            if key == "no":
                val = str(n)
            elif key == "day":
                val = DAY_SHORT.get(it["day"], it["day"]) if it["day"] != prev_day else ""
            else:
                val = str(it.get(key, "") or "")
            font = f_bold if key in ("day", "course") or (key == "teacher" and mine) else f_cell
            color = INK
            if key == "room" and "LAB" in val.upper():
                color = LAB_TXT
            if key == "title" and mine:
                font = f_bold
            pad = 14
            if key == "no" and mine:
                _star(draw, cx + 22, y + rh / 2, 10, MINE_BAR)
                val = ""
            draw.text((cx + pad, y + rh / 2), _fit(draw, val, font, cw - pad * 2), font=font,
                      fill=color if key != "no" else MUTED, anchor="lm")
            cx += cw
        draw.line((x, y + rh, x + w, y + rh), fill=LINE, width=1)
        prev_day = it["day"]
        y += rh
    if not rows:
        draw.rectangle((x, y, x + w, y + rh), fill=CARD)
        draw.text((x + 14, y + rh / 2), "No classes", font=f_cell, fill=MUTED, anchor="lm")
        y += rh
    return y


def render(sec: str, items: list, mine: list, *, teacher: str, titles: dict, accent=(52, 84, 209),
           heading: str = "", sub: str = "", footer: str = "") -> bytes:
    """items/mine: [{day, time, room, course, teacher, title}] (already sorted)."""
    W, M = 1500, 36
    w = W - 2 * M
    cols_all = [("#", 60, "no"), ("Day", 160, "day"), ("Time", 270, "time"), ("Room", 170, "room"),
                ("Course", 150, "course"), ("Faculty", 120, "teacher")]
    cols_all.append(("Course Title", w - sum(c[1] for c in cols_all), "title"))
    cols_mine = [("#", 60, "no"), ("Day", 160, "day"), ("Time", 270, "time"), ("Room", 170, "room"),
                 ("Course", 150, "course")]
    cols_mine.append(("Course Title", w - sum(c[1] for c in cols_mine), "title"))
    fonts = (_f("Medium", 21), _f("Regular", 21), _f("Medium", 21), _f("Bold", 27))
    for it in [*items, *mine]:
        it.setdefault("title", titles.get(it["course"], ""))

    h = 150 + 46 + 46 + 44 * max(1, len(items)) + 40
    if mine:
        h += 46 + 46 + 44 * len(mine) + 30
    h += 70
    img = Image.new("RGB", (W, h), BG)
    d = ImageDraw.Draw(img)
    # header band
    d.rounded_rectangle((M, 24, W - M, 130), radius=18, fill=accent)
    d.text((M + 28, 58), heading or f"Section {sec} — Class Routine", font=_f("Bold", 36), fill=HEAD_TXT, anchor="lm")
    d.text((M + 28, 102), sub, font=_f("Regular", 22), fill=(230, 234, 255), anchor="lm")
    y = 150
    y = _table(d, M, y, w, items, cols_all, accent, teacher, fonts, "Full routine")
    # legend
    y += 12
    _star(d, M + 12, y + 14, 10, MINE_BAR)
    d.text((M + 30, y + 14), f"= class with {teacher}", font=_f("Regular", 19), fill=MUTED, anchor="lm")
    d.text((M + 260, y + 14), "Green room = lab", font=_f("Regular", 19), fill=LAB_TXT, anchor="lm")
    y += 40
    if mine:
        y += 8
        y = _table(d, M, y, w, mine, cols_mine, (196, 136, 0), "", fonts, f"Classes with {teacher}")
    d.text((M, h - 36), footer, font=_f("Regular", 18), fill=MUTED, anchor="lm")
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
