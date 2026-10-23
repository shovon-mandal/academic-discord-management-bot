"""Voice-channel attendance sessions.

!att_start 2C            → anyone (verified, in 2C) joining a voice channel of the 2C category = present
!att_start 2C CSE1206 20 → lab attendance, auto-stops after 20 minutes
!att_stop 2C             → saves data/attendance/CSE1205_2C/2026-10-24_1030.csv and posts it
"""
from __future__ import annotations

import csv
import logging
from collections import OrderedDict
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from core.storage import ATTENDANCE_DIR, append_audit, load_json, save_json, write_csv

log = logging.getLogger("nubtk.attendance")
HEADER = ["SL", "Student ID", "Name", "Status", "Join Time", "Voice Channel", "Discord Username"]


class Attendance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        self.path = ATTENDANCE_DIR / "active_sessions.json"
        self.sessions: dict = load_json(self.path, {})

    async def cog_load(self):
        self.auto_stop.start()

    async def cog_unload(self):
        self.auto_stop.cancel()

    def _save(self):
        save_json(self.path, self.sessions)

    def _category(self, sec):
        return self.bot.channel_for(f"sec:{sec}:category")

    def _mark(self, sec, member, channel_name):
        sess = self.sessions.get(sec)
        if not sess or member.bot or self.bot.is_admin(member):
            return
        did = str(member.id)
        if did in sess["present"]:
            return
        sess["present"][did] = {"time": self.s.now().strftime("%H:%M:%S"), "display": member.display_name,
                                "username": member.name, "channel": channel_name}
        self._save()

    def _need_section(self, raw):
        sec = self.s.normalize_section(raw)
        if not sec:
            raise commands.BadArgument(f"Unknown section `{raw}`. Valid: {', '.join(self.s.sections)}")
        return sec

    # ------------------------------------------------------------ commands
    @commands.command(name="att_start")
    async def att_start(self, ctx, section: str, *args):
        sec = self._need_section(section)
        if sec in self.sessions:
            raise commands.BadArgument(f"Attendance for {sec} is already running. Use `!att_stop {sec}`.")
        course, minutes = None, 0
        for a in args:
            if a.isdigit() and len(a) <= 3:
                minutes = int(a)
                continue
            c = self.s.normalize_course(a)
            if not c or c not in self.s.section_courses(sec):
                raise commands.BadArgument(f"`{a}` is not a course of {sec}: "
                                           f"{', '.join(self.s.section_courses(sec))}")
            course = c
        course = course or self.s.default_course(sec)
        now = self.s.now()
        self.sessions[sec] = {
            "section": sec, "course": course, "started_at": now.isoformat(timespec="seconds"),
            "ends_at": (now + timedelta(minutes=minutes)).isoformat(timespec="seconds") if minutes else None,
            "started_by": ctx.author.name, "present": {},
        }
        self._save()
        cat = self._category(sec)
        if cat:
            for vc in cat.voice_channels:
                for m in vc.members:
                    self._mark(sec, m, vc.name)
        info = self.s.courses[course]
        ann = self.bot.channel_for(f"sec:{sec}:announcement")
        role = self.bot.role_for(f"role:sec:{sec}")
        if ann:
            try:
                await ann.send(f"{role.mention if role else ''} 🟢 **Attendance is open** for {info['code']} "
                               f"{info['short']} — join a voice channel of this section now."
                               + (f" (closes in {minutes} min)" if minutes else ""))
            except discord.HTTPException:
                pass
        await ctx.send(f"🟢 Attendance started: **{sec} · {info['code']} {info['short']}**"
                       + (f" · auto-stop in {minutes} min" if minutes else " · stop with `!att_stop " + sec + "`")
                       + f"\nAlready present: {len(self.sessions[sec]['present'])}")

    @commands.command(name="att_stop")
    async def att_stop(self, ctx, section: str):
        sec = self._need_section(section)
        sess = self.sessions.pop(sec, None)
        self._save()
        if not sess:
            raise commands.BadArgument(f"No running attendance for {sec}.")
        path, summary = self._finalize(sess)
        await ctx.send(summary, file=discord.File(path))

    @commands.command(name="att_status")
    async def att_status(self, ctx):
        if not self.sessions:
            await ctx.send("No attendance session is running.")
            return
        lines = [f"• **{sec}** {self.s.courses[x['course']]['code']} — present {len(x['present'])} — since "
                 f"{x['started_at'][11:16]}" + (f" · ends {x['ends_at'][11:16]}" if x.get("ends_at") else "")
                 for sec, x in self.sessions.items()]
        await ctx.send("\n".join(lines))

    @commands.command(name="att_report")
    async def att_report(self, ctx, section: str, course: str = None):
        sec = self._need_section(section)
        c = self.s.normalize_course(course) if course else self.s.default_course(sec)
        if not c or c not in self.s.section_courses(sec):
            raise commands.BadArgument(f"Course must be one of {', '.join(self.s.section_courses(sec))}")
        folder = ATTENDANCE_DIR / f"{c}_{sec}"
        files = sorted(folder.glob("*.csv")) if folder.exists() else []
        if not files:
            raise commands.BadArgument(f"No attendance files for {c} {sec} yet.")
        counts: dict = OrderedDict()
        for r in self.bot.store.section_students(sec):
            counts[r["student_id"]] = {"name": r["name"], "present": 0}
        for f in files:
            with open(f, encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    sid = row.get("Student ID", "")
                    if not sid:
                        continue
                    entry = counts.setdefault(sid, {"name": row.get("Name", ""), "present": 0})
                    if row.get("Status", "").startswith("P"):
                        entry["present"] += 1
        total = len(files)
        rows = [[i, sid, v["name"], v["present"], total, f"{100 * v['present'] / total:.0f}%"]
                for i, (sid, v) in enumerate(sorted(counts.items()), 1)]
        out = ATTENDANCE_DIR / f"{c}_{sec}_report.csv"
        write_csv(out, ["SL", "Student ID", "Name", "Present", "Total Classes", "Percentage"], rows)
        low = [r for r in rows if total and r[3] / total < 0.6]
        await ctx.send(f"📈 **{c} {sec}** — {total} classes · {len(rows)} students · below 60%: {len(low)}",
                       file=discord.File(out))

    # ------------------------------------------------------------ finalize
    def _finalize(self, sess):
        sec, course = sess["section"], sess["course"]
        present = sess["present"]
        roster = self.bot.store.section_students(sec)
        roster_ids = {r["discord_id"] for r in roster}
        rows, p = [], 0
        for i, r in enumerate(roster, 1):
            hit = present.get(r["discord_id"])
            p += bool(hit)
            rows.append([i, r["student_id"], r["name"], "P" if hit else "A",
                         hit["time"] if hit else "", hit["channel"] if hit else "", r.get("discord_name", "")])
        extras = [(did, x) for did, x in present.items() if did not in roster_ids]
        for did, x in extras:
            rec = self.bot.store.get(did) or {}
            rows.append([len(rows) + 1, rec.get("student_id", ""), rec.get("name", x["display"]),
                         "P* (other section)" if rec else "P* (unverified)", x["time"], x["channel"], x["username"]])
        started = datetime.fromisoformat(sess["started_at"])
        path = ATTENDANCE_DIR / f"{course}_{sec}" / f"{started:%Y-%m-%d_%H%M}.csv"
        write_csv(path, HEADER, rows)
        append_audit(self.s, "attendance", section=sec, course=course, present=p, total=len(roster), file=path.name)
        info = self.s.courses[course]
        summary = (f"🗓️ **Attendance · {sec} · {info['code']} {info['short']}** · {started:%d %b %Y %H:%M}\n"
                   f"Present **{p}/{len(roster)}** · Absent {len(roster) - p}"
                   + (f" · Others {len(extras)}" if extras else ""))
        return path, summary

    @tasks.loop(seconds=30)
    async def auto_stop(self):
        now = self.s.now()
        for sec, sess in list(self.sessions.items()):
            if sess.get("ends_at") and now >= datetime.fromisoformat(sess["ends_at"]):
                self.sessions.pop(sec, None)
                self._save()
                path, summary = self._finalize(sess)
                ch = self.bot.channel_for("admin_channel")
                if ch:
                    try:
                        await ch.send("⏱️ Auto-stopped.\n" + summary, file=discord.File(path))
                    except discord.HTTPException:
                        pass

    @auto_stop.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()

    # ------------------------------------------------------------ voice events
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot or not self.sessions or after.channel is None:
            return
        if before.channel and before.channel.id == after.channel.id:
            return
        for sec in list(self.sessions):
            cat = self._category(sec)
            if cat and after.channel.category_id == cat.id:
                self._mark(sec, member, after.channel.name)


async def setup(bot):
    await bot.add_cog(Attendance(bot))
