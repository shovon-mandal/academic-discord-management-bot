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


def voice_options(bot, secs=None, current=None):
    """Voice / stage channels as a list with their category, sections' own channels first."""
    g = bot.main_guild()
    if not g:
        return []
    mine = []
    for sec in (secs or []):
        ch = bot.channel_for(f"sec:{sec}:voice_class")
        if ch:
            mine.append(ch)
    others = [c for c in list(g.voice_channels) + list(g.stage_channels) if c not in mine]
    out = []
    for c in (mine + others)[:25]:
        out.append(discord.SelectOption(label=f"{c.name} · {c.category.name if c.category else '—'}"[:100],
                                        value=str(c.id), emoji="🔊", default=(current == c.id)))
    return out


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


class CleanWelcomeMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id, 300)
        a = discord.ui.Button(label="Verified / left only", emoji="✅", style=discord.ButtonStyle.secondary)
        b = discord.ui.Button(label="Everything", emoji="🧹", style=discord.ButtonStyle.danger)
        a.callback = lambda i: self._go(i, "")
        b.callback = lambda i: self._go(i, "all")
        self.add_item(a)
        self.add_item(b)

    async def _go(self, interaction, mode):
        async def run(i):
            await run_command(i, "clean_welcome", mode)
            await done(i)
        if mode == "all":  # deletes messages of students who are still waiting → safety lock
            await interaction.response.send_modal(TypedConfirmModal("CLEAN", run, self))
            return
        self.stop()
        await interaction.response.edit_message(content="⏳ Cleaning...", view=None)
        await run(interaction)


class TypedConfirmModal(discord.ui.Modal):
    """Safety lock: the teacher must TYPE a word before something big/irreversible runs."""

    def __init__(self, word, on_confirm, view=None):
        super().__init__(title="⚠️ Are you sure?", timeout=300)
        self.word, self.on_confirm, self.view_ = word, on_confirm, view
        self.inp = discord.ui.TextInput(label=f"Type {word} to continue", max_length=30, placeholder=word)
        self.add_item(self.inp)

    async def on_submit(self, interaction: discord.Interaction):
        if self.inp.value.strip().upper() != self.word.upper():
            await interaction.response.send_message(f"❎ Not confirmed (you had to type `{self.word}`). Nothing changed.",
                                                    ephemeral=True)
            return
        if self.view_ is not None:
            self.view_.stop()
        try:
            await interaction.response.edit_message(content="⏳ Working...", view=None)
        except discord.HTTPException:
            await interaction.response.defer(ephemeral=True)
        await self.on_confirm(interaction)


class ConfirmView(BaseMenu):
    def __init__(self, cog, user_id, on_confirm, label="Yes, continue", danger=True, timeout=120, typed=None):
        super().__init__(cog, user_id, timeout)
        self.on_confirm = on_confirm
        self.typed = typed
        yes = discord.ui.Button(label=label, style=discord.ButtonStyle.danger if danger else discord.ButtonStyle.success,
                                emoji="⚠️" if danger else "✅")
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def _yes(self, interaction: discord.Interaction):
        if self.typed:  # safety lock for sensitive actions
            await interaction.response.send_modal(TypedConfirmModal(self.typed, self.on_confirm, self))
            return
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
            staged = await self.cog.stage_job(jid, interaction.user)
            await interaction.response.send_message(
                f"🗓️ Scheduled for **{when:%a %d %b %Y, %H:%M}** → {', '.join(self.targets)} (id `{jid}`)."
                + ("\n👀 Preview in the bot-test channel — click **✅ Approve** there, otherwise it is NOT posted."
                   if staged else ""), ephemeral=True)
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
            pid = await self.cog.stage_msg_reminder(target, msg.jump_url, self.cog.snapshot(msg), self.note.value,
                                                    interaction.user)
            if pid:
                await self.cog.bot.get_cog("Poster").notify_staged(interaction, pid)
                return
            await self.cog.post_reminder(target, msg.jump_url, self.cog.snapshot(msg), self.note.value)
            await interaction.followup.send(f"⏰ Reminder posted → **{target}**.", ephemeral=True)
            return
        done_at = []
        for t in times:
            when = self.cog.parse_when(t)
            if not when:
                await interaction.followup.send(f"⚠️ Could not read the time `{t}`.", ephemeral=True)
                return
            jid = self.cog.add_schedule([target], self.note.value, "⏰ Reminder", True, when, interaction.user.name,
                                        kind="reminder", link=msg.jump_url, snapshot=self.cog.snapshot(msg))
            await self.cog.stage_job(jid, interaction.user)
            done_at.append(f"{when:%a %d %b, %I:%M %p}")
        await interaction.followup.send(f"🗓️ Reminder scheduled → **{target}** at: " + " · ".join(done_at)
                                        + "\n👀 Approve each one in the bot-test channel, otherwise it is NOT posted.",
                                        ephemeral=True)


class EventMenu(BaseMenu):
    """📌 Add a deadline / test: type → sections → pick a class date (suggested from the routine) → form."""

    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.kind = "assignment"
        self.secs = []
        self.date = None
        self.where = None
        self.target = "announcement"
        self._build()

    def _build(self):
        self.clear_items()
        r = self.cog.bot.get_cog("Routine")
        ks = discord.ui.Select(placeholder="1️⃣ What is it?", row=0, options=[
            discord.SelectOption(label=name, value=k, emoji=icon, default=(k == self.kind))
            for k, (name, icon) in r.EVENT_TYPES.items()])
        ks.callback = self._kind
        self.add_item(ks)
        opts = [discord.SelectOption(label="All my sections", value="all", emoji="🏫", default="all" in self.secs)]
        for o in section_options(self.cog.s):
            o.default = o.value in self.secs
            opts.append(o)
        ss = discord.ui.Select(placeholder="2️⃣ Which section(s)?", row=1, options=opts[:25], min_values=1,
                               max_values=len(opts[:25]))
        ss.callback = self._secs
        self.add_item(ss)
        sug = r.class_suggestions(self.secs) if self.secs else []
        if sug:
            ds = discord.ui.Select(placeholder="3️⃣ 📅 Pick a date (🏫 = you have class with them)", row=2, options=[
                discord.SelectOption(label=label[:100], value=d.isoformat(), emoji="🏫" if has else "📅",
                                     default=(d == self.date)) for d, label, has in sug])
        else:
            ds = discord.ui.Select(placeholder="3️⃣ Class dates appear after you choose section(s)", row=2,
                                   disabled=True, options=[discord.SelectOption(label="—", value="x")])
        ds.callback = self._date
        self.add_item(ds)
        if self.kind != "online":
            secs_txt = ", ".join(r.expand_secs(self.secs)) or "each chosen section"
            tg = discord.ui.Select(placeholder="📍 Post it in…", row=3, options=[
                discord.SelectOption(label=name, value=key, default=key == self.target,
                                     description=f"in the category of: {secs_txt}"[:100])
                for key, name in r.target_options()][:25])
            tg.callback = self._target
            self.add_item(tg)
        if self.kind == "online":
            vo = voice_options(self.cog.bot, self.cog.bot.get_cog("Routine").expand_secs(self.secs), self.where)
            cs = discord.ui.Select(placeholder="📍 Where? (empty = each section's own 🔊 voice channel)", row=3,
                                   min_values=0, max_values=1,
                                   options=vo or [discord.SelectOption(label="(no voice channels)", value="0")])
            cs.callback = self._where
            self.add_item(cs)
        b = discord.ui.Button(label="4️⃣ Enter title (date/time can be changed)", style=discord.ButtonStyle.success,
                              emoji="✏️", row=4)
        b.callback = self._go
        self.add_item(b)

    async def _kind(self, interaction):
        self.kind = interaction.data["values"][0]
        self.target = self.cog.bot.get_cog("Routine").default_target(self.kind)
        self._build()
        await interaction.response.edit_message(view=self)

    async def _target(self, interaction):
        self.target = interaction.data["values"][0]
        await interaction.response.defer()

    async def _where(self, interaction):
        vals = [v for v in interaction.data.get("values", []) if v != "0"]
        self.where = int(vals[0]) if vals else None
        await interaction.response.defer()

    async def _secs(self, interaction):
        self.secs = list(interaction.data["values"])
        self.date = None
        self._build()
        await interaction.response.edit_message(view=self)

    async def _date(self, interaction):
        from datetime import date as _d
        self.date = _d.fromisoformat(interaction.data["values"][0])
        await interaction.response.defer()

    async def _go(self, interaction):
        if not self.secs:
            await interaction.response.send_message("Choose the section(s) first.", ephemeral=True)
            return
        await interaction.response.send_modal(EventModal(self.cog, self.kind, self.secs, interaction, self.date,
                                                         self.where, self.target))


DATE_HELP = "Empty time = each section's class time on that day"


class EventModal(discord.ui.Modal):
    def __init__(self, cog, kind, secs, menu, day=None, where=None, target=None):
        name = cog.bot.get_cog("Routine").EVENT_TYPES[kind][0]
        super().__init__(title=f"📌 New {name}"[:45], timeout=900)
        self.cog, self.kind, self.secs, self.menu, self.where = cog, kind, secs, menu, where
        self.target = target
        self.head = discord.ui.TextInput(label="Title", max_length=100, placeholder="e.g. Assignment 2 — Inheritance"
                                         if kind != "online" else "e.g. OOP II — Polymorphism (online)")
        self.when = discord.ui.TextInput(label="Date", max_length=30, default=day.isoformat() if day else None,
                                         placeholder="2026-11-26  (or 26/11)")
        self.time = discord.ui.TextInput(label="Time (empty = each section's class time)", required=False,
                                         max_length=60,
                                         placeholder="11:59 PM  ·  23:59  ·  3D=09:00 3E=14:30")
        self.details = discord.ui.TextInput(label="Details (optional)", required=False, max_length=600,
                                            style=discord.TextStyle.paragraph,
                                            placeholder="Syllabus: Chapter 3–4 · Submit PDF in general-text")
        self.announce = discord.ui.TextInput(label="Announce now in the section? (yes / no)", default="yes",
                                             max_length=3, required=False)
        for x in (self.head, self.when, self.time, self.details, self.announce):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        tm = (self.time.value or "").strip()
        got = self.cog.parse_event_date(f"{self.when.value.strip()} {tm}".strip())
        if not got:
            await interaction.response.send_message("⚠️ Could not read the date. Use `2026-11-26` or `26/11`.",
                                                    ephemeral=True)
            return
        if tm and not got[1] and not got[2]:
            await interaction.response.send_message(f"⚠️ Could not read the time `{tm}`. Use `11:59 PM`, `23:59` or "
                                                    "`3D=09:00 3E=14:30` (or leave it empty).", ephemeral=True)
            return
        r = self.cog.bot.get_cog("Routine")
        when, times = r.resolve_when(self.secs, got[0], got[1], got[2])
        eid = r.add_event(self.kind, self.head.value.strip(), self.secs, when, (self.details.value or "").strip(),
                          interaction.user.name, times)
        r._events()[eid]["target"] = self.target or r.default_target(self.kind)
        if self.where:
            r._events()[eid]["where"] = self.where
        r.save_events()
        posted, staged = [], None
        if (self.announce.value or "yes").strip().lower().startswith("y"):
            staged = await r.stage_event(eid, interaction.user)
            if not staged:
                posted = await r.announce_event(eid)
        else:
            r._events()[eid]["no_announce"] = True
            r.save_events()
        tc = self.cog.bot.get_cog("Poster").test_channel()
        await interaction.response.send_message(
            f"✅ Saved: {r.event_line(when, r._events()[eid], True)}\n"
            + (f"👀 Preview in {tc.mention} — click **✅ Post now** there to announce it.\n" if staged else "")
            + (f"📢 Announced in: {', '.join(posted)}\n" if posted else "")
            + "Students see it under 📅 **My section today** in 🗓️┃routine-and-deadlines (on the day and 7 days before).",
            ephemeral=True)
        await done(self.menu, "✅ Deadline / test saved.")


class EventsListMenu(BaseMenu):
    def __init__(self, cog, user_id, items):
        super().__init__(cog, user_id)
        if items:
            r = cog.bot.get_cog("Routine")

            def opts():
                return [discord.SelectOption(
                    label=f"{w:%d %b %H:%M} · {r.EVENT_TYPES.get(ev['kind'], ('', ''))[0]}: {ev['title']}"[:100],
                    value=eid, description=", ".join(ev["sections"])[:100]) for w, eid, ev in items[:25]]
            ed = discord.ui.Select(placeholder="✏️ Select ONE to edit…", options=opts(), row=0)
            ed.callback = self._edit
            self.add_item(ed)
            o = opts()
            sel = discord.ui.Select(placeholder="🗑️ Select to delete…", options=o, min_values=1, max_values=len(o), row=1)
            sel.callback = self._delete
            self.add_item(sel)
            rm = discord.ui.Select(placeholder="🔔 Send a reminder NOW for…", options=opts(), row=2)
            rm.callback = self._remind
            self.add_item(rm)
            sch = discord.ui.Select(placeholder="⏰ Schedule a reminder at MY time for…", options=opts(), row=3)
            sch.callback = self._schedule
            self.add_item(sch)
            mv = discord.ui.Select(placeholder="📍 Change channel (post / online class voice channel)…", options=opts(), row=4)
            mv.callback = self._move
            self.add_item(mv)

    async def _move(self, interaction):
        eid = interaction.data["values"][0]
        v = MoveTargetMenu(self.cog, self.user_id, eid)
        await interaction.response.send_message(v.text(), view=v, ephemeral=True)

    async def _schedule(self, interaction):
        await interaction.response.send_modal(ReminderTimeModal(self.cog, interaction.data["values"][0]))

    async def _remind(self, interaction):
        r = self.cog.bot.get_cog("Routine")
        eid = interaction.data["values"][0]
        await interaction.response.defer(ephemeral=True, thinking=True)
        pid = await r.stage_reminder_now(eid, None, interaction.user)
        if pid:
            await self.cog.bot.get_cog("Poster").notify_staged(interaction, pid)
            return
        sent = await r.send_reminder(eid)
        await interaction.followup.send(("🔔 Reminder sent to: " + ", ".join(sent)) if sent else
                                        "⚠️ Could not send (no announcement channel?).", ephemeral=True)

    async def _edit(self, interaction):
        eid = interaction.data["values"][0]
        await interaction.response.send_modal(EventEditModal(self.cog, eid, interaction))

    async def _delete(self, interaction):
        r = self.cog.bot.get_cog("Routine")
        for eid in interaction.data["values"]:
            r._events().pop(eid, None)
        r.save_events()
        await interaction.response.edit_message(content="🗑️ Deleted.", view=None, embed=None)


class DashMenu(BaseMenu):
    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        opts = [discord.SelectOption(label="All sections", value="all", emoji="🏫")] + section_options(cog.s)
        sel = discord.ui.Select(placeholder="Section…", options=opts[:25])
        sel.callback = self._go
        self.add_item(sel)

    async def _go(self, interaction):
        v = interaction.data["values"][0]
        secs = list(self.cog.s.sections) if v == "all" else [v]
        sem = self.cog.bot.get_cog("Semester")
        await interaction.response.defer(ephemeral=True)
        for i in range(0, len(secs), 10):
            await interaction.followup.send(embeds=[sem.dashboard(x) for x in secs[i:i + 10]], ephemeral=True)


class NewSemesterModal(discord.ui.Modal):
    def __init__(self, cog):
        super().__init__(title="🆕 New semester — are you sure?", timeout=600)
        self.cog = cog
        self.confirm = discord.ui.TextInput(
            label="Type NEW SEMESTER to continue", max_length=20,
            placeholder="Archive → clear students, deadlines, attendance → remove student roles")
        self.add_item(self.confirm)

    async def on_submit(self, interaction: discord.Interaction):
        if self.confirm.value.strip().upper() != "NEW SEMESTER":
            await interaction.response.send_message("❎ Cancelled — nothing changed.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        sem = self.cog.bot.get_cog("Semester")

        async def report(t):
            try:
                await interaction.edit_original_response(content=t[:1990])
            except discord.HTTPException:
                pass
        text = await sem.new_semester(report)
        await interaction.followup.send(text, ephemeral=True)
        await self.cog.bot.send_log("🆕 New semester", text + f"\nby {interaction.user.mention}")


DEFAULT_CANCEL_NOTE = ("A makeup class will be announced later if needed. "
                       "Cancelled due to unavoidable reasons. Thank you for your understanding.")


class ClassChangeMenu(BaseMenu):
    """🚫 Cancel / 🔁 reschedule / 💻 make online one of my classes.

    Cancel: section dropdown is multi-select → all selected sections' classes appear
    together (labelled [SEC]) → pick any combination → one message per section channel.
    Reschedule / online: section single-select, class single-select (need per-class details).
    """
    ACTIONS = [("cancel", "Cancel class(es)", "🚫"), ("reschedule", "Reschedule (new date / time / room)", "🔁"),
               ("online", "Take it online", "💻")]

    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.secs: list = []     # for cancel (multi-section)
        self.sec = None          # for reschedule / online (single section)
        self.occ = None          # single (date, entry) — reschedule / online
        # list of (sec, date, entry) tuples selected for cancel
        self.occs: list = []
        self.action = "cancel"
        self._occ_map: list = []  # [(sec, date, entry), …] — indexed by dropdown value
        self._build()

    # ---------------------------------------------------------------- build
    def _build(self):
        self.clear_items()
        r = self.cog.bot.get_cog("Routine")
        is_cancel = self.action == "cancel"
        so = section_options(self.cog.s)

        if is_cancel:
            # Multi-select section dropdown
            for o in so:
                o.default = o.value in self.secs
            ss = discord.ui.Select(
                placeholder="1️⃣ Section(s) — pick one or more",
                options=so, row=0,
                min_values=1, max_values=len(so))
        else:
            for o in so:
                o.default = o.value == self.sec
            ss = discord.ui.Select(placeholder="1️⃣ Section", options=so, row=0)
        ss.callback = self._sec
        self.add_item(ss)

        # Build combined occ list for selected sections
        if is_cancel:
            combined = []
            for sec in self.secs:
                for d, it in r.class_occurrences(sec)[:25]:
                    combined.append((sec, d, it))
            combined = combined[:25]
            self._occ_map = combined
            if combined:
                selected_keys = {(s, d.isoformat(), it["time"], it["course"])
                                 for s, d, it in self.occs}
                opts = [
                    discord.SelectOption(
                        label=f"[{sec}] {d:%d %b} · {it['time']} · {it['course']}"[:100],
                        value=str(i),
                        description=f"Room {it['room']} · {r._course_name(it['course'], True)}"[:100],
                        emoji="🏫",
                        default=(sec, d.isoformat(), it["time"], it["course"]) in selected_keys,
                    )
                    for i, (sec, d, it) in enumerate(combined)
                ]
                os_ = discord.ui.Select(
                    placeholder="2️⃣ Which class(es) to cancel?",
                    row=1, min_values=1, max_values=len(opts), options=opts)
            else:
                self._occ_map = []
                os_ = discord.ui.Select(
                    placeholder="2️⃣ Classes appear after you choose section(s)", row=1,
                    disabled=True, options=[discord.SelectOption(label="—", value="x")])
        else:
            occ = r.class_occurrences(self.sec) if self.sec else []
            self._occ_map = [(self.sec, d, it) for d, it in occ[:25]]
            if self._occ_map:
                opts = [
                    discord.SelectOption(
                        label=f"{d:%a %d %b} · {it['time']} · {it['course']}"[:100],
                        value=str(i),
                        description=f"Room {it['room']} · {r._course_name(it['course'], True)}"[:100],
                        emoji="🏫",
                        default=(self.occ is not None and self.occ == (d, it)),
                    )
                    for i, (_, d, it) in enumerate(self._occ_map)
                ]
                os_ = discord.ui.Select(placeholder="2️⃣ Which class?", row=1, options=opts)
            else:
                os_ = discord.ui.Select(
                    placeholder="2️⃣ Your classes appear after you choose a section", row=1,
                    disabled=True, options=[discord.SelectOption(label="—", value="x")])
        os_.callback = self._occ_pick
        self.add_item(os_)

        ac = discord.ui.Select(placeholder="3️⃣ What to do?", row=2, options=[
            discord.SelectOption(label=l, value=v, emoji=e, default=v == self.action)
            for v, l, e in self.ACTIONS])
        ac.callback = self._act
        self.add_item(ac)

        chg = sorted(r._changes().items(), key=lambda x: x[1]["date"])[:25]
        if chg:
            un = discord.ui.Select(placeholder="↩️ Undo a change (delete it)…", row=3, options=[
                discord.SelectOption(label=f"{c['sec']} · {c['date']} · {c['kind']} · {c['course']}"[:100], value=k)
                for k, c in chg])
            un.callback = self._undo
            self.add_item(un)

        go = discord.ui.Button(label="4️⃣ Details & announce", emoji="📢",
                               style=discord.ButtonStyle.success, row=4)
        go.callback = self._go
        self.add_item(go)

    def _status(self) -> str:
        lines = ["🚫 **Cancel / reschedule / online class**",
                 "1️⃣ Section(s) → 2️⃣ class(es) → 3️⃣ action → 4️⃣ Details & announce"]
        if self.action == "cancel" and self.occs:
            picked = [f"[{sec}] **{it['course']}** {d:%d %b}" for sec, d, it in self.occs]
            lines.append(f"🗑️ Selected ({len(picked)}): " + ", ".join(picked))
        elif self.occ:
            d, it = self.occ
            lines.append(f"🏫 Selected: **{it['course']}** {d:%a %d %b} {it['time']}")
        return "\n".join(lines)

    # ---------------------------------------------------------------- callbacks
    async def _sec(self, i):
        vals = i.data["values"]
        if self.action == "cancel":
            self.secs = vals
            self.occs = []
        else:
            self.sec = vals[0]
            self.occ = None
        self._build()
        await i.response.edit_message(content=self._status(), view=self)

    async def _occ_pick(self, i):
        vals = i.data["values"]
        if self.action == "cancel":
            self.occs = [self._occ_map[int(v)] for v in vals if int(v) < len(self._occ_map)]
        else:
            idx = int(vals[0])
            if idx < len(self._occ_map):
                _, d, it = self._occ_map[idx]
                self.occ = (d, it)
        await i.response.edit_message(content=self._status(), view=self)

    async def _act(self, i):
        self.action = i.data["values"][0]
        self.occ = None
        self.occs = []
        self._build()
        await i.response.edit_message(content=self._status(), view=self)

    async def _undo(self, i):
        r = self.cog.bot.get_cog("Routine")
        r._changes().pop(i.data["values"][0], None)
        r.save_changes()
        self._build()
        await i.response.edit_message(content="↩️ Change removed (students' 📅 views are back to normal). "
                                              "Post a short notice if you had announced it.", view=self)

    async def _go(self, i):
        if self.action == "cancel":
            if not self.occs:
                await i.response.send_message("Choose at least one class to cancel.", ephemeral=True)
                return
            await i.response.send_modal(ClassCancelBulkModal(self.cog, self.occs, i))
        else:
            if not self.occ:
                await i.response.send_message("Choose the section and the class first.", ephemeral=True)
                return
            await i.response.send_modal(ClassChangeModal(self.cog, self.sec, self.occ, self.action, i))


class ClassCancelBulkModal(discord.ui.Modal):
    """Modal for cancelling one or more classes at once, possibly across multiple sections.

    occs: list of (sec, date, entry) tuples.
    """

    def __init__(self, cog, occs: list, menu):
        # occs = [(sec, date, entry), …]
        n = len(occs)
        secs_uniq = list(dict.fromkeys(sec for sec, _, _ in occs))
        sec_label = " · ".join(secs_uniq[:3]) + (" …" if len(secs_uniq) > 3 else "")
        super().__init__(title=f"🚫 Cancel {n} class{'es' if n > 1 else ''} · {sec_label}"[:45], timeout=900)
        self.cog, self.occs, self.menu = cog, occs, menu

        summary = ", ".join(
            f"[{sec}] {d:%d %b} {it['course']}" for sec, d, it in occs[:3]
        ) + (" …" if n > 3 else "")
        self.note = discord.ui.TextInput(
            label=f"Note for students ({summary})"[:45],
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=300,
            default=DEFAULT_CANCEL_NOTE,
            placeholder="Leave as-is or change the message students will see",
        )
        self.ann = discord.ui.TextInput(
            label="Announce now? (yes / no)", default="yes", max_length=3, required=False)
        self.add_item(self.note)
        self.add_item(self.ann)

    async def on_submit(self, interaction: discord.Interaction):
        r = self.cog.bot.get_cog("Routine")
        note = (self.note.value or "").strip()
        cids = []
        for sec, d, it in self.occs:
            cid = r.add_change(sec, d, it, "cancel", note=note)
            cids.append(cid)

        announce = (self.ann.value or "yes").strip().lower().startswith("y")
        staged, ok_secs = None, {}
        if announce:
            staged = await r.stage_changes_bulk(cids, note, interaction.user)
            if not staged:
                ok_secs = await r.announce_changes_bulk(cids, note)

        secs_uniq = list(dict.fromkeys(sec for sec, _, _ in self.occs))
        lines = [f"✅ Saved {len(cids)} cancellation(s) across **{', '.join(secs_uniq)}**:"]
        changes = r._changes()
        for cid in cids:
            c = changes.get(cid)
            if c:
                lines.append(f"  🚫 [{c['sec']}] {c['course']} · {c['date']} {c['time']}")
        if ok_secs:
            ok = [s for s, v in ok_secs.items() if v]
            lines.append(f"📢 Announced in: {', '.join(ok) or '—'}")
        if staged:
            lines.append("👀 Preview in the bot-test channel — click **✅ Post now** there to announce.")
        lines.append("Students see it under 📅 Today / Tomorrow in 🗓️┃routine-and-deadlines.")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)
        await done(self.menu, f"✅ {len(cids)} class(es) cancelled.")


class ClassChangeModal(discord.ui.Modal):
    def __init__(self, cog, sec, occ, action, menu):
        title = {"cancel": "🚫 Cancel class", "online": "💻 Online class", "reschedule": "🔁 Reschedule class"}[action]
        super().__init__(title=f"{title} · {sec}"[:45], timeout=900)
        self.cog, self.sec, self.occ, self.action, self.menu = cog, sec, occ, action, menu
        self.new = None
        self.place = None
        if action == "reschedule":
            self.new = discord.ui.TextInput(label="New date + time", max_length=40,
                                            placeholder="2026-11-28 10:30 AM   (date only = same time)")
            self.place = discord.ui.TextInput(label="Room / place (optional)", required=False, max_length=80,
                                              placeholder="Room 305  ·  Online")
        elif action == "online":
            vc = cog.bot.channel_for(f"sec:{sec}:voice_class")
            self.place = discord.ui.TextInput(label="Where? (channel or meeting link)", required=False, max_length=200,
                                              default=vc.mention if vc else None,
                                              placeholder="https://meet.google.com/...")
        self.note = discord.ui.TextInput(label="Note for students (optional)", required=False, max_length=300,
                                         style=discord.TextStyle.paragraph,
                                         default=DEFAULT_CANCEL_NOTE if action == "cancel" else None,
                                         placeholder="e.g. Make-up class will be announced · Bring your laptop")
        self.ann = discord.ui.TextInput(label="Announce now? (yes / no)", default="yes", max_length=3, required=False)
        for x in (self.new, self.place, self.note, self.ann):
            if x is not None:
                self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        r = self.cog.bot.get_cog("Routine")
        d, it = self.occ
        new_date, new_time = None, ""
        if self.action == "reschedule":
            got = self.cog.parse_event_date(self.new.value)
            if not got:
                await interaction.response.send_message("⚠️ Write the new date, e.g. `2026-11-28 10:30 AM`.",
                                                        ephemeral=True)
                return
            new_date = got[0]
            if got[1]:
                h, m = got[1]
                new_time = f"{(h % 12) or 12:02d}:{m:02d} {'AM' if h < 12 else 'PM'}"
        place = (self.place.value or "").strip() if self.place else ""
        cid = r.add_change(self.sec, d, it, self.action, new_date, new_time, place, (self.note.value or "").strip())
        ok, staged = False, None
        if (self.ann.value or "yes").strip().lower().startswith("y"):
            staged = await r.stage_change(cid, interaction.user)
            if not staged:
                ok = await r.announce_change(cid)
        await interaction.response.send_message(
            "✅ Saved: " + r.change_text(r._changes()[cid]) + ("\n📢 Announced in the section." if ok else "")
            + ("\n👀 Preview in the bot-test channel — click **✅ Post now** there to announce it." if staged else "")
            + "\nStudents see it under 📅 Today / Tomorrow in 🗓️┃routine-and-deadlines.", ephemeral=True)
        await done(self.menu, "✅ Class change saved.")


class MoveTargetMenu(BaseMenu):
    def __init__(self, cog, user_id, eid):
        super().__init__(cog, user_id)
        self.eid = eid
        r = cog.bot.get_cog("Routine")
        ev = r._events()[eid]
        self.target = ev.get("target") or r.default_target(ev["kind"])
        sel = discord.ui.Select(placeholder="📍 New channel", options=[
            discord.SelectOption(label=name, value=key, default=key == self.target) for key, name in r.target_options()][:25])
        sel.callback = self._pick
        self.add_item(sel)
        a = discord.ui.Button(label="Move: delete old post & post there", emoji="🚚", style=discord.ButtonStyle.primary)
        a.callback = self._move
        self.add_item(a)
        b = discord.ui.Button(label="Only for next reminders", emoji="💾", style=discord.ButtonStyle.secondary)
        b.callback = self._save
        self.add_item(b)
        if ev["kind"] == "online":
            vo = voice_options(cog.bot, r.expand_secs(ev["sections"]), ev.get("where"))
            vc = discord.ui.Select(placeholder="🔊 Change where the ONLINE CLASS happens", min_values=1, max_values=1,
                                   row=2, options=vo or [discord.SelectOption(label="(no voice channels)", value="0")])
            vc.callback = self._where
            self.add_item(vc)

    async def _where(self, i):
        r = self.cog.bot.get_cog("Routine")
        ev = r._events()[self.eid]
        if i.data["values"][0] == "0":
            await i.response.send_message("No voice channel to choose.", ephemeral=True)
            return
        ev["where"] = int(i.data["values"][0])
        ev["edited"] = self.cog.s.now_str()
        r.save_events()
        await i.response.defer()
        pid = await r.stage_event(self.eid, i.user, "edit") if ev.get("msgs") else None
        await i.edit_original_response(content=f"🔊 Class channel changed to <#{ev['where']}>"
                                       + (" · 👀 preview of the fixed announcement is in the bot-test channel "
                                          "(✅ Post now there)." if pid else ""), view=None)

    def text(self):
        ev = self.cog.bot.get_cog("Routine")._events()[self.eid]
        return f"📍 **{ev['title']}** — where should it be posted?"

    async def _pick(self, i):
        self.target = i.data["values"][0]
        await i.response.defer()

    async def _save(self, i):
        r = self.cog.bot.get_cog("Routine")
        r._events()[self.eid]["target"] = self.target
        r.save_events()
        await i.response.edit_message(content="💾 Saved — the old post stays; new posts go to the new channel.",
                                      view=None)

    async def _move(self, i):
        r = self.cog.bot.get_cog("Routine")
        r._events()[self.eid]["target"] = self.target
        r.save_events()
        await i.response.defer()
        pid = await r.stage_event(self.eid, i.user, "move")
        if pid:
            await i.edit_original_response(content="👀 Preview of the moved post is in the bot-test channel — "
                                                   "✅ Post now there deletes the old post and posts in the new channel.",
                                           view=None)
            return
        posted = await r.announce_event(self.eid, "move")
        await i.edit_original_response(content=f"🚚 Moved — posted in: {', '.join(posted) or 'nothing'}", view=None)


class ReminderTimeModal(discord.ui.Modal):
    def __init__(self, cog, eid):
        super().__init__(title="⏰ Schedule a reminder", timeout=900)
        self.cog, self.eid = cog, eid
        self.at = discord.ui.TextInput(label="When? (date + time, or +2h / +30m)", max_length=40,
                                       placeholder="2026-11-24 17:00   ·   24/11 5:00 PM   ·   +2h")
        self.secs = discord.ui.TextInput(label="Only some sections? (empty = all)", required=False, max_length=60,
                                         placeholder="e.g. 3D 3E")
        self.add_item(self.at)
        self.add_item(self.secs)

    async def on_submit(self, interaction: discord.Interaction):
        r = self.cog.bot.get_cog("Routine")
        raw = self.at.value.strip()
        at = None
        if raw.startswith("+"):
            at = self.cog.parse_when(raw)
        else:
            got = self.cog.parse_event_date(raw)
            if got and got[1]:
                d, (h, m) = got[0], got[1]
                at = datetime(d.year, d.month, d.day, h, m, tzinfo=self.cog.s.tz)
            elif not got:
                at = self.cog.parse_when(raw)  # e.g. just "17:00" → today/tomorrow
        if not at or at <= self.cog.s.now():
            await interaction.response.send_message("⚠️ Write a future date + time, e.g. `2026-11-24 17:00` or `+2h`.",
                                                    ephemeral=True)
            return
        secs = [x for x in (self.cog.s.normalize_section(w) for w in (self.secs.value or "").replace(",", " ").split()) if x]
        r.add_custom_reminder(self.eid, at, secs)
        ev = r._events()[self.eid]
        await interaction.response.send_message(
            f"⏰ Reminder scheduled for **{at:%a %d %b, %I:%M %p}** → {', '.join(secs) or 'all its sections'}\n"
            "👀 A preview appears in the bot-test channel within 2 minutes — **✅ Approve** it there, "
            "otherwise it is NOT sent.\n"
            f"Reminders for **{ev['title']}**: {r.reminder_lines(ev)}", ephemeral=True)


class EventEditModal(discord.ui.Modal):
    def __init__(self, cog, eid, menu, restage=None):
        r = cog.bot.get_cog("Routine")
        ev = r._events()[eid]
        super().__init__(title="✏️ Edit deadline / test", timeout=900)
        self.cog, self.eid, self.menu, self.restage = cog, eid, menu, restage
        from datetime import datetime as _dt
        w = _dt.fromisoformat(ev["when"])
        self.head = discord.ui.TextInput(label="Title", max_length=100, default=ev["title"])
        times = {k: v for k, v in (ev.get("times") or {}).items() if k != "*"}
        if times and len(set(times.values())) > 1:
            default = f"{w:%Y-%m-%d} " + " ".join(f"{k}={v}" for k, v in times.items())
        else:
            default = f"{w:%Y-%m-%d %H:%M}"
        self.when = discord.ui.TextInput(label="Date (time optional → class time)", max_length=80, default=default,
                                         placeholder="2026-11-25 · 2026-11-25 23:59 · 2026-11-25 3D=08:30 3E=10:30")
        self.secs = discord.ui.TextInput(label="Sections (space separated, or all)", max_length=60,
                                         default=" ".join(ev["sections"]))
        self.details = discord.ui.TextInput(label="Details (optional)", required=False, max_length=600,
                                            style=discord.TextStyle.paragraph, default=ev.get("details") or None)
        had = bool(ev.get("msgs"))
        self.mode = discord.ui.TextInput(label="Announcement: edit / new / no", max_length=4,
                                         default="edit" if had else "no",
                                         placeholder="edit = fix old post silently · new = post again (notify)")
        for x in (self.head, self.when, self.secs, self.details, self.mode):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        r = self.cog.bot.get_cog("Routine")
        ev = r._events().get(self.eid)
        if ev is None:
            await interaction.response.send_message("This item was deleted.", ephemeral=True)
            return
        got = self.cog.parse_event_date(self.when.value)
        if not got:
            await interaction.response.send_message("⚠️ Could not read the date. Use `2026-11-25` "
                                                    "(+ optional time `23:59`).", ephemeral=True)
            return
        words = self.secs.value.replace(",", " ").split()
        if any(w.lower() == "all" for w in words):
            secs = ["all"]
        else:
            secs = [x for x in (self.cog.s.normalize_section(w) for w in words) if x]
        if not secs:
            await interaction.response.send_message("⚠️ Write at least one valid section (e.g. `3D 3E`) or `all`.",
                                                    ephemeral=True)
            return
        when, times = r.resolve_when(secs, got[0], got[1], got[2])
        if when.isoformat(timespec="minutes") != ev["when"] or times != ev.get("times"):
            ev["auto_reminded"] = []
        ev.update({"title": self.head.value.strip(), "when": when.isoformat(timespec="minutes"), "sections": secs,
                   "times": times,
                   "details": (self.details.value or "").strip(), "edited": self.cog.s.now_str()})
        r.save_events()
        mode = (self.mode.value or "no").strip().lower()
        if self.restage and mode.startswith("no"):
            mode = "new"  # editing an unposted preview → make a fresh preview
        posted, staged = [], None
        if mode.startswith("e") or mode.startswith("n"):
            m = "edit" if mode.startswith("e") else "new"
            staged = await r.stage_event(self.eid, interaction.user, m, replace=self.restage)
            if not staged:
                posted = await r.announce_event(self.eid, m)
        await interaction.response.send_message(
            f"✅ Updated: {r.event_line(when, ev, True)}\n"
            + ("👀 New preview in the bot-test channel — click ✅ Post now there.\n" if staged else "")
            + (f"📢 Announcement {'edited' if mode.startswith('e') else 'posted'} in: {', '.join(posted)}\n" if posted else "")
            + "Students see the new date under 📅 in 🗓️┃routine-and-deadlines.", ephemeral=True)
        if self.menu is not None:
            await done(self.menu, "✅ Deadline / test updated.")


# ====================================================================== attendance
def _sections_in_channel(bot, ch: discord.VoiceChannel, settings) -> list[str]:
    """Sections whose role has access to this voice channel (can connect / view)."""
    out = []
    for sec in settings.sections:
        role = bot.role_for(f"role:sec:{sec}")
        if role is None:
            continue
        perms = ch.permissions_for(role)
        if perms.view_channel or perms.connect:
            out.append(sec)
    return out


class AttStartMenu(BaseMenu):
    """▶️ Start attendance.

    Flow A — specific voice channel (new):
      1️⃣ Pick a voice channel → bot shows which sections have access there
      2️⃣ Pick section(s) (multi-select, default = all with access)
      3️⃣ Course per section (modal if sections differ in course)
      4️⃣ Duration → Start

    Flow B — classic (section-first, category-based):
      If no voice channel is chosen, falls back to the old per-section category tracking.
    """

    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.voice_ch: int | None = None   # chosen voice channel ID
        self.secs: list = []               # chosen sections
        self.courses: dict = {}            # sec → course key
        self.minutes = 0
        self._eligible: list = []          # sections that have access to voice_ch
        self._build()

    # ---------------------------------------------------------------- build
    def _build(self):
        self.clear_items()
        s = self.cog.s
        bot = self.cog.bot
        att = bot.get_cog("Attendance")
        running = set(att.sessions)

        # Row 0 — voice channel dropdown
        vo = voice_options(bot, list(s.sections), self.voice_ch)
        none_opt = discord.SelectOption(label="(no specific channel — use section category)", value="0",
                                        emoji="🏫", default=(self.voice_ch is None))
        vc_sel = discord.ui.Select(
            placeholder="1️⃣ Voice channel (or 'section category')",
            options=[none_opt] + vo if vo else [none_opt],
            row=0)
        vc_sel.callback = self._vc
        self.add_item(vc_sel)

        # Row 1 — section multi-select
        if self.voice_ch:
            eligible = self._eligible or []
        else:
            eligible = [sec for sec in s.sections if sec not in running]
        if eligible:
            sec_opts = []
            for o in section_options(s):
                if o.value not in eligible:
                    continue
                if o.value in running:
                    o.label += " ⚠️ running"
                    o.description = "already has attendance — will be skipped"
                o.default = o.value in self.secs
                sec_opts.append(o)
            if sec_opts:
                ss = discord.ui.Select(
                    placeholder="2️⃣ Section(s)",
                    options=sec_opts, row=1,
                    min_values=1, max_values=len(sec_opts))
                ss.callback = self._secs
                self.add_item(ss)

        # Row 2 — course (single dropdown showing all selected sections' default course)
        if self.secs:
            # Build a flat list of (sec, course) options — one entry per section×course combo
            all_copts = []
            for sec in self.secs:
                for o in course_options(s, sec):
                    o.label = f"[{sec}] {o.label}"
                    o.value = f"{sec}:{o.value}"
                    o.default = (self.courses.get(sec) == o.value.split(":", 1)[1])
                    all_copts.append(o)
            if all_copts:
                csel = discord.ui.Select(
                    placeholder="3️⃣ Course (one per section)",
                    options=all_copts[:25], row=2,
                    min_values=len(self.secs), max_values=len(all_copts[:25]))
                csel.callback = self._course
                self.add_item(csel)

        # Row 3 — duration
        if self.secs:
            dopts = [discord.SelectOption(label=l, value=str(m), emoji="⏱️", default=(m == self.minutes))
                     for l, m in DURATIONS]
            dsel = discord.ui.Select(placeholder="4️⃣ Auto-stop after…", options=dopts, row=3)
            dsel.callback = self._dur
            self.add_item(dsel)

        # Row 4 — start button
        if self.secs and all(self.courses.get(sec) for sec in self.secs):
            go = discord.ui.Button(label=f"▶️ Start attendance ({len(self.secs)} section(s))",
                                   style=discord.ButtonStyle.success, row=4)
            go.callback = self._go
            self.add_item(go)

    def _embed(self) -> discord.Embed:
        s = self.cog.s
        if not self.secs:
            return discord.Embed(title="▶️ Start attendance",
                                 description="1️⃣ Pick a voice channel (or section category)\n"
                                             "2️⃣ Pick section(s)\n3️⃣ Course · 4️⃣ Duration → Start",
                                 color=discord.Color.green())
        lines = []
        g = self.cog.bot.main_guild()
        if self.voice_ch and g:
            vc = g.get_channel(self.voice_ch)
            lines.append(f"🔊 **Voice channel:** {vc.mention if vc else self.voice_ch}")
        for sec in self.secs:
            c_key = self.courses.get(sec)
            c = s.courses[c_key] if c_key else None
            lines.append(f"• **{sec}** — {c['code'] + ' ' + c['short'] if c else '(choose course)'}")
        lines.append(f"⏱️ Auto-stop: {self.minutes or 'off'}{' min' if self.minutes else ''}")
        return discord.Embed(title="▶️ Start attendance", description="\n".join(lines),
                             color=discord.Color.green())

    # ---------------------------------------------------------------- callbacks
    async def _vc(self, interaction):
        val = int(interaction.data["values"][0])
        if val == 0:
            self.voice_ch = None
            self._eligible = []
        else:
            self.voice_ch = val
            g = self.cog.bot.main_guild()
            ch = g.get_channel(val) if g else None
            self._eligible = _sections_in_channel(self.cog.bot, ch, self.cog.s) if ch else []
            # pre-select all eligible that aren't already running
            running = set(self.cog.bot.get_cog("Attendance").sessions)
            self.secs = [sec for sec in self._eligible if sec not in running]
            # set default courses
            for sec in self.secs:
                if sec not in self.courses:
                    self.courses[sec] = self.cog.s.default_course(sec)
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _secs(self, interaction):
        self.secs = interaction.data["values"]
        # keep existing course choices, default new ones
        for sec in self.secs:
            if sec not in self.courses:
                self.courses[sec] = self.cog.s.default_course(sec)
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _course(self, interaction):
        for v in interaction.data["values"]:
            sec, course = v.split(":", 1)
            self.courses[sec] = course
        self._build()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _dur(self, interaction):
        self.minutes = int(interaction.data["values"][0])
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _go(self, interaction):
        att = self.cog.bot.get_cog("Attendance")
        running = set(att.sessions)
        channels = [self.voice_ch] if self.voice_ch else []
        results = []
        for sec in self.secs:
            if sec in running:
                results.append(f"⚠️ {sec} already running — skipped")
                continue
            course = self.courses.get(sec) or self.cog.s.default_course(sec)
            try:
                text = await att.start_session(sec, course, self.minutes,
                                               interaction.user.name, channels=channels)
                results.append(text)
            except commands.BadArgument as e:
                results.append(f"⚠️ {sec}: {e}")
        await interaction.response.edit_message(content="✅ Done.", embed=None, view=None)
        await interaction.channel.send("\n".join(results))


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
            view=ConfirmView(self.cog, interaction.user.id, go, label="Yes, kick", typed="KICK"), embed=None)


# ====================================================================== the panel
B = discord.ButtonStyle
# Each group = one message in the admin channel: title, short text per button, then the buttons.
PANEL_GROUPS = [
    ("Row 1 · 📢 Communication", discord.Color.blurple(), [
        ("announce", "Announce", "📢", B.primary, "Pick section/course → write → posts in their announcement channel (can be scheduled)."),
        ("post", "Post as bot", "📤", B.success, "ANY channels (many) + roles → your text / files / link buttons → bot posts it everywhere."),
        ("edit_post", "Edit bot post", "✏️", B.secondary, "Paste a message link → edit the text, buttons or style of any message the bot posted."),
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
        ("clean_welcome", "Clean welcome", "🧹", B.danger, "Remove messages in #welcome and #id-name-section (pinned stay)."),
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
        ("event_add", "Add deadline / test / online class", "📌", B.success, "Type → sections → 📅 pick a date (class days marked) → title. Auto reminder: 10 PM the day before (online class: 15 min before)."),
        ("event_list", "Edit / delete / remind", "📋", B.secondary, "Upcoming deadlines, tests, online classes → ✏️ edit · 🗑️ delete · 🔔 send a reminder now."),
        ("subs", "Submission list", "📝", B.primary, "Google Sheet / Form link → who submitted (ID, name, time) → each section's list in its own channel."),
    ]),
    ("Row 8 · 🏫 Section & semester", discord.Color.dark_teal(), [
        ("dash", "Section dashboard", "📊", B.primary, "One look: students, next classes, deadlines, changes, attendance, submissions."),
        ("class_change", "Cancel / reschedule class", "🚫", B.danger, "Cancel · move to another day/time/room · take online → announced + shown in 📅."),
        ("archive", "Semester archive", "🗂️", B.secondary, "ZIP of all data + students Excel + every section channel's posts."),
        ("new_semester", "New semester", "🆕", B.danger, "Archive → clear students/deadlines/attendance → remove student roles (asks first)."),
    ]),
    ("Row 9 · 📖 Help", discord.Color.teal(), [
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
        if key == "dash":
            await menu(DashMenu(self, uid), "📊 **Section dashboard** — choose a section (or all).")
            return
        if key == "class_change":
            await menu(ClassChangeMenu(self, uid), "🚫 **Cancel / reschedule / online** — section → class → action → "
                                                    "📢 Details & announce.\nStudents also see it in 📅 Today / Tomorrow.")
            return
        if key == "archive":
            sem = self.bot.get_cog("Semester")

            async def go_archive(i):
                async def rep_(t):
                    pass
                await sem.send_archive(rep_)
                await i.followup.send("🗂️ Archive posted in the admin channel.", ephemeral=True)
            await interaction.response.send_modal(TypedConfirmModal("ARCHIVE", go_archive))
            return
        if key == "new_semester":
            await interaction.response.send_modal(NewSemesterModal(self))
            return
        if key == "subs":
            from cogs.submissions import SubModal
            await interaction.response.send_modal(SubModal(interaction.client.get_cog("Submissions"), interaction.user.id))
            return
        if key == "post":
            from cogs.poster import PostMenu
            pc = interaction.client.get_cog("Poster")
            pm = PostMenu(pc, uid, interaction.guild)
            await menu(pm, pm.status_text() + "\n🔗 Style **…+ link buttons**: every link becomes a button (link under it)\n"
                       "🤖 First time? Click **🤖 Only bot-test** (only admins see it) to try.")
            return
        if key == "edit_post":
            from cogs.poster import EditPostLinkModal
            pc = interaction.client.get_cog("Poster")
            await interaction.response.send_modal(EditPostLinkModal(pc, uid))
            return
        if key == "clean_welcome":
            await menu(CleanWelcomeMenu(self, uid),
                       "🧹 **Clean #welcome and #id-name-section** (pinned instructions always stay)\n"
                       "• **Verified / left only** — happens automatically too\n"
                       "• **Everything** — also messages of students who are still waiting")
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
            text = ("📌 **Upcoming deadlines & tests**\n" + "\n".join(r.event_line(w, ev, True) + f"\n   ⏰ {r.reminder_lines(ev)}"
                                                                    for w, _, ev in items)
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
    def parse_event_date(self, raw: str):
        """'2026-11-25' / '25-11-2026' / '25/11' + optional '23:59' or '11:59 PM' + optional '3D=08:30 3E=10:30'
        → (date, (h, m) or None, {sec: (h, m)}) or None."""
        txt = (raw or "").strip()
        per = {}
        for sec_txt, hh, mm in re.findall(r"(?i)\b([0-9][A-Z])\s*[=:@]\s*(\d{1,2})[:.](\d{2})", txt):
            sec = self.s.normalize_section(sec_txt)
            if sec:
                per[sec] = (int(hh), int(mm))
        txt = re.sub(r"(?i)\b[0-9][A-Z]\s*[=:@]\s*\d{1,2}[:.]\d{2}", " ", txt).strip()
        m = (re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", txt) or re.search(r"(\d{1,2})[-/](\d{1,2})(?:[-/](\d{2,4}))?", txt))
        if not m:
            return None
        now = self.s.now()
        try:
            if len(m.group(1)) == 4:
                d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            else:
                y = m.group(3)
                y = int(y) + (2000 if y and len(y) == 2 else 0) if y else now.year
                d = datetime(y, int(m.group(2)), int(m.group(1))).date()
        except ValueError:
            return None
        rest = txt[m.end():]
        hm = None
        t = re.search(r"(\d{1,2})[:.](\d{2})\s*([ap]m)?", rest, re.I)
        if t:
            h, mi = int(t.group(1)), int(t.group(2))
            if t.group(3):
                h = h % 12 + (12 if t.group(3).lower() == "pm" else 0)
            hm = (h, mi)
        return d, hm, per

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

    def _reminder_embed(self, target, link, snapshot, note):
        admin = self.bot.get_cog("Admin")
        kind, val = admin.resolve_target(target)
        ch, roles, _, label = admin.target_info(kind, val)
        quoted = "\n".join(f"> {line}" for line in (snapshot or "(no text)").splitlines()[:15])
        e = discord.Embed(title="⏰ Reminder", color=discord.Color.orange(), timestamp=self.s.now(),
                          description=((f"**{note}**\n\n" if note else "") + quoted)[:4000])
        e.set_footer(text=f"{self.s.teacher} · {label}")
        return ch, roles, e

    async def stage_msg_reminder(self, target, link, snapshot, note, by):
        poster = self.bot.get_cog("Poster")
        if not poster or not poster.test_channel():
            return None
        ch, roles, e = self._reminder_embed(target, link, snapshot, note)
        mention = " ".join(r.mention for r in roles if r) or "nobody"
        info = [f"📍 **Where:** {ch.mention if ch else '—'} ({ch.category.name if ch and ch.category else ''})",
                f"🔔 **Mention:** {mention}", f"🔗 Original: {link}"]

        async def render(tc):
            v = discord.ui.View(timeout=None)
            v.add_item(discord.ui.Button(label="Open the original message", emoji="🔗",
                                         style=discord.ButtonStyle.link, url=link))
            await tc.send(content=mention if mention != "nobody" else None, embed=e, view=v,
                          allowed_mentions=discord.AllowedMentions.none())
        return await poster.gate("msgrem", {"target": target, "link": link, "snapshot": snapshot, "note": note},
                                 f"Reminder → {target}", info, render, 1, by)

    async def stage_job(self, jid, by):
        """Scheduled announcement / reminder → preview in bot-test; it is only posted if approved."""
        poster = self.bot.get_cog("Poster")
        j = self.jobs.get(jid)
        if not poster or not j or not poster.test_channel():
            if j:
                j["ok"] = True
                self.save_jobs()
            return None
        j["ok"] = False
        self.save_jobs()
        when = datetime.fromisoformat(j["when"])
        info = [f"🗓️ **Scheduled for:** {when:%a %d %b %Y, %I:%M %p}", f"📍 **To:** {', '.join(j['targets'])}",
                f"🔔 **Mention:** {'yes (section roles)' if j.get('ping') else 'no'}",
                "Not approved = **not posted**."]
        admin = self.bot.get_cog("Admin")

        async def render(tc):
            if j.get("kind") == "reminder":
                _, _, e = self._reminder_embed(j["targets"][0], j["link"], j.get("snapshot", ""), j.get("text", ""))
            else:
                kind, val = admin.resolve_target(j["targets"][0])
                _, _, _, label = admin.target_info(kind, val)
                e = discord.Embed(title=j.get("title") or "📢 Announcement", description=j["text"] or None,
                                  color=discord.Color.gold())
                e.set_footer(text=f"{self.s.teacher} · {label}")
            await tc.send(embed=e)
        return await poster.gate("job", {"jid": jid}, "Scheduled post", info, render, len(j["targets"]), by)

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
            if j.get("ok") is False:  # never approved in bot-test → not posted
                await log_ch.send(f"⏭️ Scheduled post `{jid}` → {', '.join(j['targets'])} was **not posted** "
                                  "(it was not approved in the bot-test channel).")
                continue

            class _Ctx:
                approved = True
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
