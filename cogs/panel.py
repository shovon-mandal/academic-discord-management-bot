"""🎛️ Admin control panel — buttons, dropdowns and forms for (almost) every admin job.

A persistent panel message is pinned in 🛠️┃admin. Every button opens a small private
(ephemeral) menu: pick section / course / time / student from dropdowns, fill a form, done.
Results (files, summaries) are posted in the admin channel. Text commands still work too.

Also here: scheduled announcements (post later at a chosen time).
"""
from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta

import discord
from discord.ext import commands, tasks

from core.config import DATA_DIR
from core.storage import load_json, save_json

log = logging.getLogger("nubtk.panel")
SCHEDULE_FILE = DATA_DIR / "scheduled.json"


# ====================================================================== helpers
class PanelCtx:
    """Looks enough like commands.Context for the existing command functions."""

    def __init__(self, interaction: discord.Interaction, attachments=None):
        self.bot = interaction.client
        self.guild = interaction.guild
        self.author = interaction.user
        self.channel = interaction.channel
        self.message = type("Msg", (), {"attachments": attachments or []})()

    async def send(self, content=None, **kwargs):
        return await self.channel.send(content, **kwargs)


async def run_command(interaction: discord.Interaction, name: str, *args, **kwargs):
    """Run an existing text command's code from a button. Errors are shown to the admin."""
    bot = interaction.client
    cmd = bot.get_command(name)
    ctx = PanelCtx(interaction)
    try:
        await cmd.callback(cmd.cog, ctx, *args, **kwargs)
    except (commands.BadArgument, commands.UserInputError) as e:
        await ctx.send(f"⚠️ {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("Panel action %s failed", name)
        await ctx.send(f"❌ `{name}` failed: `{str(e)[:300]}`")


async def done(interaction: discord.Interaction, text: str = "✅ Done — see the result in the channel."):
    """Close an ephemeral menu nicely."""
    try:
        if interaction.response.is_done():
            await interaction.edit_original_response(content=text, view=None, embed=None)
        else:
            await interaction.response.edit_message(content=text, view=None, embed=None)
    except discord.HTTPException:
        pass


def section_options(settings, with_courses=True):
    out = []
    for sec in settings.sections:
        desc = settings.section_courses_text(sec) + (" · Evening" if settings.is_evening(sec) else "")
        out.append(discord.SelectOption(label=f"Section {sec}", value=sec, description=desc[:100] if with_courses else None,
                                        emoji="📘"))
    return out[:25]


def course_options(settings, sec):
    return [discord.SelectOption(label=f"{settings.courses[c]['code']} · {settings.courses[c]['short']}",
                                 value=c, description=settings.courses[c]["title"][:100],
                                 emoji="🧪" if settings.courses[c].get("type") == "lab" else "📗",
                                 default=(i == 0))
            for i, c in enumerate(settings.section_courses(sec))]


def target_options(settings, include_lab=True, include_courses=True):
    opts = [discord.SelectOption(label="All students", value="all", emoji="🏫", description="Notice board, everyone")]
    for sec in settings.sections:
        opts.append(discord.SelectOption(label=f"Section {sec}", value=sec, emoji="📘",
                                         description=settings.section_courses_text(sec)[:100]))
    if include_lab:
        for sec in settings.sections:
            if settings.section_has_lab(sec):
                opts.append(discord.SelectOption(label=f"Section {sec} — lab channel", value=f"{sec}:lab", emoji="🧪"))
    if include_courses:
        for theory, g in settings.course_groups().items():
            opts.append(discord.SelectOption(
                label=f"{settings.codes_label(g['courses'])} · {settings.courses[theory]['short']}"[:100],
                value=theory, emoji="🌐", description=f"All sections: {', '.join(g['sections'])}"[:100]))
    return opts[:25]


DURATIONS = [("No auto-stop", 0), ("5 minutes", 5), ("10 minutes", 10), ("15 minutes", 15), ("20 minutes", 20),
             ("30 minutes", 30), ("45 minutes", 45), ("60 minutes", 60), ("90 minutes", 90), ("120 minutes", 120)]


class BaseMenu(discord.ui.View):
    """Ephemeral menu that only the admin who opened it can use."""

    def __init__(self, cog, user_id: int, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.cog = cog
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This menu belongs to someone else.", ephemeral=True)
            return False
        return True


class ConfirmView(BaseMenu):
    def __init__(self, cog, user_id, on_confirm, label="Yes, continue", danger=True, timeout=120):
        super().__init__(cog, user_id, timeout)
        self.on_confirm = on_confirm
        yes = discord.ui.Button(label=label, style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
                                emoji="⚠️" if danger else "✅")
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _yes(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="⏳ Working...", view=None)
        await self.on_confirm(interaction)

    async def _no(self, interaction: discord.Interaction):
        self.stop()
        await interaction.response.edit_message(content="❎ Cancelled.", view=None, embed=None)


# ====================================================================== communication
class TargetMenu(BaseMenu):
    """mode: announce | dm | excel"""

    def __init__(self, cog, user_id, mode):
        super().__init__(cog, user_id)
        self.mode = mode
        s = cog.s
        multi = mode in ("announce", "dm")
        opts = target_options(s, include_lab=(mode == "announce"), include_courses=True)
        if mode == "excel":
            opts = [discord.SelectOption(label="Everything (all sections)", value="all", emoji="📗")] + \
                   [o for o in opts if o.value != "all" and ":lab" not in o.value]
        self.select = discord.ui.Select(placeholder="Choose who / what…", options=opts[:25],
                                        min_values=1, max_values=len(opts[:25]) if multi else 1)
        self.select.callback = self._picked
        self.add_item(self.select)
        if mode == "announce":
            for label, ping, style, emoji in (("Write & ping", True, discord.ButtonStyle.primary, "📢"),
                                              ("Write (no ping)", False, discord.ButtonStyle.secondary, "🔕")):
                b = discord.ui.Button(label=label, style=style, emoji=emoji)
                b.callback = self._make_announce(ping)
                self.add_item(b)
        elif mode == "dm":
            b = discord.ui.Button(label="Write message", style=discord.ButtonStyle.primary, emoji="✉️")
            b.callback = self._open_dm
            self.add_item(b)

    async def _picked(self, interaction: discord.Interaction):
        if self.mode == "excel":
            await interaction.response.edit_message(content="⏳ Building Excel…", view=None)
            target = self.select.values[0]
            await run_command(interaction, "excel", target=target)
            await done(interaction)
            return
        await interaction.response.defer()  # keep menu open; selection is remembered

    def _make_announce(self, ping):
        async def cb(interaction: discord.Interaction):
            if not self.select.values:
                await interaction.response.send_message("Pick at least one target first.", ephemeral=True)
                return
            await interaction.response.send_modal(AnnounceModal(self.cog, list(self.select.values), ping, interaction))
        return cb

    async def _open_dm(self, interaction: discord.Interaction):
        if not self.select.values:
            await interaction.response.send_message("Pick at least one target first.", ephemeral=True)
            return
        await interaction.response.send_modal(DMModal(self.cog, list(self.select.values), interaction))


class AnnounceModal(discord.ui.Modal):
    def __init__(self, cog, targets, ping, menu_interaction):
        super().__init__(title="📢 New announcement", timeout=900)
        self.cog, self.targets, self.ping, self.menu = cog, targets, ping, menu_interaction
        self.head = discord.ui.TextInput(label="Title", default="📢 Announcement", max_length=200, required=False)
        self.body = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=4000,
                                         placeholder="e.g. Class test on Sunday at 10 AM. Syllabus: Chapter 1–3.")
        self.when = discord.ui.TextInput(label="Schedule (optional) — leave empty to post now", required=False,
                                         placeholder="e.g. 2026-10-26 09:00   or   21:30   or   +2h", max_length=30)
        for x in (self.head, self.body, self.when):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        when_raw = (self.when.value or "").strip()
        if when_raw:
            when = self.cog.parse_when(when_raw)
            if not when:
                await interaction.response.send_message(
                    "⚠️ Could not read the time. Use `2026-10-26 09:00`, `21:30` or `+2h` / `+30m`.", ephemeral=True)
                return
            jid = self.cog.add_schedule(self.targets, self.body.value, self.head.value, self.ping, when,
                                        interaction.user.name)
            await interaction.response.send_message(
                f"🗓️ Scheduled for **{when:%a %d %b %Y, %H:%M}** → {', '.join(self.targets)} (id `{jid}`).",
                ephemeral=True)
            await done(self.menu, "🗓️ Scheduled.")
            return
        await interaction.response.send_message("⏳ Posting…", ephemeral=True)
        for t in self.targets:
            await self.cog.announce_with_title(interaction, t, self.body.value, self.ping,
                                               self.head.value or "📢 Announcement")
        await done(self.menu, "✅ Posted.")


class DMModal(discord.ui.Modal):
    def __init__(self, cog, targets, menu_interaction):
        super().__init__(title="✉️ DM students", timeout=900)
        self.cog, self.targets, self.menu = cog, targets, menu_interaction
        self.body = discord.ui.TextInput(label="Message", style=discord.TextStyle.paragraph, max_length=3500)
        self.add_item(self.body)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("⏳ Sending DMs… (about 1 second per student)", ephemeral=True)
        targets = ["all"] if "all" in self.targets else self.targets
        for t in targets:
            await run_command(interaction, "dm", t, text=self.body.value)
        await done(self.menu, "✅ DMs sent — summary is in the channel.")


class ScheduledMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        jobs = cog.jobs
        if jobs:
            opts = [discord.SelectOption(
                label=f"{datetime.fromisoformat(j['when']):%d %b %H:%M} → {', '.join(j['targets'])}"[:100],
                value=jid, description=(j.get("text") or "")[:100]) for jid, j in list(jobs.items())[:25]]
            sel = discord.ui.Select(placeholder="Select posts to cancel…", options=opts, min_values=1,
                                    max_values=len(opts))
            sel.callback = self._cancel
            self.add_item(sel)

    async def _cancel(self, interaction: discord.Interaction):
        for jid in interaction.data.get("values", []):
            self.cog.jobs.pop(jid, None)
        self.cog.save_jobs()
        await interaction.response.edit_message(content="🗑️ Cancelled the selected scheduled posts.", view=None,
                                                embed=None)


# ====================================================================== attendance
class AttStartMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.sec = None
        self.course = None
        self.minutes = 0
        self._build()

    def _build(self):
        self.clear_items()
        s = self.cog.s
        running = set(self.cog.bot.get_cog("Attendance").sessions)
        sec_opts = [o for o in section_options(s) if o.value not in running]
        for o in sec_opts:
            o.default = (o.value == self.sec)
        if not sec_opts:
            return
        sel = discord.ui.Select(placeholder="1️⃣ Choose section", options=sec_opts, row=0)
        sel.callback = self._sec
        self.add_item(sel)
        if self.sec:
            copts = course_options(s, self.sec)
            for o in copts:
                o.default = (o.value == self.course)
            csel = discord.ui.Select(placeholder="2️⃣ Course", options=copts, row=1)
            csel.callback = self._course
            self.add_item(csel)
            dopts = [discord.SelectOption(label=l, value=str(m), emoji="⏱️", default=(m == self.minutes))
                     for l, m in DURATIONS]
            dsel = discord.ui.Select(placeholder="3️⃣ Auto-stop after…", options=dopts, row=2)
            dsel.callback = self._dur
            self.add_item(dsel)
            go = discord.ui.Button(label="Start attendance", style=discord.ButtonStyle.success, emoji="▶️", row=3)
            go.callback = self._go
            self.add_item(go)

    def _embed(self):
        s = self.cog.s
        if not self.sec:
            return discord.Embed(title="▶️ Start attendance", description="Choose the section.", color=discord.Color.green())
        c = s.courses[self.course]
        return discord.Embed(title="▶️ Start attendance", color=discord.Color.green(),
                             description=f"**Section:** {self.sec}\n**Course:** {c['code']} {c['title']}\n"
                                         f"**Auto-stop:** {self.minutes or 'off'}{' min' if self.minutes else ''}\n\n"
                                         "Students who join any 🔊 voice channel of this section are marked present, "
                                         "and their time in class is recorded.")

    async def _sec(self, interaction):
        self.sec = interaction.data["values"][0]
        self.course = self.cog.s.default_course(self.sec)
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _course(self, interaction):
        self.course = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _dur(self, interaction):
        self.minutes = int(interaction.data["values"][0])
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _go(self, interaction):
        att = self.cog.bot.get_cog("Attendance")
        try:
            text = await att.start_session(self.sec, self.course, self.minutes, interaction.user.name)
        except commands.BadArgument as e:
            await interaction.response.edit_message(content=f"⚠️ {e}", embed=None, view=None)
            return
        await interaction.response.edit_message(content="✅ Started.", embed=None, view=None)
        await interaction.channel.send(text)


class AttStopMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        att = cog.bot.get_cog("Attendance")
        opts = []
        for sec, x in att.sessions.items():
            c = cog.s.courses[x["course"]]
            opts.append(discord.SelectOption(label=f"Section {sec} · {c['code']} {c['short']}"[:100], value=sec,
                                             description=f"started {x['started_at'][11:16]} · joined {len(x['present'])}",
                                             emoji="🔴"))
        if opts:
            sel = discord.ui.Select(placeholder="Choose session(s) to stop", options=opts[:25], min_values=1,
                                    max_values=len(opts[:25]))
            sel.callback = self._stop
            self.add_item(sel)

    async def _stop(self, interaction):
        await interaction.response.edit_message(content="⏳ Stopping and building the Excel sheet…", view=None, embed=None)
        att = self.cog.bot.get_cog("Attendance")
        for sec in interaction.data["values"]:
            try:
                summary, files = await att.stop_session(sec)
                await interaction.channel.send(summary, files=files)
            except commands.BadArgument as e:
                await interaction.channel.send(f"⚠️ {e}")
        await done(interaction)


class SectionCourseMenu(BaseMenu):
    """Pick section (+ course) then run an action. action: list | att_report"""

    def __init__(self, cog, user_id, action):
        super().__init__(cog, user_id)
        self.action = action
        self.sec = None
        sel = discord.ui.Select(placeholder="Choose section", options=section_options(cog.s))
        sel.callback = self._sec
        self.add_item(sel)

    async def _sec(self, interaction):
        self.sec = interaction.data["values"][0]
        if self.action == "list":
            await interaction.response.edit_message(content="⏳", view=None, embed=None)
            await run_command(interaction, "list", self.sec)
            await done(interaction)
            return
        courses = self.cog.s.section_courses(self.sec)
        if len(courses) == 1:
            await self._run(interaction, courses[0])
            return
        self.clear_items()
        csel = discord.ui.Select(placeholder="Choose course", options=course_options(self.cog.s, self.sec))
        csel.callback = lambda i: self._run(i, i.data["values"][0])
        self.add_item(csel)
        await interaction.response.edit_message(content=f"Section **{self.sec}** → now choose the course:", view=self)

    async def _run(self, interaction, course):
        await interaction.response.edit_message(content="⏳ Building report…", view=None, embed=None)
        att = self.cog.bot.get_cog("Attendance")
        try:
            text, files = att.build_report(self.sec, course)
            await interaction.channel.send(text, files=files)
        except commands.BadArgument as e:
            await interaction.channel.send(f"⚠️ {e}")
        await done(interaction)


# ====================================================================== students
class FindModal(discord.ui.Modal, title="🔎 Find a student"):
    query = discord.ui.TextInput(label="Student ID, name or Discord username", max_length=60)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message("🔎 Results are in the channel.", ephemeral=True)
        await run_command(interaction, "find", query=self.query.value)


class MemberMenu(BaseMenu):
    """action: verify | move | rename | unverify"""

    TITLES = {"verify": "✅ Verify a member", "move": "🔀 Move to another section",
              "rename": "✏️ Fix a student's name", "unverify": "❌ Remove a student's access"}

    def __init__(self, cog, user_id, action):
        super().__init__(cog, user_id)
        self.action = action
        self.member_id = None
        self.sec = None
        us = discord.ui.UserSelect(placeholder="Choose the member", min_values=1, max_values=1, row=0)
        us.callback = self._member
        self.add_item(us)
        if action in ("verify", "move"):
            ss = discord.ui.Select(placeholder="Choose section", options=section_options(cog.s), row=1)
            ss.callback = self._sec
            self.add_item(ss)
        label, style, emoji = {"verify": ("Enter ID & name", discord.ButtonStyle.success, "🪪"),
                               "move": ("Move", discord.ButtonStyle.primary, "🔀"),
                               "rename": ("Enter new name", discord.ButtonStyle.primary, "✏️"),
                               "unverify": ("Remove access", discord.ButtonStyle.danger, "❌")}[action]
        b = discord.ui.Button(label=label, style=style, emoji=emoji, row=2)
        b.callback = self._go
        self.add_item(b)

    async def _member(self, interaction):
        self.member_id = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _sec(self, interaction):
        self.sec = interaction.data["values"][0]
        await interaction.response.defer()

    async def _go(self, interaction):
        member = interaction.guild.get_member(self.member_id) if self.member_id else None
        if not member:
            await interaction.response.send_message("Choose a member first.", ephemeral=True)
            return
        if self.action in ("verify", "move") and not self.sec:
            await interaction.response.send_message("Choose a section first.", ephemeral=True)
            return
        if self.action == "verify":
            await interaction.response.send_modal(AdminVerifyModal(member, self.sec, interaction))
        elif self.action == "rename":
            await interaction.response.send_modal(RenameModal(member, interaction))
        elif self.action == "move":
            await interaction.response.edit_message(content="⏳", view=None, embed=None)
            await run_command(interaction, "move", member, self.sec)
            await done(interaction)
        else:
            await interaction.response.edit_message(content="⏳", view=None, embed=None)
            await run_command(interaction, "unverify", member)
            await done(interaction)


class AdminVerifyModal(discord.ui.Modal):
    def __init__(self, member, sec, menu):
        super().__init__(title=f"Verify {member.display_name} → {sec}"[:45])
        self.member, self.sec, self.menu = member, sec, menu
        self.sid = discord.ui.TextInput(label="Student ID", min_length=5, max_length=20, placeholder="11250121964")
        self.full = discord.ui.TextInput(label="Full name", max_length=60, placeholder="Rahim Uddin")
        self.add_item(self.sid)
        self.add_item(self.full)

    async def on_submit(self, interaction):
        await interaction.response.send_message("⏳ Verifying…", ephemeral=True)
        await run_command(interaction, "verify", self.member, self.sec, self.sid.value, name=self.full.value)
        await done(self.menu)


class RenameModal(discord.ui.Modal):
    def __init__(self, member, menu):
        super().__init__(title=f"New name for {member.display_name}"[:45])
        self.member, self.menu = member, menu
        self.full = discord.ui.TextInput(label="Full official name", max_length=60)
        self.add_item(self.full)

    async def on_submit(self, interaction):
        await interaction.response.send_message("⏳", ephemeral=True)
        await run_command(interaction, "rename", self.member, name=self.full.value)
        await done(self.menu)


class KickMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.days = 7
        sel = discord.ui.Select(placeholder="Unverified for at least…", options=[
            discord.SelectOption(label=f"{d} days", value=str(d), default=(d == 7)) for d in (1, 3, 7, 14, 30)])
        sel.callback = self._days
        self.add_item(sel)
        pv = discord.ui.Button(label="Show list", style=discord.ButtonStyle.secondary, emoji="📋")
        pv.callback = self._preview
        kk = discord.ui.Button(label="Kick them", style=discord.ButtonStyle.danger, emoji="🚪")
        kk.callback = self._kick
        self.add_item(pv)
        self.add_item(kk)

    async def _days(self, interaction):
        self.days = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _preview(self, interaction):
        await interaction.response.defer()
        await run_command(interaction, "kick_unverified", self.days)

    async def _kick(self, interaction):
        async def go(i):
            await run_command(i, "kick_unverified", self.days, "confirm")
            await done(i)
        await interaction.response.edit_message(
            content=f"⚠️ Kick **everyone** who joined more than **{self.days} days** ago and never verified?",
            view=ConfirmView(self.cog, interaction.user.id, go, label="Yes, kick"), embed=None)


# ====================================================================== the panel
PANEL_BUTTONS = [
    # row 0 — communication
    ("announce", "Announce", "📢", discord.ButtonStyle.primary, 0),
    ("dm", "DM students", "✉️", discord.ButtonStyle.primary, 0),
    ("scheduled", "Scheduled posts", "🗓️", discord.ButtonStyle.secondary, 0),
    ("invite", "Invite link", "🔗", discord.ButtonStyle.secondary, 0),
    ("files", "Marks / roster upload", "📎", discord.ButtonStyle.secondary, 0),
    # row 1 — attendance
    ("att_start", "Start attendance", "▶️", discord.ButtonStyle.success, 1),
    ("att_stop", "Stop attendance", "⏹️", discord.ButtonStyle.danger, 1),
    ("att_status", "Running now", "⏱️", discord.ButtonStyle.secondary, 1),
    ("att_report", "Attendance report", "📈", discord.ButtonStyle.secondary, 1),
    ("stats", "Stats", "📊", discord.ButtonStyle.secondary, 1),
    # row 2 — students
    ("list", "Student list", "📋", discord.ButtonStyle.secondary, 2),
    ("excel", "Excel", "📗", discord.ButtonStyle.success, 2),
    ("find", "Find", "🔎", discord.ButtonStyle.secondary, 2),
    ("pending", "Pending", "🟡", discord.ButtonStyle.secondary, 2),
    ("notify", "Remind unverified", "🔔", discord.ButtonStyle.secondary, 2),
    # row 3 — manage members
    ("verify", "Verify", "✅", discord.ButtonStyle.success, 3),
    ("move", "Move section", "🔀", discord.ButtonStyle.primary, 3),
    ("rename", "Rename", "✏️", discord.ButtonStyle.secondary, 3),
    ("unverify", "Unverify", "❌", discord.ButtonStyle.danger, 3),
    ("kick", "Kick unverified", "🚪", discord.ButtonStyle.danger, 3),
    # row 4 — system
    ("welcome", "Re-post welcome", "📌", discord.ButtonStyle.secondary, 4),
    ("backup", "Backup", "💾", discord.ButtonStyle.secondary, 4),
    ("status", "Status", "🖥️", discord.ButtonStyle.secondary, 4),
    ("update", "Update", "🔄", discord.ButtonStyle.primary, 4),
    ("restart", "Restart", "♻️", discord.ButtonStyle.danger, 4),
]


class PanelButton(discord.ui.Button):
    def __init__(self, key, label, emoji, style, row):
        super().__init__(label=label, emoji=emoji, style=style, row=row, custom_id=f"nubtk:panel:{key}")
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Panel").handle(interaction, self.key)


class AdminPanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for key, label, emoji, style, row in PANEL_BUTTONS:
            self.add_item(PanelButton(key, label, emoji, style, row))


# ====================================================================== cog
class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        self.jobs: dict = load_json(SCHEDULE_FILE, {})

    async def cog_load(self):
        self.bot.add_view(AdminPanel())
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()

    # ------------------------------------------------------------ panel message
    PANEL_GUIDE = [
        ("📢 Row 1 · Communication", [
            ("📢 Announce", "Pick section/course → write title + message → posts in their announcement channel. "
                           "Fill *Schedule* to post later."),
            ("✉️ DM students", "Pick section/course → write message → every student gets a private DM."),
            ("🗓️ Scheduled posts", "See / cancel announcements waiting to be posted."),
            ("🔗 Invite link", "Server invite link + ready message for Facebook / WhatsApp."),
            ("📎 Marks / roster", "Shows how to send marks by DM or upload the official list (needs a file)."),
        ]),
        ("🗓️ Row 2 · Attendance", [
            ("▶️ Start", "Section → course → auto-stop time → Start. Students join the section voice channel."),
            ("⏹️ Stop", "Stops attendance → Excel with present/absent and minutes in class."),
            ("⏱️ Running now", "Which attendance is running and how many joined."),
            ("📈 Report", "Section → course → semester Excel with every class and %."),
            ("📊 Stats", "How many students in each section and course."),
        ]),
        ("🎓 Row 3 · Students", [
            ("📋 Student list", "Section → list of all verified students."),
            ("📗 Excel", "Download the student list as Excel (all, a section or a course)."),
            ("🔎 Find", "Search by Student ID, name or Discord name."),
            ("🟡 Pending", "Requests waiting for approval (e.g. same ID used twice) with Approve / Reject."),
            ("🔔 Remind unverified", "DM + mention everyone who has not verified yet."),
        ]),
        ("👤 Row 4 · Manage members", [
            ("✅ Verify", "Pick member + section → type ID and name → verified."),
            ("🔀 Move section", "Pick member + new section → moved (roles and nickname change)."),
            ("✏️ Rename", "Pick member → type correct name → nickname fixed."),
            ("❌ Unverify", "Pick member → access removed, record deleted."),
            ("🚪 Kick unverified", "Choose days → preview list → kick people who never verified."),
        ]),
        ("⚙️ Row 5 · System", [
            ("📌 Re-post welcome", "Re-post welcome buttons, instructor cards and this panel."),
            ("💾 Backup", "Zip of all student data and attendance."),
            ("🖥️ Status", "Is the bot healthy? Uptime, Tinker Board temperature, RAM."),
            ("🔄 Update", "Get the newest code from GitHub now and restart."),
            ("♻️ Restart", "Restart the bot (back in ~15 seconds)."),
        ]),
    ]

    def panel_embed(self) -> discord.Embed:
        e = discord.Embed(
            title="🎛️ Control panel — how to use",
            description=("Click a button below. A **private menu** opens (only you see it): choose from the "
                         "dropdowns or fill the form. Results are posted in this channel.\n"
                         f"⚠️ `{self.s.prefix}setup_server` and `{self.s.prefix}reset_server` are typed commands "
                         "and always ask **Yes / Cancel** first."),
            color=discord.Color.blurple())
        for title, items in self.PANEL_GUIDE:
            e.add_field(name=title, value="\n".join(f"**{b}** — {d}" for b, d in items)[:1024], inline=False)
        e.set_footer(text=f"Type {self.s.prefix}help for text commands with examples")
        return e

    async def post_panel(self, channel: discord.TextChannel):
        """Remove old pinned bot messages, then pin the help list + the panel."""
        try:
            async for m in channel.history(limit=100):
                if m.author.id == self.bot.user.id and m.pinned:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
            admin = self.bot.get_cog("Admin")
            help_msg = await channel.send(embeds=admin.help_embeds()) if admin else None
            panel_msg = await channel.send(embed=self.panel_embed(), view=AdminPanel())
            for m in (help_msg, panel_msg):
                if m:
                    await m.pin()
            async for m in channel.history(limit=6):
                if m.type == discord.MessageType.pins_add:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
        except discord.HTTPException as e:
            log.warning("Could not post panel: %s", e)

    @commands.command(name="panel")
    async def panel_cmd(self, ctx):
        await self.post_panel(ctx.channel)

    # ------------------------------------------------------------ button dispatcher
    async def handle(self, interaction: discord.Interaction, key: str):
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only the teacher can use this panel.", ephemeral=True)
            return
        uid = interaction.user.id

        async def menu(view, text=None, embed=None):
            kw = {"view": view, "ephemeral": True}
            if text:
                kw["content"] = text
            if embed:
                kw["embed"] = embed
            await interaction.response.send_message(**kw)

        async def quick(name, *args, **kwargs):
            await interaction.response.defer(ephemeral=True, thinking=True)
            await run_command(interaction, name, *args, **kwargs)
            await interaction.followup.send("✅ Done.", ephemeral=True)

        if key == "announce":
            await menu(TargetMenu(self, uid, "announce"),
                       "📢 **Announcement** — pick one or more targets, then click a button to write it.\n"
                       "*(Need to attach a file? Use `!announce 2C your text` with the file attached.)*")
        elif key == "dm":
            await menu(TargetMenu(self, uid, "dm"), "✉️ **DM students** — pick targets, then write the message.")
        elif key == "excel":
            await menu(TargetMenu(self, uid, "excel"), "📗 **Excel** — what should the file contain?")
        elif key == "scheduled":
            text = self.jobs_text()
            await menu(ScheduledMenu(self, uid), text)
        elif key == "invite":
            await quick("invite")
        elif key == "files":
            p = self.s.prefix
            await interaction.response.send_message(
                "📎 **Uploads need a file, so use a message:**\n"
                f"• **Marks to each student by DM:** type `{p}marks_dm CT-1 Marks` and attach a CSV/Excel "
                "(first row = column names, one column = Student ID).\n"
                f"• **Official roster:** type `{p}roster_upload 2C` and attach a CSV (ID, Name).\n"
                f"• **Announcement with a file:** type `{p}announce 2C your text` and attach the file.",
                ephemeral=True)
        elif key == "att_start":
            m = AttStartMenu(self, uid)
            if not m.children:
                await interaction.response.send_message("All sections already have attendance running.", ephemeral=True)
                return
            await menu(m, embed=m._embed())
        elif key == "att_stop":
            m = AttStopMenu(self, uid)
            if not m.children:
                await interaction.response.send_message("No attendance is running.", ephemeral=True)
                return
            await menu(m, "⏹️ **Stop attendance** — choose the session(s). The Excel sheet will be posted here.")
        elif key == "att_status":
            await interaction.response.send_message(self.bot.get_cog("Attendance").status_text(), ephemeral=True)
        elif key == "att_report":
            await menu(SectionCourseMenu(self, uid, "att_report"), "📈 **Attendance report** — choose section.")
        elif key == "stats":
            await quick("stats")
        elif key == "list":
            await menu(SectionCourseMenu(self, uid, "list"), "📋 **Student list** — choose section.")
        elif key == "find":
            await interaction.response.send_modal(FindModal())
        elif key == "pending":
            await quick("pending")
        elif key == "notify":
            async def go(i):
                await run_command(i, "notify_unverified")
                await done(i)
            await menu(ConfirmView(self, uid, go, label="Send reminders", danger=False),
                       "🔔 DM + mention **every unverified member**?")
        elif key in ("verify", "move", "rename", "unverify"):
            await menu(MemberMenu(self, uid, key), MemberMenu.TITLES[key])
        elif key == "kick":
            await menu(KickMenu(self, uid), "🚪 **Kick unverified members** — choose days, preview, then kick.")
        elif key == "welcome":
            await quick("setup_welcome")
        elif key == "backup":
            await quick("backup")
        elif key == "status":
            await quick("status")
        elif key == "update":
            async def go(i):
                await run_command(i, "update")
            await menu(ConfirmView(self, uid, go, label="Update now", danger=False),
                       "🔄 Pull the latest code from GitHub and restart?")
        elif key == "restart":
            async def go(i):
                await run_command(i, "restart")
            await menu(ConfirmView(self, uid, go, label="Restart"), "♻️ Restart the bot? (back in ~15 s)")

    # ------------------------------------------------------------ announcements with custom title
    async def announce_with_title(self, interaction, target, text, ping, title):
        admin = self.bot.get_cog("Admin")
        ctx = PanelCtx(interaction)
        try:
            await admin._announce(ctx, target, text, ping, title=title)
        except commands.BadArgument as e:
            await ctx.send(f"⚠️ {e}")

    # ------------------------------------------------------------ scheduled announcements
    def parse_when(self, raw: str):
        now = self.s.now()
        raw = raw.strip().lower()
        try:
            if raw.startswith("+"):
                n, unit = int(raw[1:-1]), raw[-1]
                return now + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n) if unit == "m"
                              else timedelta(days=n) if unit == "d" else None)
            for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=self.s.tz)
                except ValueError:
                    pass
            t = datetime.strptime(raw.replace(".", ":"), "%H:%M").time()
            when = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return when if when > now else when + timedelta(days=1)
        except (ValueError, TypeError):
            return None

    def add_schedule(self, targets, text, title, ping, when, by):
        jid = secrets.token_hex(3)
        self.jobs[jid] = {"targets": targets, "text": text, "title": title, "ping": ping,
                          "when": when.isoformat(timespec="minutes"), "by": by}
        self.save_jobs()
        return jid

    def save_jobs(self):
        save_json(SCHEDULE_FILE, self.jobs)

    def jobs_text(self):
        if not self.jobs:
            return "🗓️ No scheduled announcements. (Create one with 📢 Announce → fill the *Schedule* box.)"
        lines = ["🗓️ **Scheduled announcements** — select to cancel:"]
        for jid, j in sorted(self.jobs.items(), key=lambda x: x[1]["when"]):
            lines.append(f"• `{jid}` **{datetime.fromisoformat(j['when']):%a %d %b %H:%M}** → {', '.join(j['targets'])}"
                         f" — {(j['text'] or '')[:60]}")
        return "\n".join(lines)

    @commands.command(name="scheduled")
    async def scheduled_cmd(self, ctx):
        await ctx.send(self.jobs_text())

    @tasks.loop(seconds=30)
    async def scheduler(self):
        now = self.s.now()
        admin = self.bot.get_cog("Admin")
        log_ch = self.bot.channel_for("admin_channel")
        for jid, j in list(self.jobs.items()):
            if datetime.fromisoformat(j["when"]) > now:
                continue
            self.jobs.pop(jid, None)
            self.save_jobs()
            if not admin or not log_ch:
                continue

            class _Ctx:
                guild = log_ch.guild
                author = log_ch.guild.me
                channel = log_ch
                message = type("Msg", (), {"attachments": []})()

                @staticmethod
                async def send(content=None, **kw):
                    return await log_ch.send(content, **kw)

            for t in j["targets"]:
                try:
                    await admin._announce(_Ctx, t, j["text"], j["ping"], title=j.get("title") or "📢 Announcement")
                except Exception as e:  # noqa: BLE001
                    await log_ch.send(f"⚠️ Scheduled post `{jid}` → {t} failed: {e}")

    @scheduler.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Panel(bot))
