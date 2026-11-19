"""🏫 Section dashboard · 🗂️ semester archive · 🆕 new semester reset.

!dash 3D          one-look summary of a section (students, today, deadlines, changes, attendance, submissions)
!archive          ZIP: all data + students Excel + every section channel's posts as text → admin channel
!new_semester     archive first, then clear students / deadlines / attendance and remove student roles
                  (asks twice). After that: tell Claude the new sections/courses → config.json → !setup_server.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import shutil
import zipfile
from datetime import timedelta

import discord
from discord.ext import commands

from core.config import BASE_DIR, DATA_DIR
from cogs.routine import _time_key
from core.storage import ATTENDANCE_DIR, load_json

log = logging.getLogger("nubtk.semester")
ARCHIVE_DIR = BASE_DIR / "backups" / "archives"
UPLOAD_LIMIT = 9 * 1024 * 1024


class Semester(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings

    # ------------------------------------------------------------ dashboard
    def _attendance_summary(self, sec) -> list:
        out = []
        n_students = max(1, len(self.bot.store.section_students(sec)))
        for c in self.s.section_courses(sec):
            folder = ATTENDANCE_DIR / f"{c}_{sec}"
            files = sorted(folder.glob("*.csv")) if folder.exists() else []
            if not files:
                continue
            pcts = []
            for f in files:
                try:
                    with open(f, encoding="utf-8-sig", newline="") as fh:
                        rows = list(csv.DictReader(fh))
                    present = sum(1 for r in rows if (r.get("Status") or "").startswith("P"))
                    pcts.append(100 * present / n_students)
                except OSError:
                    pass
            if pcts:
                out.append(f"**{self.s.courses[c]['code']}** · {len(files)} classes · avg present "
                           f"**{sum(pcts) / len(pcts):.0f}%** · last **{pcts[-1]:.0f}%**")
        return out

    def dashboard(self, sec) -> discord.Embed:
        r = self.bot.get_cog("Routine")
        store = self.bot.store
        studs = store.section_students(sec)
        extra = sum(1 for x in studs if x.get("section") != sec)
        pend = sum(1 for p in self.bot.pending.items.values() if p.get("section") == sec)
        role = self.bot.role_for(f"role:sec:{sec}")
        members = len(role.members) if role else 0
        e = discord.Embed(title=f"📊 Section {sec} — dashboard", color=discord.Color(self.s.section_color(sec)),
                          timestamp=self.s.now(),
                          description=f"📚 {self.s.section_courses_text(sec)}")
        e.add_field(name="👥 Students", inline=False,
                    value=f"Verified **{len(studs)}** (extra-section {extra}) · with the role on Discord **{members}**"
                          + (f" · ⏳ pending **{pend}**" if pend else ""))
        now = self.s.now()
        if r:
            nxt = r.class_occurrences(sec, 14)
            nxt = [(d, it) for d, it in nxt
                   if not (d == now.date() and _time_key(it["time"]) < now.hour * 60 + now.minute)][:3]
            if nxt:
                e.add_field(name="🏫 Next classes with you", inline=False, value="\n".join(
                    f"{d:%a %d %b} · {it['time']} · {it['course']} · Room {it['room']}" for d, it in nxt))
            evs = r.events_between(sec, now - timedelta(hours=1), now + timedelta(days=21))
            if evs:
                e.add_field(name="📌 Deadlines & tests (3 weeks)", inline=False,
                            value="\n".join(r.event_line(w, ev) for w, _, ev in evs)[:1024])
            chg = []
            for i in range(14):
                chg += r.changes_on(sec, (now + timedelta(days=i)).date())
            if chg:
                e.add_field(name="⚠️ Class changes", inline=False, value="\n".join(chg)[:1024])
        att = self._attendance_summary(sec)
        e.add_field(name="🗓️ Attendance", inline=False, value="\n".join(att) if att else "No attendance taken yet.")
        posts = load_json(DATA_DIR / "submission_posts.json", {})
        sec_ids = {self.bot.ids.get(k) for k in self.bot.ids.data if k.startswith(f"sec:{sec}:")}
        topics = [k.split("|")[0] for k in posts if int(k.split("|")[1]) in sec_ids]
        if topics:
            e.add_field(name="📝 Submission lists posted", inline=False, value=", ".join(dict.fromkeys(topics))[:1024])
        return e

    @commands.command(name="dash", aliases=["dashboard"])
    async def dash(self, ctx, section: str = ""):
        secs = [self.s.normalize_section(section)] if section else list(self.s.sections)
        secs = [x for x in secs if x]
        if not secs:
            raise commands.BadArgument(f"Section must be one of {', '.join(self.s.sections)}")
        for i in range(0, len(secs), 10):
            await ctx.send(embeds=[self.dashboard(x) for x in secs[i:i + 10]])

    # ------------------------------------------------------------ archive
    async def _channel_text(self, ch, limit=3000) -> str:
        lines = [f"# {ch.name}  ({ch.category.name if ch.category else ''})", ""]
        try:
            msgs = [m async for m in ch.history(limit=limit, oldest_first=True)]
        except discord.HTTPException:
            return ""
        for m in msgs:
            parts = [m.content or ""]
            for em in m.embeds:
                parts += [em.title or "", em.description or ""] + [f"{f.name}: {f.value}" for f in em.fields]
            parts += [f"[file] {a.filename} {a.url}" for a in m.attachments]
            text = "\n".join(p for p in parts if p).strip()
            if text:
                lines.append(f"--- {m.created_at.astimezone(self.s.tz):%Y-%m-%d %H:%M} · {m.author.display_name}")
                lines.append(text)
        return "\n".join(lines) if len(lines) > 2 else ""

    async def build_archive(self, progress=None):
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        path = ARCHIVE_DIR / f"semester_archive_{self.s.now():%Y%m%d_%H%M}.zip"
        texts = {}
        g = self.bot.main_guild()
        keys = [k for k in getattr(self.bot.ids, "data", {}) if k.startswith(("sec:", "group:", "all:"))]
        for k in keys:
            ch = g.get_channel(self.bot.ids.get(k)) if g else None
            if isinstance(ch, discord.TextChannel):
                if progress:
                    await progress(f"⏳ Reading #{ch.name} ({ch.category.name if ch.category else ''})...")
                t = await self._channel_text(ch)
                if t:
                    folder = (ch.category.name if ch.category else "other").replace("/", "-")
                    texts[f"channels/{folder}/{ch.name}.txt"] = t
        xlsx = None
        try:
            from core.excel import build_student_workbook
            pairs = [(c, sec) for sec in self.s.sections for c in self.s.section_courses(sec)]
            xlsx = await asyncio.to_thread(build_student_workbook, self.bot.store, self.s, pairs,
                                           DATA_DIR / "exports" / "students_all_archive.xlsx")
        except Exception as e:  # noqa: BLE001
            log.warning("archive excel: %s", e)

        def _zip():
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
                if DATA_DIR.exists():
                    for p in DATA_DIR.rglob("*"):
                        if p.is_file() and not p.name.endswith(".tmp"):
                            z.write(p, "data/" + str(p.relative_to(DATA_DIR)))
                z.write(BASE_DIR / "config.json", "config.json")
                if xlsx:
                    z.write(xlsx, "students_all.xlsx")
                for name, t in texts.items():
                    z.writestr(name, t)
        await asyncio.to_thread(_zip)
        return path, len(texts)

    async def send_archive(self, report):
        path, n = await self.build_archive(report)
        size = path.stat().st_size
        msg = f"🗂️ **Semester archive** · {n} channel(s) · {size // 1024} KB · saved on the device: `backups/archives/{path.name}`"
        adm = self.bot.channel_for("admin_channel")
        if adm and size < UPLOAD_LIMIT:
            await adm.send(msg, file=discord.File(path))
        elif adm:
            await adm.send(msg + "\n(too big to upload — copy it from the Tinker Board)")
        return path

    @commands.command(name="archive")
    async def archive(self, ctx, confirm: str = ""):
        if confirm.upper() != "ARCHIVE":
            await ctx.send("⚠️ Safety lock: type `!archive ARCHIVE` to build the semester archive.")
            return
        m = await ctx.send("⏳ Building the archive...")

        async def report(t):
            try:
                await m.edit(content=t[:1990])
            except discord.HTTPException:
                pass
        await self.send_archive(report)
        await m.edit(content="✅ Archive ready (see the file above/below).")

    # ------------------------------------------------------------ new semester
    async def new_semester(self, report) -> str:
        await self.send_archive(report)
        g = self.bot.main_guild()
        removed = 0
        roles = [self.bot.role_for("role:verified")] + [self.bot.role_for(f"role:sec:{x}") for x in self.s.sections]
        roles = [r for r in roles if r]
        if g:
            for mem in g.members:
                if mem.bot or self.bot.is_admin(mem):
                    continue
                rs = [r for r in roles if r in mem.roles]
                if rs:
                    try:
                        await mem.remove_roles(*rs, reason="New semester")
                        removed += 1
                    except discord.HTTPException:
                        pass
                    if removed % 10 == 0:
                        await report(f"⏳ Removing student roles... {removed}")
        stamp = f"{self.s.now():%Y%m%d}"
        old = DATA_DIR / f"old_semester_{stamp}"
        old.mkdir(parents=True, exist_ok=True)
        for name in ("verified_records.json", "pending.json", "events.json", "class_changes.json",
                     "submission_posts.json", "students", "attendance", "exports"):
            p = DATA_DIR / name
            if p.exists():
                shutil.move(str(p), str(old / name))
        # reload in-memory stores
        self.bot.store.records = {}
        self.bot.store.save()
        self.bot.pending.items = {}
        self.bot.pending.save()
        r = self.bot.get_cog("Routine")
        if r:
            r._ev = {}
            r._chg = {}
            r.save_events()
            r.save_changes()
        return (f"✅ **New semester ready.** Archive posted · student roles removed from **{removed}** member(s) · "
                f"old data moved to `data/{old.name}`.\n"
                "Next: tell Claude the new sections + courses (config.json) → after the update run `!setup_server` "
                "(old section channels are removed, new ones created) → students verify again.")

    @commands.command(name="new_semester")
    async def new_semester_cmd(self, ctx):
        await ctx.send("Use **🎛️ Panel → 🏫 Section & semester → 🆕 New semester** (it asks for confirmation).")


async def setup(bot):
    await bot.add_cog(Semester(bot))
