"""🗓️ Class routine from the official Google Sheet.

The department routine sheet (config.json → routine.sheet_url) is downloaded as Excel,
the grid sheets ("CSE", "ECSE" …) are parsed, and for every section the bot keeps
ONE pinned, always-latest routine message in that section's 🗓️┃routine channel.
If something changed since the last check, it also posts what changed (➕ / ➖).

Check: panel button "🔄 Check routine", or `!routine_check`. Auto-check every N hours.
"""
from __future__ import annotations

import io
import logging
import re
from itertools import zip_longest

import aiohttp
import discord
from discord.ext import commands, tasks

from core.config import DATA_DIR
from core.storage import append_audit, load_json, save_json

log = logging.getLogger("nubtk.routine")
STATE_FILE = DATA_DIR / "routine" / "state.json"

DAYS = ["Saturday", "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
DAY_ALIASES = {d[:3].lower(): d for d in DAYS}
TIME_RE = re.compile(r"(\d{1,2})[.:](\d{2})\s*([AP]M)", re.I)
DATE_RE = re.compile(r"\b(\d{1,2}-[A-Za-z]{3}-\d{4})\b")


# ====================================================================== parsing (pure functions)
def _norm_time(label: str) -> str:
    """'03:30 PM – 04:30 AM' → '03:30 PM – 04:30 PM' (fixes the common AM/PM typo)."""
    found = TIME_RE.findall(label or "")
    if len(found) < 2:
        return (label or "").strip()
    (h1, m1, p1), (h2, m2, p2) = found[0], found[1]
    p1, p2 = p1.upper(), p2.upper()
    if p1 == "PM" and p2 == "AM" and int(h2) != 12:
        p2 = "PM"
    return f"{int(h1):02d}:{m1} {p1} – {int(h2):02d}:{m2} {p2}"


def _time_key(label: str) -> int:
    m = TIME_RE.search(label or "")
    if not m:
        return 9999
    h, mi, p = int(m.group(1)), int(m.group(2)), m.group(3).upper()
    if p == "PM" and h != 12:
        h += 12
    if p == "AM" and h == 12:
        h = 0
    return h * 60 + mi


def _cell(row, i) -> str:
    if i < len(row) and row[i] is not None:
        return str(row[i]).strip()
    return ""


def parse_grid(rows: list, sheet_name: str = "") -> list:
    """Parse a routine grid (Day | Room | Sec C.C. T | Sec C.C. T …, one or more blocks/shifts).

    Returns a list of dicts: day, time, room, section, course, teacher, sheet.
    """
    entries = []
    time_at: dict = {}        # column → time label   (from a row that contains time ranges)
    sec_cols: list = []       # columns where a "Sec" triple starts
    day_cols: list = []       # columns of "Day" (one per block)
    current_day: dict = {}    # block day col → current day name
    for raw in rows:
        row = [_cell(raw, i) for i in range(len(raw))]
        low = [c.lower() for c in row]
        # header row: Day, Room, Sec, C.C., T ...
        if "sec" in low and "day" in low:
            sec_cols = [i for i, c in enumerate(low) if c == "sec"]
            day_cols = [i for i, c in enumerate(low) if c == "day"]
            continue
        # time row: contains 2+ time ranges
        if sum(1 for c in row if len(TIME_RE.findall(c)) >= 2) >= 2:
            time_at = {i: _norm_time(c) for i, c in enumerate(row) if len(TIME_RE.findall(c)) >= 2}
            continue
        if not sec_cols or not day_cols:
            continue
        found = {dc: DAY_ALIASES.get(_cell(row, dc).lower()[:3]) for dc in day_cols}
        found = {dc: d for dc, d in found.items() if d}
        if found:
            # a new day starts: blocks whose own Day cell is empty follow the same day
            fallback = next(iter(found.values()))
            for dc in day_cols:
                current_day[dc] = found.get(dc, fallback)
        for sc in sec_cols:
            sec_raw, cc_raw, t_raw = _cell(row, sc), _cell(row, sc + 1), _cell(row, sc + 2)
            if not sec_raw or not cc_raw:
                continue
            block = max((dc for dc in day_cols if dc <= sc), default=None)
            if block is None or block not in current_day:
                continue
            room = _cell(row, block + 1)
            secs = [x.strip() for x in sec_raw.split("/")]
            ccs = [x.strip() for x in cc_raw.split("/")]
            ts = [x.strip() for x in t_raw.split("/")]
            last = ("", "", "")
            for s_, c_, t_ in zip_longest(secs, ccs, ts):
                s_ = s_ or last[0]
                c_ = c_ or last[1]
                t_ = t_ or last[2]
                last = (s_, c_, t_)
                if not s_ or not c_:
                    continue
                entries.append({
                    "day": current_day[block],
                    "time": time_at.get(sc, ""),
                    "room": room,
                    "section": s_.upper(),
                    "course": re.sub(r"\s+", " ", c_.upper()),
                    "teacher": t_.upper(),
                    "sheet": sheet_name,
                })
    return entries


def find_effective_date(rows: list) -> str:
    for raw in rows[:8]:
        for c in raw:
            m = DATE_RE.search(str(c or ""))
            if m:
                return m.group(1)
    return ""


CODE_RE = re.compile(r"^[A-Z]{2,5} ?\d{4}$")


def parse_titles(rows: list) -> dict:
    """'CSE 1205' → 'Digital Logic Design' from the Course Offerings sheet."""
    out = {}
    for raw in rows:
        row = [_cell(raw, i) for i in range(len(raw))]
        for i, c in enumerate(row[:-1]):
            cu = re.sub(r"\s+", " ", c.upper())
            nxt = row[i + 1]
            if CODE_RE.match(cu) and nxt and not CODE_RE.match(nxt.upper()) and len(nxt) > 3:
                out.setdefault(cu if " " in cu else re.sub(r"([A-Z]+)(\d+)", r"\1 \2", cu), nxt)
    return out


def section_routines(entries: list, settings, teacher: str, scope: str) -> dict:
    """section → sorted unique list of entries for that section."""
    codes = {info["code"].upper(): key for key, info in settings.courses.items()}
    out = {sec: [] for sec in settings.sections}
    seen = set()
    for e in entries:
        sec = settings.normalize_section(e["section"])
        if not sec:
            continue
        if scope != "all":
            if teacher and e["teacher"] != teacher.upper():
                continue
            if e["course"] not in codes:
                continue
        key = (sec, e["day"], e["time"], e["course"], e["room"], e["teacher"])
        if key in seen:
            continue
        seen.add(key)
        out[sec].append({k: e[k] for k in ("day", "time", "room", "course", "teacher")})
    for sec in out:
        out[sec].sort(key=lambda e: (DAYS.index(e["day"]), _time_key(e["time"]), e["course"]))
    return out


def _line(e) -> str:
    return f"{e['day']} · {e['time']} · {e['course']} · Room {e['room']} · {e['teacher']}"


# ====================================================================== cog
class Routine(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        cfg = self.s.raw.get("routine", {}) or {}
        self.sheet_url = cfg.get("sheet_url", "")
        self.sheet_names = cfg.get("sheets", ["CSE", "ECSE"])
        self.titles_sheet = cfg.get("titles_sheet", "Course Offerings")
        self.titles: dict = {}
        self.teacher = cfg.get("teacher_initial", "") or (self.s.raw.get("instructor", {}) or {}).get("initial", "")

        self.state: dict = load_json(STATE_FILE, {})
        hours = float(cfg.get("auto_check_hours", 6) or 0)
        self.auto = tasks.loop(hours=hours)(self._auto) if hours > 0 else None

    async def cog_load(self):
        if self.auto:
            self.auto.start()

    async def cog_unload(self):
        if self.auto:
            self.auto.cancel()

    async def _auto(self):
        await self.bot.wait_until_ready()
        try:
            report, changed = await self.check(post_summary=False)
        except Exception as e:  # noqa: BLE001
            log.warning("Auto routine check failed: %s", e)
            return
        if changed:
            await self.bot.send_log("🗓️ Routine changed", report, discord.Color.orange())

    # ------------------------------------------------------------ download + parse
    def _sheet_id(self) -> str:
        m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", self.sheet_url or "")
        return m.group(1) if m else ""

    async def fetch_rows(self) -> tuple:
        sid = self._sheet_id()
        if not sid:
            raise commands.BadArgument("No routine sheet link in config.json → `routine.sheet_url`.")
        url = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=xlsx"
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as sess:
            async with sess.get(url, allow_redirects=True) as r:
                data = await r.read()
                ctype = r.headers.get("Content-Type", "")
        if r.status != 200 or "html" in ctype.lower() or not data.startswith(b"PK"):
            raise commands.BadArgument(
                "Could not download the routine sheet. In Google Sheets click **Share → General access → "
                "Anyone with the link (Viewer)**, then try again.")
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = {}
        for name in self.sheet_names:
            if name in wb.sheetnames:
                sheets[name] = [list(r) for r in wb[name].iter_rows(values_only=True)]
        if self.titles_sheet in wb.sheetnames:
            self.titles = parse_titles([list(r) for r in wb[self.titles_sheet].iter_rows(values_only=True)])
        if not sheets:
            raise commands.BadArgument(f"Sheets {self.sheet_names} not found. Available: {', '.join(wb.sheetnames)}")
        return sheets

    async def build(self):
        """→ ({section: [all classes of the section]}, effective_date)"""
        sheets = await self.fetch_rows()
        entries, effective = [], ""
        for name, rows in sheets.items():
            entries += parse_grid(rows, name)
            effective = effective or find_effective_date(rows)
        return section_routines(entries, self.s, "", "all"), effective

    def mine(self, items: list) -> list:
        codes = {info["code"].upper() for info in self.s.courses.values()}
        t = (self.teacher or "").upper()
        return [e for e in items if (not t or e["teacher"] == t) and e["course"] in codes]

    # ------------------------------------------------------------ messages
    def routine_embed(self, sec, items, effective, kind="all") -> discord.Embed:
        """kind = 'all' (full section routine) or 'mine' (only my classes)."""
        s = self.s
        eve = " (Evening)" if s.is_evening(sec) else ""
        if kind == "all":
            title = f"📚 Full class routine — Section {sec}{eve}"
            color = discord.Color(s.section_color(sec))
            intro = f"All courses of Section {sec} · ⭐ = {self.teacher or 'my'} classes\n"
        else:
            title = f"👨‍🏫 Section {sec} — classes with {self.teacher or 'me'}"
            color = discord.Color.gold()
            intro = "Classes are **offline** (on campus) unless an online class is announced.\n"
        e = discord.Embed(title=title, color=color,
                          description=f"Effective from **{effective or '—'}** · checked {s.now():%d %b %Y, %I:%M %p}\n" + intro)
        ours = {info["code"].upper(): info for info in s.courses.values()}
        by_day: dict = {}
        for it in items:
            by_day.setdefault(it["day"], []).append(it)
        t = (self.teacher or "").upper()
        for day in DAYS:
            if day not in by_day:
                continue
            lines = []
            for it in by_day[day]:
                info = ours.get(it["course"], {})
                name = info.get("short") if kind == "mine" and info else (self.titles.get(it["course"]) or info.get("title") or "")
                lab = " 🧪" if "LAB" in (it["room"] or "").upper() or info.get("type") == "lab" else ""
                star = "⭐ " if kind == "all" and it["teacher"] == t else ""
                if kind == "all":
                    lines.append(f"{star}`{it['time']}` **{it['course']}** {name[:38]}{lab} · 🏫 {it['room']} · {it['teacher']}")
                else:
                    lines.append(f"`{it['time']}` · **{it['course']}** {name}{lab}\n   🏫 Room **{it['room']}**")
            e.add_field(name=f"📅 {day}", value="\n".join(lines)[:1024], inline=False)
        if not items:
            e.add_field(name="No classes found", value="Nothing in the current routine.", inline=False)
        e.set_footer(text="Source: official department routine · this pinned message is always the latest")
        return e

    def _link_view(self):
        """Under the pinned routine: 🔄 Update routine (teacher only) + 📄 open the sheet."""
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Update routine (teacher)", emoji="🔄", style=discord.ButtonStyle.success,
                                     custom_id="nubtk:panel:routine_check"))
        if self.sheet_url:
            v.add_item(discord.ui.Button(label="Open full routine sheet", emoji="📄", style=discord.ButtonStyle.link,
                                         url=self.sheet_url))
        v.stop()  # handled by the panel's persistent button
        return v

    async def _replace_pinned(self, sec, embeds) -> int | None:
        ch = self.bot.channel_for(f"sec:{sec}:routine")
        if ch is None:
            return None
        old = (self.state.get(sec) or {}).get("msg_id")
        if old:
            try:
                await (await ch.fetch_message(int(old))).delete()
            except discord.HTTPException:
                pass
        kw = {"embeds": embeds, "view": self._link_view()}
        msg = await ch.send(**kw)
        try:
            await msg.pin()
            async for m in ch.history(limit=5):
                if m.type == discord.MessageType.pins_add:
                    await m.delete()
        except discord.HTTPException:
            pass
        return msg.id

    # ------------------------------------------------------------ main check
    async def check(self, force_repost: bool = False, post_summary: bool = True):
        """Returns (report_text, any_changed)."""
        routines, effective = await self.build()
        lines, any_changed = [], False
        for sec, items in routines.items():
            prev = self.state.get(sec) or {}
            old_set = {_line(e) for e in prev.get("entries", [])}
            new_set = {_line(e) for e in items}
            added, removed = sorted(new_set - old_set), sorted(old_set - new_set)
            first_time = "entries" not in prev
            changed = bool(added or removed) and not first_time
            eff_changed = prev.get("effective") != effective
            ch = self.bot.channel_for(f"sec:{sec}:routine")
            if ch is None:
                lines.append(f"• **{sec}** — ⚠️ no routine channel (run `!setup_server`)")
                continue
            if first_time or changed or eff_changed or force_repost or not prev.get("msg_id"):
                mid = await self._replace_pinned(sec, [self.routine_embed(sec, items, effective, "all"),
                                                       self.routine_embed(sec, self.mine(items), effective, "mine")])
                prev = {"entries": items, "effective": effective, "msg_id": mid}
                self.state[sec] = prev
            if changed:
                any_changed = True
                role = self.bot.role_for(f"role:sec:{sec}")
                text = [f"{role.mention if role else ''} 🔔 **Class routine updated** (effective from {effective}):"]
                t = (self.teacher or "").upper()
                star = lambda x: "⭐ " if t and x.endswith(f"· {t}") else ""  # noqa: E731
                text += [f"➕ {star(x)}{x}" for x in added] + [f"➖ {star(x)}~~{x}~~" for x in removed]
                text.append("📌 The pinned message above is the latest routine.")
                try:
                    await ch.send("\n".join(text)[:1990])
                except discord.HTTPException:
                    pass
                append_audit(self.s, "routine_changed", section=sec, added=added, removed=removed)
                lines.append(f"• **{sec}** — 🔔 **updated** (+{len(added)} / −{len(removed)}) → {ch.mention}")
            elif first_time:
                lines.append(f"• **{sec}** — 📌 routine posted ({len(items)} classes, {len(self.mine(items))} yours) → {ch.mention}")
            else:
                lines.append(f"• **{sec}** — ✅ no change ({len(items)} classes, {len(self.mine(items))} yours)")
        save_json(STATE_FILE, self.state)
        report = f"🗓️ **Routine check** · effective from **{effective or '—'}**\n" + "\n".join(lines)
        return report, any_changed

    # ------------------------------------------------------------ commands
    @commands.command(name="routine_check")
    async def routine_check(self, ctx):
        msg = await ctx.send("⏳ Downloading the routine sheet…")
        report, _ = await self.check()
        await msg.edit(content=report[:1990])

    @commands.command(name="routine_repost")
    async def routine_repost(self, ctx):
        msg = await ctx.send("⏳ Re-posting routines…")
        report, _ = await self.check(force_repost=True)
        await msg.edit(content=report[:1990])

    @commands.command(name="routine")
    async def routine_show(self, ctx, section: str = "", which: str = "both"):
        """!routine · !routine 2C · !routine 2C all · !routine 2C mine"""
        sec = self.s.normalize_section(section) if section else None
        if section and not sec:
            if section.lower() in ("all", "mine", "both"):
                which, sec = section.lower(), None
            else:
                raise commands.BadArgument(f"Unknown section. Valid: {', '.join(self.s.sections)}")
        secs = [sec] if sec else list(self.s.sections)
        embeds = []
        for x in secs:
            st = self.state.get(x)
            if not st:
                continue
            items = st.get("entries", [])
            if which in ("both", "all"):
                embeds.append(self.routine_embed(x, items, st.get("effective", ""), "all"))
            if which in ("both", "mine"):
                embeds.append(self.routine_embed(x, self.mine(items), st.get("effective", ""), "mine"))
        if not embeds:
            raise commands.BadArgument("No routine saved yet — run `!routine_check` first.")
        for i in range(0, len(embeds), 4):
            await ctx.send(embeds=embeds[i:i + 4])


async def setup(bot):
    await bot.add_cog(Routine(bot))
