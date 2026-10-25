"""🎛️ Admin control panel — buttons, dropdowns and forms for (almost) every admin job.

A persistent panel message is pinned in 🛠️┃admin. Every button opens a small private
(ephemeral) menu: pick section / course / time / student from dropdowns, fill a form, done.
Results (files, summaries) are posted in the admin channel. Text commands still work too.

Also here: scheduled announcements (post later at a chosen time).
"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

import discord
from discord import app_commands
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


class ReminderModal(discord.ui.Modal):
    def __init__(self, cog, link: str = ""):
        super().__init__(title="⏰ Reminder for a message", timeout=900)
        self.cog = cog
        self.link = discord.ui.TextInput(label="Message link (or message ID)", default=link or None,
                                         placeholder="Right-click the message → Copy Message Link", max_length=200)
        self.when = discord.ui.TextInput(label="When? (empty = now · several: use commas)", required=False,
                                         placeholder="2026-10-28 20:00, 2026-10-29 08:00   or   +1d   or   21:30",
                                         max_length=120)
        self.note = discord.ui.TextInput(label="Reminder note (optional)", required=False,
                                         style=discord.TextStyle.paragraph, max_length=1000,
                                         placeholder="e.g. Assignment 2 deadline is tomorrow 11:59 PM!")
        self.target = discord.ui.TextInput(label="Where? (empty = same as the message)",
                                           required=False, max_length=40, placeholder="3D  ·  3D:lab  ·  CSE2109  ·  all")
        for x in (self.link, self.when, self.note, self.target):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            msg = await self.cog.resolve_message(interaction.guild, self.link.value)
        except commands.BadArgument as e:
            await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            return
        target = (self.target.value or "").strip() or self.cog.reminder_target(msg.channel)
        kind, _ = self.cog.bot.get_cog("Admin").resolve_target(target)
        if not kind:
            await interaction.followup.send(f"⚠️ `{target}` is not a valid target (all / 3D / 3D:lab / CSE2109).",
                                            ephemeral=True)
            return
        times = [t.strip() for t in (self.when.value or "").split(",") if t.strip()]
        if not times:
            await self.cog.post_reminder(target, msg.jump_url, self.cog.snapshot(msg), self.note.value)
            await interaction.followup.send(f"⏰ Reminder posted → **{target}**.", ephemeral=True)
            return
        done_at = []
        for t in times:
            when = self.cog.parse_when(t)
            if not when:
                await interaction.followup.send(f"⚠️ Could not read the time `{t}`.", ephemeral=True)
                return
            self.cog.add_schedule([target], self.note.value, "⏰ Reminder", True, when, interaction.user.name,
                                  kind="reminder", link=msg.jump_url, snapshot=self.cog.snapshot(msg))
            done_at.append(f"{when:%a %d %b, %I:%M %p}")
        await interaction.followup.send(f"🗓️ Reminder scheduled → **{target}** at: " + " · ".join(done_at),
                                        ephemeral=True)


class EventMenu(BaseMenu):
    """📌 Add a deadline / test: choose type + sections, then fill the form."""

    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        r = cog.bot.get_cog("Routine")
        self.kind = "assignment"
        self.secs = []
        ks = discord.ui.Select(placeholder="1️⃣ What is it?", row=0, options=[
            discord.SelectOption(label=name, value=k, emoji=icon, default=(k == "assignment"))
            for k, (name, icon) in r.EVENT_TYPES.items()])
        ks.callback = self._kind
        self.add_item(ks)
        opts = [discord.SelectOption(label="All my sections", value="all", emoji="🏫")] + section_options(cog.s)
        ss = discord.ui.Select(placeholder="2️⃣ Which section(s)?", row=1, options=opts[:25], min_values=1,
                               max_values=len(opts[:25]))
        ss.callback = self._secs
        self.add_item(ss)
        b = discord.ui.Button(label="3️⃣ Enter title & date", style=discord.ButtonStyle.success, emoji="✏️", row=2)
        b.callback = self._go
        self.add_item(b)

    async def _kind(self, interaction):
        self.kind = interaction.data["values"][0]
        await interaction.response.defer()

    async def _secs(self, interaction):
        self.secs = list(interaction.data["values"])
        await interaction.response.defer()

    async def _go(self, interaction):
        if not self.secs:
            await interaction.response.send_message("Choose the section(s) first.", ephemeral=True)
            return
        await interaction.response.send_modal(EventModal(self.cog, self.kind, self.secs, interaction))


class EventModal(discord.ui.Modal):
    def __init__(self, cog, kind, secs, menu):
        name = cog.bot.get_cog("Routine").EVENT_TYPES[kind][0]
        super().__init__(title=f"📌 New {name}"[:45], timeout=900)
        self.cog, self.kind, self.secs, self.menu = cog, kind, secs, menu
        self.head = discord.ui.TextInput(label="Title", max_length=100, placeholder="e.g. Assignment 2 — Inheritance")
        self.when = discord.ui.TextInput(label="Date & time (deadline / test time)", max_length=30,
                                         placeholder="2026-10-30 23:59   or   2026-10-30 (= 11:59 PM)")
        self.details = discord.ui.TextInput(label="Details (optional)", required=False, max_length=600,
                                            style=discord.TextStyle.paragraph,
                                            placeholder="Syllabus: Chapter 3–4 · Submit PDF in general-text")
        self.announce = discord.ui.TextInput(label="Announce now in the section? (yes / no)", default="yes",
                                             max_length=3, required=False)
        for x in (self.head, self.when, self.details, self.announce):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.when.value.strip()
        when = self.cog.parse_when(raw) or self.cog.parse_when(raw + " 23:59")
        if not when:
            await interaction.response.send_message("⚠️ Could not read the date. Use `2026-10-30 23:59`.", ephemeral=True)
            return
        r = self.cog.bot.get_cog("Routine")
        eid = r.add_event(self.kind, self.head.value.strip(), self.secs, when, (self.details.value or "").strip(),
                          interaction.user.name)
        posted = []
        if (self.announce.value or "yes").strip().lower().startswith("y"):
            posted = await r.announce_event(eid)
        await interaction.response.send_message(
            f"✅ Saved: {r.event_line(when, r._events()[eid], True)}\n"
            + (f"📢 Announced in: {', '.join(posted)}\n" if posted else "")
            + "Students see it under 📅 **My section today** in 🗓️┃routine (on the day and 7 days before).",
            ephemeral=True)
        await done(self.menu, "✅ Deadline / test saved.")


class EventsListMenu(BaseMenu):
    def __init__(self, cog, user_id, items):
        super().__init__(cog, user_id)
        if items:
            r = cog.bot.get_cog("Routine")
            opts = [discord.SelectOption(
                label=f"{w:%d %b %H:%M} · {r.EVENT_TYPES.get(ev['kind'], ('', ''))[0]}: {ev['title']}"[:100],
                value=eid, description=", ".join(ev["sections"])[:100]) for w, eid, ev in items[:25]]
            sel = discord.ui.Select(placeholder="Select to delete…", options=opts, min_values=1, max_values=len(opts))
            sel.callback = self._delete
            self.add_item(sel)

    async def _delete(self, interaction):
        r = self.cog.bot.get_cog("Routine")
        for eid in interaction.data["values"]:
            r._events().pop(eid, None)
        r.save_events()
        await interaction.response.edit_message(content="🗑️ Deleted.", view=None, embed=None)


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
              "rename": "✏️ Fix a student's name", "unverify": "❌ Remove a student's access",
              "extra_add": "➕ Give an EXTRA section (student stays in the main one too)",
              "extra_remove": "➖ Remove an extra section"}

    def __init__(self, cog, user_id, action):
        super().__init__(cog, user_id)
        self.action = action
        self.member_id = None
        self.sec = None
        us = discord.ui.UserSelect(placeholder="Choose the member", min_values=1, max_values=1, row=0)
        us.callback = self._member
        self.add_item(us)
        if action in ("extra_add", "extra_remove"):
            opts = section_options(cog.s)
            ss = discord.ui.Select(placeholder="Choose section(s)", options=opts, row=1, min_values=1,
                                   max_values=len(opts))
            ss.callback = self._secs
            self.add_item(ss)
        if action in ("verify", "move"):
            ss = discord.ui.Select(placeholder="Choose section", options=section_options(cog.s), row=1)
            ss.callback = self._sec
            self.add_item(ss)
        label, style, emoji = {"verify": ("Enter ID & name", discord.ButtonStyle.success, "🪪"),
                               "move": ("Move", discord.ButtonStyle.primary, "🔀"),
                               "rename": ("Enter new name", discord.ButtonStyle.primary, "✏️"),
                               "unverify": ("Remove access", discord.ButtonStyle.danger, "❌"),
                               "extra_add": ("Add", discord.ButtonStyle.success, "➕"),
                               "extra_remove": ("Remove", discord.ButtonStyle.secondary, "➖")}[action]
        b = discord.ui.Button(label=label, style=style, emoji=emoji, row=2)
        b.callback = self._go
        self.add_item(b)

    async def _member(self, interaction):
        self.member_id = int(interaction.data["values"][0])
        await interaction.response.defer()

    async def _sec(self, interaction):
        self.sec = interaction.data["values"][0]
        await interaction.response.defer()

    async def _secs(self, interaction):
        self.secs = list(interaction.data["values"])
        self.sec = self.secs[0]
        await interaction.response.defer()

    async def _go(self, interaction):
        member = interaction.guild.get_member(self.member_id) if self.member_id else None
        if not member:
            await interaction.response.send_message("Choose a member first.", ephemeral=True)
            return
        if self.action in ("extra_add", "extra_remove"):
            if not getattr(self, "secs", None):
                await interaction.response.send_message("Choose section(s) first.", ephemeral=True)
                return
            await interaction.response.edit_message(content="⏳", view=None, embed=None)
            await run_command(interaction, "add_section" if self.action == "extra_add" else "remove_section",
                              member, *self.secs)
            await done(interaction)
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
B = discord.ButtonStyle
# Each group = one message in the admin channel: title, short text per button, then the buttons.
PANEL_GROUPS = [
    ("Row 1 · 📢 Communication", discord.Color.blurple(), [
        ("announce", "Announce", "📢", B.primary, "Pick section/course → write → posts in their announcement channel (can be scheduled)."),
        ("dm", "DM students", "✉️", B.primary, "Pick section/course → write → every student gets a private DM."),
        ("scheduled", "Scheduled posts", "🗓️", B.secondary, "See or cancel announcements waiting to be posted."),
        ("invite", "Invite link", "🔗", B.secondary, "Invite link + ready message for Facebook / WhatsApp."),
        ("files", "Marks / roster", "📎", B.secondary, "How to send marks by DM or upload the official list (needs a file)."),
    ]),
    ("Row 2 · 🗓️ Attendance", discord.Color.green(), [
        ("att_start", "Start", "▶️", B.success, "Section → course → auto-stop time → Start."),
        ("att_stop", "Stop", "⏹️", B.danger, "Stop → Excel with present/absent and minutes in class."),
        ("att_status", "Running now", "⏱️", B.secondary, "Which attendance is running, how many joined."),
        ("att_report", "Report", "📈", B.secondary, "Section → course → semester Excel with %."),
        ("stats", "Stats", "📊", B.secondary, "Students per section and course."),
    ]),
    ("Row 3 · 🎓 Students", discord.Color.gold(), [
        ("list", "Student list", "📋", B.secondary, "Section → list of verified students."),
        ("excel", "Excel", "📗", B.success, "Student list as Excel (all / section / course)."),
        ("find", "Find", "🔎", B.secondary, "Search by ID, name or Discord name."),
        ("pending", "Pending", "🟡", B.secondary, "Requests waiting for Approve / Reject."),
        ("notify", "Remind unverified", "🔔", B.secondary, "DM + mention everyone not verified yet."),
    ]),
    ("Row 4 · 👤 Manage members", discord.Color.orange(), [
        ("verify", "Verify", "✅", B.success, "Member + section → ID and name → verified."),
        ("move", "Move section", "🔀", B.primary, "Member + new section → moved."),
        ("rename", "Rename", "✏️", B.secondary, "Member → correct name → nickname fixed."),
        ("unverify", "Unverify", "❌", B.danger, "Member → access and record removed."),
        ("kick", "Kick unverified", "🚪", B.danger, "Days → preview → kick people who never verified."),
        ("extra_add", "Add extra section", "➕", B.success, "Student in two sections (e.g. 3D + 4B): member → extra section(s)."),
        ("extra_remove", "Remove extra section", "➖", B.secondary, "Take away an extra section."),
    ]),
    ("Row 5 · ⚙️ System", discord.Color.dark_grey(), [
        ("welcome", "Re-post welcome", "📌", B.secondary, "Re-post welcome buttons, instructor cards and this panel."),
        ("backup", "Backup", "💾", B.secondary, "Zip of all data."),
        ("status", "Status", "🖥️", B.secondary, "Uptime, Tinker Board temperature, RAM."),
        ("update", "Update", "🔄", B.primary, "Get newest code from GitHub and restart."),
        ("restart", "Restart", "♻️", B.danger, "Restart the bot (~15 s)."),
    ]),
    ("Row 6 · 🗓️ Routine & reminders", discord.Color.purple(), [
        ("routine_check", "Update routine", "🔄", B.success, "Checks the online routine sheet now → updates every section's pinned 🗓️┃routine and posts what changed."),
        ("routine_show", "Show routine", "🗓️", B.secondary, "Show the saved routine of every section here."),
        ("today", "My day", "📅", B.success, "Today & tomorrow (with date): classes, sections, rooms, deadlines and tests."),
        ("week", "My week", "📆", B.secondary, "Your whole weekly routine (all sections)."),
        ("reminder", "Remind a message", "⏰", B.primary, "Paste a message link → re-announce it now or later (deadline, CT…). Tip: right-click a message → Apps → ⏰ Set reminder."),
    ]),
    ("Row 7 · 📌 Deadlines & tests", discord.Color.orange(), [
        ("event_add", "Add deadline / test", "📌", B.success, "Assignment, CT, quiz, lab report, presentation… → students see it in 📅 My section today."),
        ("event_list", "Upcoming deadlines", "📋", B.secondary, "See all upcoming deadlines and tests, delete wrong ones."),
    ]),
    ("Row 8 · 📖 Help", discord.Color.teal(), [
        ("allcmds", "All commands", "📖", B.primary, "Every text command with an example."),
        ("panel", "Show panel here", "🎛️", B.secondary, "Open this whole panel privately, anywhere."),
    ]),
]


class PanelButton(discord.ui.Button):
    def __init__(self, key, label, emoji, style, row=None):
        super().__init__(label=label, emoji=emoji, style=style, row=row, custom_id=f"nubtk:panel:{key}")
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        await interaction.client.get_cog("Panel").handle(interaction, self.key)


class GroupView(discord.ui.View):
    """Buttons of one panel row (persistent)."""

    def __init__(self, index: int):
        super().__init__(timeout=None)
        for key, label, emoji, style, _ in PANEL_GROUPS[index][2]:
            self.add_item(PanelButton(key, label, emoji, style))


class QuickView(discord.ui.View):
    """Small buttons added under every bot message in the admin channels (persistent)."""

    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelButton("panel", "Panel", "🎛️", B.secondary))
        self.add_item(PanelButton("allcmds", "All commands", "📖", B.secondary))


class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        self.jobs: dict = load_json(SCHEDULE_FILE, {})
        self.ctx_menu = app_commands.ContextMenu(name="⏰ Set reminder", callback=self.ctx_set_reminder)
        self.ctx_menu.default_permissions = discord.Permissions(administrator=True)
        self.bot.tree.add_command(self.ctx_menu)

    async def cog_load(self):
        for i in range(len(PANEL_GROUPS)):
            self.bot.add_view(GroupView(i))
        self.bot.add_view(QuickView())
        self.scheduler.start()

    async def cog_unload(self):
        self.scheduler.cancel()
        self.bot.tree.remove_command(self.ctx_menu.name, type=self.ctx_menu.type)

    # ------------------------------------------------------------ panel message
    def header_embed(self) -> discord.Embed:
        return discord.Embed(
            title="🎛️ Control panel",
            description=("Each row below says what its buttons do — the buttons are right under it.\n"
                         "Click a button → a **private menu** opens (only you see it) → choose from the dropdowns "
                         "or fill the form → the result is posted here.\n\n"
                         "Every bot message here ends with **🎛️ Panel** and **📖 All commands** buttons, "
                         "so you never have to scroll up.\n"
                         f"⚠️ `{self.s.prefix}setup_server` and `{self.s.prefix}reset_server` are typed commands "
                         "and always ask **Yes / Cancel** first."),
            color=discord.Color.blurple())

    def group_embed(self, index: int) -> discord.Embed:
        title, color, items = PANEL_GROUPS[index]
        body = "\n".join(f"{emoji} **{label}** — {desc}" for _, label, emoji, _, desc in items)
        return discord.Embed(title=title, description=body, color=color)

    async def post_panel(self, channel: discord.TextChannel):
        """Delete the old panel messages and post: header + one message per row (text + its buttons)."""
        old = [x for x in str(self.bot.ids.data.get("panel_msgs", "")).split(",") if x.isdigit()]
        for mid in old:
            try:
                await (await channel.fetch_message(int(mid))).delete()
            except discord.HTTPException:
                pass
        try:
            async for m in channel.history(limit=100):  # also clean old-style pinned panel/help
                if m.author.id == self.bot.user.id and m.pinned:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
            ids = []
            head = await channel.send(embed=self.header_embed())
            ids.append(head.id)
            for i in range(len(PANEL_GROUPS)):
                msg = await channel.send(embed=self.group_embed(i), view=GroupView(i))
                ids.append(msg.id)
            await head.pin()
            async for m in channel.history(limit=10):
                if m.type == discord.MessageType.pins_add:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
            self.bot.ids.data["panel_msgs"] = ",".join(str(x) for x in ids)
            self.bot.ids.save()
        except discord.HTTPException as e:
            log.warning("Could not post panel: %s", e)

    async def send_panel_private(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=self.header_embed(), ephemeral=True)
        for i in range(len(PANEL_GROUPS)):
            await interaction.followup.send(embed=self.group_embed(i), view=GroupView(i), ephemeral=True)

    @commands.command(name="panel")
    async def panel_cmd(self, ctx):
        await self.post_panel(ctx.channel)

    # ------------------------------------------------------------ "🎛️ Panel" under every bot message
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.id != self.bot.user.id or message.components or message.flags.ephemeral:
            return
        ids = {c.id for c in (self.bot.channel_for("admin_channel"), self.bot.channel_for("log_channel")) if c}
        if message.channel.id not in ids:
            return
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return
        panel_ids = str(self.bot.ids.data.get("panel_msgs", ""))
        if str(message.id) in panel_ids or (message.embeds and (message.embeds[0].title or "").startswith("🎛️")):
            return
        try:
            await message.edit(view=QuickView())
        except discord.HTTPException:
            pass

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

        if key == "panel":
            await self.send_panel_private(interaction)
            return
        if key == "allcmds":
            admin = self.bot.get_cog("Admin")
            await interaction.response.send_message(embeds=admin.help_embeds(), ephemeral=True)
            return
        if key == "routine_check":
            await interaction.response.defer(ephemeral=True, thinking=True)
            try:
                report, _ = await self.bot.get_cog("Routine").check()
                adm = self.bot.channel_for("admin_channel")
                if adm:
                    await adm.send(report[:1990])
                await interaction.followup.send(report[:1990], ephemeral=True)
            except commands.BadArgument as e:
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
            except Exception as e:  # noqa: BLE001
                log.exception("routine check failed")
                await interaction.followup.send(f"❌ Routine check failed: `{str(e)[:300]}`", ephemeral=True)
            return
        if key == "event_add":
            await menu(EventMenu(self, uid), "📌 **Add a deadline / test** — choose type and section(s), then enter title & date.")
            return
        if key == "event_list":
            from datetime import timedelta
            r = self.bot.get_cog("Routine")
            now = self.s.now()
            items = r.events_between(None, now - timedelta(hours=1), now + timedelta(days=60))
            text = ("📌 **Upcoming deadlines & tests**\n" + "\n".join(r.event_line(w, ev, True) for w, _, ev in items)
                    if items else "No upcoming deadlines or tests.")
            await menu(EventsListMenu(self, uid, items), text[:1990])
            return
        if key in ("today", "week"):
            await interaction.response.defer(ephemeral=True, thinking=True)
            r = self.bot.get_cog("Routine")
            try:
                await r._ensure_state()
            except Exception as e:  # noqa: BLE001
                await interaction.followup.send(f"⚠️ {e}", ephemeral=True)
                return
            emb = r.day_embed(r._today(), "Today") if key == "today" else r.week_embed()
            await interaction.followup.send(embed=emb, ephemeral=True)
            if key == "today":
                await interaction.followup.send(embed=r.day_embed(r._today(1), "Tomorrow"), ephemeral=True)
            return
        if key == "routine_show":
            await quick("routine")
            return
        if key == "reminder":
            await interaction.response.send_modal(ReminderModal(self))
            return
        if key == "announce":
            await menu(TargetMenu(self, uid, "announce"),
                       "📢 **Announcement** — pick one or more targets, then click a button to write it.\n"
                       "*(Need to attach a file? Use `!announce 3D your text` with the file attached.)*")
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
                f"• **Official roster:** type `{p}roster_upload 3D` and attach a CSV (ID, Name).\n"
                f"• **Announcement with a file:** type `{p}announce 3D your text` and attach the file.",
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
        elif key in ("verify", "move", "rename", "unverify", "extra_add", "extra_remove"):
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

    # ------------------------------------------------------------ reminders
    async def resolve_message(self, guild, raw: str) -> discord.Message:
        raw = (raw or "").strip()
        m = re.search(r"channels/(\d+)/(\d+)/(\d+)", raw)
        if m:
            ch = guild.get_channel(int(m.group(2))) or guild.get_thread(int(m.group(2)))
            if ch is None:
                raise commands.BadArgument("That channel is not in this server.")
            try:
                return await ch.fetch_message(int(m.group(3)))
            except discord.HTTPException:
                raise commands.BadArgument("Message not found (deleted?).")
        if raw.isdigit():
            for ch in guild.text_channels:
                try:
                    return await ch.fetch_message(int(raw))
                except discord.HTTPException:
                    continue
        raise commands.BadArgument("Paste the message link: right-click the message → **Copy Message Link**.")

    def reminder_target(self, channel) -> str:
        cat = getattr(channel, "category_id", None)
        for sec in self.s.sections:
            c = self.bot.channel_for(f"sec:{sec}:category")
            if c and c.id == cat:
                lab = self.bot.channel_for(f"sec:{sec}:lab")
                return f"{sec}:lab" if lab and lab.id == channel.id else sec
        for theory in self.s.course_groups():
            c = self.bot.channel_for(f"group:{theory}:category")
            if c and c.id == cat:
                return theory
        return "all"

    @staticmethod
    def snapshot(msg: discord.Message) -> str:
        text = msg.content or ""
        if not text and msg.embeds:
            e = msg.embeds[0]
            text = "\n".join(x for x in (e.title, e.description) if x)
        if msg.attachments:
            text += "\n📎 " + ", ".join(a.filename for a in msg.attachments[:5])
        return text.strip()[:1500]

    async def post_reminder(self, target: str, link: str, snapshot: str, note: str = ""):
        admin = self.bot.get_cog("Admin")
        kind, val = admin.resolve_target(target)
        ch, roles, _, label = admin.target_info(kind, val)
        if ch is None:
            raise commands.BadArgument("Announcement channel not found.")
        quoted = "\n".join(f"> {line}" for line in (snapshot or "(no text)").splitlines()[:15])
        e = discord.Embed(title="⏰ Reminder", color=discord.Color.orange(), timestamp=self.s.now(),
                          description=((f"**{note}**\n\n" if note else "") + quoted)[:4000])
        e.set_footer(text=f"{self.s.teacher} · {label}")
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="Open the original message", emoji="🔗", style=discord.ButtonStyle.link,
                                     url=link))
        content = " ".join(r.mention for r in roles if r) or None
        await ch.send(content=content, embed=e, view=v)

    async def ctx_set_reminder(self, interaction: discord.Interaction, message: discord.Message):
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only the teacher can set reminders.", ephemeral=True)
            return
        await interaction.response.send_modal(ReminderModal(self, message.jump_url))

    @commands.Cog.listener()
    async def on_ready(self):
        if getattr(self, "_synced", False):
            return
        self._synced = True
        g = self.bot.main_guild()
        if not g:
            return
        try:
            self.bot.tree.copy_global_to(guild=g)
            await self.bot.tree.sync(guild=g)
        except discord.HTTPException as e:
            log.warning("Could not register the right-click menu (re-invite the bot with "
                        "'applications.commands' scope): %s", e)

    # ------------------------------------------------------------ scheduled announcements
    def parse_when(self, raw: str):
        now = self.s.now()
        raw = raw.strip().lower()
        try:
            if raw.startswith("+"):
                n, unit = int(raw[1:-1]), raw[-1]
                return now + (timedelta(hours=n) if unit == "h" else timedelta(minutes=n) if unit == "m"
                              else timedelta(days=n) if unit == "d" else None)
            for fmt in ("%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M", "%Y-%m-%d %I:%M %p", "%d-%m-%Y %I:%M %p"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=self.s.tz)
                except ValueError:
                    pass
            t = datetime.strptime(raw.replace(".", ":"), "%H:%M").time()
            when = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
            return when if when > now else when + timedelta(days=1)
        except (ValueError, TypeError):
            return None

    def add_schedule(self, targets, text, title, ping, when, by, kind="announce", link="", snapshot=""):
        jid = secrets.token_hex(3)
        self.jobs[jid] = {"targets": targets, "text": text, "title": title, "ping": ping,
                          "when": when.isoformat(timespec="minutes"), "by": by,
                          "kind": kind, "link": link, "snapshot": snapshot}
        self.save_jobs()
        return jid

    def save_jobs(self):
        save_json(SCHEDULE_FILE, self.jobs)

    def jobs_text(self):
        if not self.jobs:
            return "🗓️ No scheduled announcements. (Create one with 📢 Announce → fill the *Schedule* box.)"
        lines = ["🗓️ **Scheduled announcements** — select to cancel:"]
        for jid, j in sorted(self.jobs.items(), key=lambda x: x[1]["when"]):
            icon = "⏰" if j.get("kind") == "reminder" else "📢"
            preview = (j.get("text") or j.get("snapshot") or "")[:60].replace("\n", " ")
            lines.append(f"• {icon} `{jid}` **{datetime.fromisoformat(j['when']):%a %d %b %H:%M}** → "
                         f"{', '.join(j['targets'])} — {preview}")
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
                    if j.get("kind") == "reminder":
                        snap = j.get("snapshot", "")
                        try:  # use the newest text if the message was edited
                            snap = self.snapshot(await self.resolve_message(log_ch.guild, j["link"]))
                        except commands.BadArgument:
                            pass
                        await self.post_reminder(t, j["link"], snap, j.get("text", ""))
                        continue
                    await admin._announce(_Ctx, t, j["text"], j["ping"], title=j.get("title") or "📢 Announcement")
                except Exception as e:  # noqa: BLE001
                    await log_ch.send(f"⚠️ Scheduled post `{jid}` → {t} failed: {e}")

    @scheduler.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(Panel(bot))
