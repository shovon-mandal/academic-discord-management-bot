"""Admin commands (work only in the admin / bot-logs channel, only for admins)."""
from __future__ import annotations

import asyncio
import io
import logging
import re
from datetime import timedelta

import discord
from discord.ext import commands

from core.config import DATA_DIR
from core.parsing import clean_name
from core.storage import (STUDENTS_DIR, append_audit, load_roster, parse_roster_text, save_roster)
from core.system import zip_files

log = logging.getLogger("nubtk.admin")
GREEN, RED, GOLD, BLUE = discord.Color.green(), discord.Color.red(), discord.Color.gold(), discord.Color.blurple()

HELP = [
    ("🏗️ Setup", [
        ("setup_server", "Create / re-sync all roles, categories, channels (safe to re-run)"),
        ("setup_welcome", "Re-post welcome buttons + ID instructions"),
        ("reset_server confirm", "⚠️ Delete ALL channels/roles and build fresh"),
        ("invite", "Permanent invite link + ready-to-share join guide for students"),
    ]),
    ("🎓 Students", [
        ("stats", "Section & course-wise counts"),
        ("list <sec>", "All students of a section (e.g. `!list 2C`)"),
        ("find <id/name>", "Search a student"),
        ("verify @user <sec> <id> <name>", "Manually verify"),
        ("unverify @user", "Remove access + record"),
        ("move @user <sec>", "Change section"),
        ("rename @user <name>", "Fix name / nickname"),
        ("pending", "Requests waiting for approval"),
        ("notify_unverified", "Check ID channel, then DM + mention unverified"),
        ("kick_unverified <days> [confirm]", "List / kick members unverified for N days"),
        ("roster_upload <sec>", "Attach official CSV (ID,Name) → enables roster checks"),
        ("missing <sec>", "Roster students who haven't joined yet"),
    ]),
    ("📢 Communication", [
        ("announce <target> <text>", "Post + ping. target = `all` / `2C` / `2C:lab` / `CSE1205`"),
        ("announce_silent <target> <text>", "Same, without ping"),
        ("dm <target> <text>", "DM every student of the target"),
    ]),
    ("🗓️ Attendance (voice)", [
        ("att_start <sec> [course] [minutes]", "Start session — join section voice = present"),
        ("att_stop <sec>", "Stop + get CSV"),
        ("att_status", "Running sessions"),
        ("att_report <sec> [course]", "Whole-semester % per student"),
    ]),
    ("💾 Data & System", [
        ("export [all | 2C | CSE1205 | CSE1205 2C]", "Download student files (CSV/JSON)"),
        ("backup", "Zip of all data"),
        ("status", "Uptime, CPU temp, RAM, git version"),
        ("logs [lines]", "Last log lines"),
        ("update", "git pull from GitHub + restart"),
        ("restart", "Restart the bot"),
    ]),
]


class Admin(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings

    # ------------------------------------------------------------ helpers
    @property
    def ver(self):
        return self.bot.get_cog("Verification")

    def help_embed(self) -> discord.Embed:
        p = self.s.prefix
        e = discord.Embed(title="🛠️ Admin commands", color=BLUE,
                          description="Only you can see this channel. Commands work only here.")
        for title, cmds in HELP:
            e.add_field(name=title, value="\n".join(f"`{p}{c}` — {d}" for c, d in cmds)[:1024], inline=False)
        return e

    async def send_long(self, ctx, text: str, filename: str | None = None):
        if len(text) > 6000 and filename:
            await ctx.send(file=discord.File(io.BytesIO(text.encode("utf-8")), filename=filename))
            return
        chunk = ""
        for line in text.splitlines():
            if len(chunk) + len(line) + 1 > 1900:
                await ctx.send(f"```\n{chunk}```")
                chunk = ""
            chunk += line + "\n"
        if chunk.strip():
            await ctx.send(f"```\n{chunk}```")

    def need_section(self, raw) -> str:
        sec = self.s.normalize_section(raw)
        if not sec:
            raise commands.BadArgument(f"Unknown section `{raw}`. Valid: {', '.join(self.s.sections)}")
        return sec

    def resolve_target(self, raw: str):
        t = raw.strip()
        if t.lower() in ("all", "everyone", "sobai"):
            return "all", None
        lab = False
        m = re.match(r"(?i)^(.+?)[:\-_ ]?lab$", t)
        if m and self.s.normalize_section(m.group(1)):
            lab, t = True, m.group(1)
        sec = self.s.normalize_section(t)
        if sec:
            return ("section_lab" if lab else "section"), sec
        course = self.s.normalize_course(t)
        if course:
            return "course", course
        return None, None

    def target_info(self, kind, val):
        """-> (channel, roles, recipients, label)"""
        s, b = self.s, self.bot
        if kind == "all":
            return (b.channel_for("all:notice"), [b.role_for("role:verified")], b.store.all_active(), "All students")
        if kind in ("section", "section_lab"):
            ch = None
            if kind == "section_lab":
                ch = b.channel_for(f"sec:{val}:lab")
            ch = ch or b.channel_for(f"sec:{val}:announcement")
            return ch, [b.role_for(f"role:sec:{val}")], b.store.section_students(val), f"Section {val}"
        theory = s.theory_course_for(val)
        secs = s.course_sections(val)
        return (b.channel_for(f"group:{theory}:announcement") or b.channel_for(f"sec:{secs[0]}:announcement"),
                [b.role_for(f"role:sec:{x}") for x in secs], b.store.course_students(val),
                f"{s.courses[val]['code']} ({', '.join(secs)})")

    # ------------------------------------------------------------ invite
    @commands.command(name="invite")
    async def invite(self, ctx):
        wc = self.bot.channel_for("welcome_channel")
        if wc is None:
            raise commands.BadArgument("Run `!setup_server` first.")
        url = await self.bot.get_invite_url()
        if not url:
            raise commands.BadArgument("Could not create an invite link.")
        inv = type("Inv", (), {"url": url})
        ex = list(self.s.sections)[0]
        guide = (
            f"📢 {self.s.semester} · {self.s.teacher} — Join our class Discord server\n\n"
            f"1️⃣ Click this link: {inv.url}\n"
            "   (No Discord account? Create one first at discord.com or in the Discord app.)\n"
            "2️⃣ In the server, open the welcome channel and click your section button.\n"
            f"3️⃣ Write your Student ID ({self.s.id_length} digits) and full official name, then click Submit.\n"
            "✅ Done! Your class channels will open right away.\n\n"
            f"Button not working? In the id-name-section channel, type: {ex} 11250121964 Your Full Name\n\n"
            "Sections:\n" + "\n".join(f"• {x} — {self.s.short_for_section(x)}"
                                        + (" (Evening)" if self.s.is_evening(x) else "") for x in self.s.sections)
        )
        await ctx.send(f"🔗 Permanent invite: {inv.url}\n\n👇 Copy the message below and post it in your Facebook / WhatsApp class group:")
        await ctx.send(f"```\n{guide}\n```")

    # ------------------------------------------------------------ help / stats
    @commands.command(name="help", aliases=["commands"])
    async def help_cmd(self, ctx):
        await ctx.send(embed=self.help_embed())

    @commands.command(name="stats")
    async def stats(self, ctx):
        s, store = self.s, self.bot.store
        e = discord.Embed(title="📊 Students", color=GREEN, timestamp=s.now())
        total = 0
        for sec in s.sections:
            n = len(store.section_students(sec))
            total += n
            e.add_field(name=f"{sec}{' (Eve)' if s.is_evening(sec) else ''}",
                        value=f"**{n}** · {s.codes_label(s.section_courses(sec))}", inline=True)
        g = ctx.guild
        humans = [m for m in g.members if not m.bot]
        unverified = [m for m in humans if not self.ver.is_verified(m) and not self.bot.is_admin(m)]
        course_lines = []
        for c in s.courses:
            if s.course_sections(c):
                course_lines.append(f"{s.courses[c]['code']} {s.courses[c]['short']}: **{len(store.course_students(c))}**")
        e.add_field(name="Courses", value="\n".join(course_lines) or "-", inline=False)
        e.description = (f"Verified: **{total}** · Unverified members: **{len(unverified)}** · "
                         f"Pending approval: **{len(self.bot.pending.items)}** · Server members: {len(humans)}")
        await ctx.send(embed=e)

    @commands.command(name="list")
    async def list_cmd(self, ctx, section: str):
        sec = self.need_section(section)
        rows = self.bot.store.section_students(sec)
        if not rows:
            await ctx.send(f"No verified students in {sec} yet.")
            return
        lines = [f"Section {sec} · {self.s.section_courses_text(sec)} · {len(rows)} students", ""]
        lines += [f"{i:>3}. {r['student_id']}  {r['name'][:28]:<28} @{r.get('discord_name', '')}"
                  for i, r in enumerate(rows, 1)]
        await self.send_long(ctx, "\n".join(lines), filename=f"{sec}_list.txt")

    @commands.command(name="find")
    async def find(self, ctx, *, query: str):
        hits = self.bot.store.search(query)[:20]
        if not hits:
            await ctx.send("Nothing found.")
            return
        lines = [f"{r['section']:<3} {r['student_id']}  {r['name'][:26]:<26} @{r.get('discord_name', '')} "
                 f"[{r.get('status', 'active')}] <{r['discord_id']}>" for r in hits]
        await self.send_long(ctx, "\n".join(lines))

    # ------------------------------------------------------------ manual management
    @commands.command(name="verify")
    async def verify(self, ctx, member: discord.Member, section: str, student_id: str, *, name: str):
        sec = self.need_section(section)
        sid = re.sub(r"\D", "", student_id)
        if len(sid) != self.s.id_length:
            raise commands.BadArgument(f"Student ID must be {self.s.id_length} digits.")
        ok, text = await self.ver.apply_verification(ctx.guild, member, sec, sid, clean_name(name) or name.strip(),
                                                     by=f"admin:{ctx.author.name}")
        p = self.bot.pending.find_by_member(member.id)
        if ok and p:
            self.bot.pending.pop(p[0])
        await ctx.send(("✅ " if ok else "❌ ") + text)

    @commands.command(name="unverify")
    async def unverify(self, ctx, member: discord.Member):
        rec = await self.ver.remove_verification(ctx.guild, member, by=ctx.author.name)
        await ctx.send(f"✅ {member.mention} unverified." + (f" (was {rec['section']} · {rec['student_id']})" if rec else ""))

    @commands.command(name="move")
    async def move(self, ctx, member: discord.Member, section: str):
        sec = self.need_section(section)
        rec = self.bot.store.get(member.id)
        if not rec:
            raise commands.BadArgument("This member is not verified. Use `!verify` instead.")
        ok, text = await self.ver.apply_verification(ctx.guild, member, sec, rec["student_id"], rec["name"],
                                                     by=f"admin-move:{ctx.author.name}")
        await ctx.send(("✅ Moved. " if ok else "❌ ") + text)

    @commands.command(name="rename")
    async def rename(self, ctx, member: discord.Member, *, name: str):
        rec = self.bot.store.get(member.id)
        if not rec:
            raise commands.BadArgument("This member is not verified.")
        ok, text = await self.ver.apply_verification(ctx.guild, member, rec["section"], rec["student_id"], clean_name(name) or name.strip(),
                                                     by=f"admin-rename:{ctx.author.name}")
        await ctx.send(("✅ " if ok else "❌ ") + text)

    @commands.command(name="pending")
    async def pending(self, ctx):
        items = self.bot.pending.items
        if not items:
            await ctx.send("✅ No pending requests.")
            return
        for pid, it in list(items.items())[:15]:
            e = discord.Embed(title=f"🟡 Pending · {it['section']} · {it['student_id']}", color=GOLD,
                              description=f"<@{it['discord_id']}> · {it['name']}\n" + "\n".join(f"• {r}" for r in it["reasons"]))
            e.set_footer(text=f"pending id {pid} · {it['created_at']}")
            await ctx.send(embed=e, view=self.ver.pending_view(pid))

    @commands.command(name="notify_unverified")
    async def notify_unverified(self, ctx):
        msg = await ctx.send("⏳ Checking ID channel first...")
        v, e = await self.ver.scan_id_channel(reply_errors=True)
        idc = self.bot.channel_for("id_channel")
        wc = self.bot.channel_for("welcome_channel")
        targets = [m for m in ctx.guild.members if not m.bot and not self.bot.is_admin(m)
                   and not self.ver.is_verified(m) and not self.bot.pending.find_by_member(m.id)]
        sent = failed = 0
        embed = discord.Embed(
            title="🔔 Verification reminder",
            description=(f"You are not verified on **{ctx.guild.name}** yet.\n"
                         f"Go to {wc.mention if wc else '#welcome'} → click your section → fill the form.\n"
                         f"Or type in {idc.mention if idc else '#id-name-section'}: "
                         f"`{list(self.s.sections)[0]} 11250121964 Your Full Name`"),
            color=GOLD)
        for m in targets:
            try:
                await m.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(0.7)
        if idc and targets:
            for i in range(0, len(targets), 40):
                chunk = " ".join(m.mention for m in targets[i:i + 40])
                try:
                    await idc.send(f"👋 {chunk}\nYou are not verified yet — click your section button in "
                                   f"{wc.mention if wc else '#welcome'} or type here: "
                                   f"`SECTION STUDENT_ID FULL_NAME`")
                except discord.HTTPException:
                    pass
        await msg.edit(content=(f"✅ Done.\n• Verified from existing messages: **{v}**\n"
                                f"• Messages with errors (replied): **{e}**\n• Unverified now: **{len(targets)}**\n"
                                f"• DM sent: **{sent}** · DM failed: **{failed}**"))

    @commands.command(name="kick_unverified")
    async def kick_unverified(self, ctx, days: int, confirm: str = ""):
        cutoff = discord.utils.utcnow() - timedelta(days=days)
        targets = [m for m in ctx.guild.members if not m.bot and not self.bot.is_admin(m)
                   and not self.ver.is_verified(m) and m.joined_at and m.joined_at < cutoff]
        if not targets:
            await ctx.send(f"No unverified member older than {days} days.")
            return
        if confirm.lower() != "confirm":
            names = "\n".join(f"{m.name} (joined {m.joined_at:%Y-%m-%d})" for m in targets[:60])
            await self.send_long(ctx, f"{len(targets)} member(s) would be kicked:\n{names}")
            await ctx.send(f"Run `{self.s.prefix}kick_unverified {days} confirm` to kick them.")
            return
        kicked = 0
        for m in targets:
            try:
                await m.kick(reason=f"Unverified for {days}+ days")
                kicked += 1
            except discord.HTTPException:
                pass
        append_audit(self.s, "kick_unverified", days=days, count=kicked, by=ctx.author.name)
        await ctx.send(f"✅ Kicked {kicked} member(s).")

    # ------------------------------------------------------------ roster
    @commands.command(name="roster_upload")
    async def roster_upload(self, ctx, section: str):
        sec = self.need_section(section)
        if not ctx.message.attachments:
            raise commands.BadArgument("Attach a .csv / .txt file (one student per line: ID, Name).")
        raw = await ctx.message.attachments[0].read()
        entries = parse_roster_text(raw.decode("utf-8-sig", errors="replace"), self.s.id_length)
        if not entries:
            raise commands.BadArgument("No valid student IDs found in the file.")
        path = save_roster(sec, entries)
        note = "" if self.s.roster_check else ("\nℹ️ `roster_check` is **false** in config.json — the roster is "
                                               "only used by `!missing`. Set it to true to block wrong IDs.")
        await ctx.send(f"✅ Roster for **{sec}** saved: **{len(entries)}** students → `{path.name}`{note}")

    @commands.command(name="missing")
    async def missing(self, ctx, section: str):
        sec = self.need_section(section)
        roster = load_roster(sec)
        if not roster:
            raise commands.BadArgument(f"No roster for {sec}. Use `!roster_upload {sec}` with a CSV.")
        verified_ids = {r["student_id"] for r in self.bot.store.section_students(sec)}
        missing = sorted(set(roster) - verified_ids)
        extra = sorted(verified_ids - set(roster))
        lines = [f"Section {sec}: roster {len(roster)} · verified {len(verified_ids)} · missing {len(missing)}", "",
                 "NOT JOINED / NOT VERIFIED:"] + [f"  {sid}  {roster[sid]}" for sid in missing]
        if extra:
            lines += ["", "VERIFIED BUT NOT IN ROSTER:"] + [f"  {sid}" for sid in extra]
        await self.send_long(ctx, "\n".join(lines), filename=f"{sec}_missing.txt")

    # ------------------------------------------------------------ communication
    async def _announce(self, ctx, target: str, text: str, ping: bool):
        kind, val = self.resolve_target(target)
        if not kind:
            raise commands.BadArgument("Target must be `all`, a section like `2C`, `2C:lab`, or a course like `CSE1205`.")
        ch, roles, _, label = self.target_info(kind, val)
        if ch is None:
            raise commands.BadArgument("Announcement channel not found. Run `!setup_server`.")
        if not text and not ctx.message.attachments:
            raise commands.BadArgument("Write the announcement text after the target.")
        files = [await a.to_file() for a in ctx.message.attachments[:10]]
        embed = discord.Embed(title="📢 Announcement", description=text or None, color=GOLD, timestamp=self.s.now())
        embed.set_footer(text=f"{self.s.teacher} · {label}")
        content = " ".join(r.mention for r in roles if r) if ping else None
        kwargs = {"embed": embed}
        if content:
            kwargs["content"] = content
        if files:
            kwargs["files"] = files
        msg = await ch.send(**kwargs)
        await ctx.send(f"✅ Posted in {ch.mention} → {msg.jump_url}")

    @commands.command(name="announce")
    async def announce(self, ctx, target: str, *, text: str = ""):
        await self._announce(ctx, target, text, ping=True)

    @commands.command(name="announce_silent")
    async def announce_silent(self, ctx, target: str, *, text: str = ""):
        await self._announce(ctx, target, text, ping=False)

    @commands.command(name="dm")
    async def dm(self, ctx, target: str, *, text: str):
        kind, val = self.resolve_target(target)
        if not kind:
            raise commands.BadArgument("Target must be `all`, `2C`, or `CSE1205`.")
        _, _, recipients, label = self.target_info(kind, val)
        status = await ctx.send(f"⏳ Sending DM to {len(recipients)} students ({label})...")
        embed = discord.Embed(title=f"📩 Message from {self.s.teacher}", description=text, color=BLUE,
                              timestamp=self.s.now())
        embed.set_footer(text=f"{ctx.guild.name} · {label}")
        sent, failed = 0, []
        for r in recipients:
            m = ctx.guild.get_member(int(r["discord_id"]))
            if not m:
                failed.append(r["name"])
                continue
            try:
                await m.send(embed=embed)
                sent += 1
            except discord.HTTPException:
                failed.append(r["name"])
            await asyncio.sleep(1.0)
        await status.edit(content=f"✅ DM sent: **{sent}** · failed: **{len(failed)}**"
                                  + (f"\nFailed: {', '.join(failed[:30])}" if failed else ""))

    # ------------------------------------------------------------ export
    @commands.command(name="export")
    async def export(self, ctx, *, target: str = "all"):
        store, s = self.bot.store, self.s
        store.export_files()
        parts = target.split()
        if not parts or parts[0].lower() == "all":
            paths = sorted(STUDENTS_DIR.glob("*.csv")) + sorted(STUDENTS_DIR.glob("*.json"))
            paths.append(DATA_DIR / "verified_records.json")
            zpath = zip_files(paths, DATA_DIR / "exports" / f"students_{s.now():%Y%m%d_%H%M}.zip")
            await ctx.send("📦 All student files (CSV + JSON):", file=discord.File(zpath))
            return
        secs, courses = [], []
        for p in parts:
            sec, c = s.normalize_section(p), s.normalize_course(p)
            if sec:
                secs.append(sec)
            elif c:
                courses.append(c)
            else:
                raise commands.BadArgument(f"`{p}` is not a section or course.")
        pairs = []
        for sec in (secs or list(s.sections)):
            for c in s.section_courses(sec):
                if not courses or c in courses:
                    pairs.append((c, sec))
        if not pairs:
            raise commands.BadArgument("That course/section combination does not exist.")
        files = [discord.File(STUDENTS_DIR / f"{c}_{sec}.csv") for c, sec in pairs
                 if (STUDENTS_DIR / f"{c}_{sec}.csv").exists()][:10]
        await ctx.send(f"📄 {', '.join(f'{c}_{sec}' for c, sec in pairs)}", files=files)


async def setup(bot):
    await bot.add_cog(Admin(bot))
