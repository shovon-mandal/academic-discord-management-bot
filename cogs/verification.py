"""Student verification: welcome buttons → form (modal) or typed message → roles + nickname.

After verification the member gets:  Sec-XX role (section category + course group)
                                     Verified role (All Students; hides the welcome area)
"""
from __future__ import annotations

import asyncio
import re
import logging
from collections import defaultdict
from itertools import cycle

import discord
from discord.ext import commands

from core.config import DATA_DIR
from core.parsing import build_nickname, clean_name, parse_verification_text
from core.storage import append_audit, load_roster

WELCOME_VERSION = "5"  # bump → welcome/section cards re-post automatically after update

log = logging.getLogger("nubtk.verify")

GREEN, RED, GOLD, BLUE, ORANGE = (discord.Color.green(), discord.Color.red(), discord.Color.gold(),
                                  discord.Color.blurple(), discord.Color.orange())


class VResult:
    TITLES = {"verified": "✅ Verified!", "pending": "⏳ Waiting for teacher approval",
              "already": "ℹ️ Already verified", "error": "⚠️ Could not verify"}
    COLORS = {"verified": GREEN, "pending": GOLD, "already": BLUE, "error": RED}

    def __init__(self, status: str, text: str):
        self.status = status
        self.text = text

    def embed(self) -> discord.Embed:
        return discord.Embed(title=self.TITLES[self.status], description=self.text, color=self.COLORS[self.status])


# ------------------------------------------------------------------ UI
class SectionButton(discord.ui.Button):
    def __init__(self, sec: str, label: str, style, row: int):
        super().__init__(label=label[:80], style=style, custom_id=f"nubtk:sec:{sec}", row=row)
        self.sec = sec

    async def callback(self, interaction: discord.Interaction):
        cog = interaction.client.get_cog("Verification")
        await cog.on_section_button(interaction, self.sec)


class SectionView(discord.ui.View):
    """Persistent: survives bot restarts (custom_id based)."""

    def __init__(self, settings):
        super().__init__(timeout=None)
        styles = cycle([discord.ButtonStyle.success, discord.ButtonStyle.primary,
                        discord.ButtonStyle.danger, discord.ButtonStyle.secondary])
        per_row = 3 if len(settings.sections) <= 15 else 5
        for i, sec in enumerate(list(settings.sections)[:25]):
            self.add_item(SectionButton(sec, settings.section_label(sec), next(styles), row=i // per_row))


class VerifyModal(discord.ui.Modal):
    def __init__(self, cog, sec: str):
        super().__init__(title=f"Verify · Section {sec}"[:45], timeout=900)
        self.cog = cog
        self.sec = sec
        n = cog.s.id_length
        self.sid = discord.ui.TextInput(label=f"Student ID ({n} digits)", placeholder="11250121964"[:n].ljust(n, "0"),
                                        min_length=n, max_length=n)
        self.full_name = discord.ui.TextInput(label="Full official name (English)", placeholder="e.g. Shovon Mandal",
                                              min_length=2, max_length=60)
        self.add_item(self.sid)
        self.add_item(self.full_name)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await self.cog.verify_request(interaction.guild, interaction.user, self.sec,
                                               self.sid.value, self.full_name.value, source="form")
        kwargs = {"embed": result.embed(), "ephemeral": True}
        if result.status in ("verified", "already"):
            rec = self.cog.bot.store.get(interaction.user.id)
            view = self.cog.go_view(rec["section"]) if rec else None
            if view:
                kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.exception("Modal error", exc_info=error)
        try:
            await interaction.followup.send("⚠️ Something went wrong. Please try again or contact the teacher.",
                                            ephemeral=True)
        except discord.HTTPException:
            pass


def _stopped_view(*items) -> discord.ui.View:
    """A view whose buttons are handled by on_interaction (works after restarts)."""
    v = discord.ui.View(timeout=None)
    for it in items:
        v.add_item(it)
    v.stop()
    return v


# ------------------------------------------------------------------ cog
class Verification(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        self.locks = defaultdict(asyncio.Lock)
        self._scanned = False

    async def cog_load(self):
        self.bot.add_view(SectionView(self.s))

    # ---------------------------------------------------------- helpers
    def section_role_ids(self) -> set:
        ids = set()
        for sec in self.s.sections:
            r = self.bot.role_for(f"role:sec:{sec}")
            if r:
                ids.add(r.id)
        return ids

    def is_verified(self, member: discord.Member) -> bool:
        vr = self.bot.role_for("role:verified")
        role_ids = {r.id for r in member.roles}
        return bool((vr and vr.id in role_ids) or (role_ids & self.section_role_ids()))

    def _main(self, guild) -> bool:
        g = self.bot.main_guild()
        return g is not None and guild is not None and guild.id == g.id

    # ---------------------------------------------------------- button
    async def on_section_button(self, interaction: discord.Interaction, sec: str):
        member = interaction.user
        if not isinstance(member, discord.Member) or not self._main(interaction.guild):
            await interaction.response.send_message("Please use this inside the class server.", ephemeral=True)
            return
        if sec not in self.s.sections:
            await interaction.response.send_message("This section no longer exists. Ask the teacher.", ephemeral=True)
            return
        if self.is_verified(member):
            rec = self.bot.store.get(member.id)
            await interaction.response.send_message(
                f"You are already verified as **{rec['section'] if rec else '?'}**. "
                "To change section, please contact the teacher.", ephemeral=True)
            return
        if self.bot.pending.find_by_member(member.id):
            await interaction.response.send_message("⏳ Your request is already waiting for teacher approval.",
                                                    ephemeral=True)
            return
        await interaction.response.send_modal(VerifyModal(self, sec))

    # ---------------------------------------------------------- core flow
    async def verify_request(self, guild, user, sec, sid_raw, name_raw, source="form") -> VResult:
        member = guild.get_member(user.id) if guild else None
        if member is None:
            return VResult("error", "You must be a member of the server.")
        async with self.locks[member.id]:
            n = self.s.id_length
            sid = "".join(ch for ch in (sid_raw or "") if ch.isdigit())
            name = clean_name(name_raw)
            if sec not in self.s.sections:
                return VResult("error", f"Unknown section. Valid: {', '.join(self.s.sections)}")
            if len(sid) != n:
                return VResult("error", f"Student ID must be exactly **{n} digits**.")
            if len(name) < 2:
                return VResult("error", "Please write your **full official name** in English letters.")
            if self.is_verified(member):
                rec = self.bot.store.get(member.id)
                return VResult("already", f"You are already verified as **{rec['section'] if rec else '?'}**.")
            if self.bot.pending.find_by_member(member.id):
                return VResult("pending", "Your request is already waiting for teacher approval.")

            reasons = []
            others = [r for r in self.bot.store.find_by_student_id(sid)
                      if r.get("discord_id") != str(member.id) and r.get("status", "active") == "active"]
            if others:
                if self.s.duplicate_policy == "block":
                    return VResult("error", "This Student ID is already registered with another Discord account. "
                                            "Please contact the teacher.")
                if self.s.duplicate_policy == "admin_approval":
                    reasons.append(f"Student ID already used by <@{others[0]['discord_id']}> "
                                   f"({others[0].get('name')}, {others[0].get('section')})")

            if self.s.roster_check:
                roster = load_roster(sec)
                if roster:
                    if sid not in roster:
                        reasons.append(f"ID `{sid}` is not in the official roster of {sec}")
                    elif roster[sid]:
                        name = clean_name(roster[sid]) or name  # use official spelling

            if reasons:
                await self.create_pending(guild, member, sec, sid, name, reasons, source)
                return VResult("pending", "Your information was sent to the teacher for approval. "
                                          "You'll get a DM once it is approved.")

            ok, text = await self.apply_verification(guild, member, sec, sid, name, by=f"self:{source}")
            return VResult("verified" if ok else "error", text)

    async def apply_verification(self, guild, member, sec, sid, name, by="self") -> tuple:
        s = self.s
        sec_role = self.bot.role_for(f"role:sec:{sec}")
        verified_role = self.bot.role_for("role:verified")
        if not sec_role or not verified_role:
            await self.bot.send_log("❌ Roles missing", "Run `!setup_server` first.", RED, admin=True)
            return False, "The server is not fully set up yet. Please try again later."

        old_rec = self.bot.store.get(member.id)
        extras = [x for x in ((old_rec or {}).get("extra_sections") or []) if x in s.sections and x != sec]
        keep_ids = {sec_role.id} | {r.id for r in (self.bot.role_for(f"role:sec:{x}") for x in extras) if r}
        remove = [r for r in member.roles if r.id in self.section_role_ids() and r.id not in keep_ids]
        extra_roles = [r for r in (self.bot.role_for(f"role:sec:{x}") for x in extras) if r and r not in member.roles]
        try:
            if remove:
                await member.remove_roles(*remove, reason=f"Section change → {sec}")
            await member.add_roles(sec_role, verified_role, *extra_roles, reason=f"Verified {sec} {sid} ({by})")
        except discord.Forbidden:
            await self.bot.send_log("❌ Permission error",
                                    f"Could not give roles to {member.mention}. Move the bot role to the top.",
                                    RED, admin=True)
            return False, "Bot permission problem. The teacher has been notified."

        nick = build_nickname(s.nickname_format, sid, sec, name)
        nick_note = ""
        try:
            if member.nick != nick:
                await member.edit(nick=nick, reason="Class nickname")
        except discord.Forbidden:
            nick_note = "\n⚠️ Nickname could not be changed (owner / higher role)."

        record = {
            "discord_id": str(member.id),
            "discord_name": member.name,
            "section": sec,
            "student_id": sid,
            "name": name,
            "nickname": nick,
            "extra_sections": extras,
            "courses": sorted({s.courses[c]["code"] for x in [sec, *extras] for c in s.section_courses(x)}),
            "status": "active",
            "verified_at": old_rec.get("verified_at") if old_rec and old_rec.get("section") == sec else s.now_str(),
            "updated_at": s.now_str(),
            "verified_by": by,
        }
        self.bot.store.upsert(record)
        append_audit(s, "verified", discord_id=str(member.id), section=sec, student_id=sid, name=name, by=by)

        await self.cleanup_id_channel(guild, member)

        moved = f" (moved from {old_rec['section']})" if old_rec and old_rec.get("section") != sec else ""
        await self.bot.send_log(
            "✅ Student verified" + moved,
            f"{member.mention} · `{member.name}`\n**Section:** {sec} · {s.section_courses_text(sec)}\n"
            f"**ID:** `{sid}` · **Name:** {name}\n**Nickname:** `{nick}` · via `{by}`{nick_note}", GREEN)

        try:
            dm = {"embed": discord.Embed(
                title="✅ You are verified!",
                description=(f"Server: **{guild.name}**\nSection: **{sec}** · {s.section_courses_text(sec)}\n"
                             f"Nickname: `{nick}`\n\nYour section channels are now open — including **all earlier "
                             "messages, slides and resources**. Start with your section's announcement page 👇"),
                color=GREEN)}
            gv = self.go_view(sec)
            if gv:
                dm["view"] = gv
            await member.send(**dm)
        except discord.HTTPException:
            pass
        return True, (f"Welcome to **Section {sec}** ({s.section_courses_text(sec)}).\n"
                      f"Your nickname is now `{nick}`.\nYour section channels are now open, with **all earlier "
                      "messages and files**. 👇 Click the button to go to your section's announcement page.")

    async def remove_verification(self, guild, member, by="admin") -> dict | None:
        roles = [r for r in member.roles if r.id in self.section_role_ids()]
        vr = self.bot.role_for("role:verified")
        if vr and vr in member.roles:
            roles.append(vr)
        try:
            if roles:
                await member.remove_roles(*roles, reason=f"Unverified by {by}")
            await member.edit(nick=None, reason="Unverified")
        except discord.Forbidden:
            pass
        rec = self.bot.store.remove(member.id)
        append_audit(self.s, "unverified", discord_id=str(member.id), by=by, record=rec)
        return rec

    # ---------------------------------------------------------- pending approvals
    async def create_pending(self, guild, member, sec, sid, name, reasons, source):
        data = {"discord_id": str(member.id), "discord_name": member.name, "section": sec,
                "student_id": sid, "name": name, "reasons": reasons, "source": source,
                "created_at": self.s.now_str()}
        pid = self.bot.pending.add(data)
        embed = discord.Embed(title="🟡 Verification needs your approval", color=GOLD, timestamp=self.s.now())
        embed.add_field(name="Member", value=f"{member.mention}\n`{member.name}`", inline=True)
        embed.add_field(name="Section", value=sec, inline=True)
        embed.add_field(name="Student ID", value=f"`{sid}`", inline=True)
        embed.add_field(name="Name", value=name, inline=False)
        embed.add_field(name="Why", value="\n".join(f"• {r}" for r in reasons), inline=False)
        embed.set_footer(text=f"pending id {pid}")
        view = _stopped_view(
            discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"nubtk:pend:approve:{pid}"),
            discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id=f"nubtk:pend:reject:{pid}"),
        )
        ch = self.bot.channel_for("admin_channel")
        if ch:
            try:
                await ch.send(embed=embed, view=view)
            except discord.HTTPException as e:
                log.warning("Could not post pending: %s", e)
        append_audit(self.s, "pending", pid=pid, **data)
        return pid

    def pending_view(self, pid):
        return _stopped_view(
            discord.ui.Button(label="Approve", style=discord.ButtonStyle.success, custom_id=f"nubtk:pend:approve:{pid}"),
            discord.ui.Button(label="Reject", style=discord.ButtonStyle.danger, custom_id=f"nubtk:pend:reject:{pid}"),
        )

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("nubtk:pend:"):
            return
        _, _, action, pid = cid.split(":", 3)
        await self.handle_pending_action(interaction, action, pid)

    async def handle_pending_action(self, interaction: discord.Interaction, action: str, pid: str):
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only the teacher/admin can do this.", ephemeral=True)
            return
        item = self.bot.pending.get(pid)
        if not item:
            await interaction.response.edit_message(view=None)
            await interaction.followup.send("This request was already handled.", ephemeral=True)
            return
        await interaction.response.defer()
        guild = interaction.guild
        member = guild.get_member(int(item["discord_id"]))
        if member is None:
            result, color = "Member has left the server.", RED
        elif action == "approve" and item.get("type") == "extra":
            ok, text = await self.add_extra_sections(guild, member, [item["section"]], by=f"admin:{interaction.user.name}")
            result, color = ("Approved ✅ — " + text if ok else f"Failed: {text}"), (GREEN if ok else RED)
        elif action == "approve":
            ok, text = await self.apply_verification(guild, member, item["section"], item["student_id"],
                                                     item["name"], by=f"admin:{interaction.user.name}")
            result, color = ("Approved ✅" if ok else f"Failed: {text}"), (GREEN if ok else RED)
        else:
            try:
                await member.send(f"❌ Your verification request on **{guild.name}** was not approved. "
                                  "Please contact the teacher (wrong section or ID?).")
            except discord.HTTPException:
                pass
            result, color = "Rejected ❌", RED
            append_audit(self.s, "rejected", pid=pid, by=interaction.user.name, **item)
        self.bot.pending.pop(pid)
        embed = interaction.message.embeds[0] if interaction.message.embeds else discord.Embed()
        embed.color = color
        embed.add_field(name="Result", value=f"{result} — by {interaction.user.mention}", inline=False)
        try:
            await interaction.message.edit(embed=embed, view=None)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------- extra sections (student in 3D AND 4B …)
    async def add_extra_sections(self, guild, member, secs, by="admin") -> tuple:
        rec = self.bot.store.get(member.id)
        if not rec:
            return False, "This member is not verified yet."
        secs = [x for x in secs if x in self.s.sections and x != rec["section"]]
        if not secs:
            return False, "Nothing to add (already their main section?)."
        extras = list(dict.fromkeys([*(rec.get("extra_sections") or []), *secs]))
        roles = [r for r in (self.bot.role_for(f"role:sec:{x}") for x in secs) if r]
        try:
            await member.add_roles(*roles, reason=f"Extra section(s) {', '.join(secs)} ({by})")
        except discord.Forbidden:
            return False, "Bot permission problem (move the bot role to the top)."
        courses = sorted({self.s.courses[c]["code"] for x in [rec["section"], *extras] for c in self.s.section_courses(x)})
        self.bot.store.update(member.id, extra_sections=extras, courses=courses, updated_at=self.s.now_str())
        append_audit(self.s, "extra_sections_added", discord_id=str(member.id), sections=secs, by=by)
        await self.bot.send_log("➕ Extra section added", f"{member.mention} → **{', '.join(secs)}** "
                                f"(main: {rec['section']}) · by `{by}`", GREEN)
        try:
            await member.send(embed=discord.Embed(
                title="➕ You got access to another section",
                description=f"Server: **{guild.name}**\nNow also in: **{', '.join(secs)}** "
                            f"({'; '.join(self.s.section_courses_text(x) for x in secs)})",
                color=GREEN))
        except discord.HTTPException:
            pass
        return True, f"{member.display_name}: main **{rec['section']}** + extra **{', '.join(extras)}**"

    async def remove_extra_sections(self, guild, member, secs, by="admin") -> tuple:
        rec = self.bot.store.get(member.id)
        if not rec:
            return False, "This member is not verified."
        extras = [x for x in (rec.get("extra_sections") or []) if x not in secs]
        roles = [r for r in (self.bot.role_for(f"role:sec:{x}") for x in secs) if r and r in member.roles
                 and x != rec["section"]]
        try:
            if roles:
                await member.remove_roles(*roles, reason=f"Extra section removed ({by})")
        except discord.Forbidden:
            return False, "Bot permission problem."
        courses = sorted({self.s.courses[c]["code"] for x in [rec["section"], *extras] for c in self.s.section_courses(x)})
        self.bot.store.update(member.id, extra_sections=extras, courses=courses, updated_at=self.s.now_str())
        append_audit(self.s, "extra_sections_removed", discord_id=str(member.id), sections=secs, by=by)
        return True, f"{member.display_name}: main **{rec['section']}**" + (f" + extra **{', '.join(extras)}**" if extras else "")

    def extra_request_view(self):
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="I also take a course in another section", emoji="➕",
                                     style=discord.ButtonStyle.secondary, custom_id="nubtk:extra:request"))
        v.stop()
        return v

    @commands.Cog.listener("on_interaction")
    async def _extra_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if cid == "nubtk:extra:request":  # old button (removed) — only the teacher gives extra sections now
            await interaction.response.send_message(
                "Only the teacher can give access to another section. Please tell your teacher.", ephemeral=True)
            return
            rec = self.bot.store.get(interaction.user.id)
            if not rec:
                await interaction.response.send_message("Please verify first (welcome channel).", ephemeral=True)
                return
            have = {rec["section"], *(rec.get("extra_sections") or [])}
            opts = [discord.SelectOption(label=f"Section {x}", value=x, description=self.s.section_courses_text(x)[:100])
                    for x in self.s.sections if x not in have]
            if not opts:
                await interaction.response.send_message("You already have every section.", ephemeral=True)
                return
            v = discord.ui.View(timeout=300)
            sel = discord.ui.Select(placeholder="Which other section do you take a course in?", options=opts[:25],
                                    custom_id="nubtk:extra:pick")
            v.add_item(sel)
            v.stop()
            await interaction.response.send_message(
                f"Your main section is **{rec['section']}**. Choose the other section — the teacher will approve it.",
                view=v, ephemeral=True)
        elif cid == "nubtk:extra:pick":
            rec = self.bot.store.get(interaction.user.id)
            sec = (interaction.data.get("values") or [None])[0]
            if not rec or sec not in self.s.sections:
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)
                return
            if self.bot.pending.find_by_member(interaction.user.id):
                await interaction.response.edit_message(content="⏳ You already have a request waiting.", view=None)
                return
            member = interaction.guild.get_member(interaction.user.id)
            await self.create_pending(interaction.guild, member, sec, rec["student_id"], rec["name"],
                                      [f"Wants EXTRA section {sec} (main: {rec['section']}) — e.g. retake / improvement"],
                                      "extra-request")
            item = self.bot.pending.find_by_member(interaction.user.id)
            if item:
                item[1]["type"] = "extra"
                self.bot.pending.save()
            await interaction.response.edit_message(
                content=f"✅ Request sent: **{sec}**. You'll get a DM when the teacher approves it.", view=None)

    # ---------------------------------------------------------- ID channel (typed messages)
    @staticmethod
    def _msg_text(m: discord.Message) -> str:
        text = m.content or ""
        for e in m.embeds:
            text += " ".join(filter(None, [e.title, e.description] + [f.value for f in e.fields]))
        return text

    def _join_channels(self):
        return [c for c in (self.bot.channel_for("welcome_channel"), self.bot.channel_for("id_channel")) if c]

    async def cleanup_id_channel(self, guild, member):
        """Remove a member's own messages + the bot's messages about them from #welcome and #id-name-section
        (pinned instructions are never touched). Called after verification and when someone leaves."""
        mentions = (f"<@{member.id}>", f"<@!{member.id}>")

        def check(m: discord.Message):
            if m.pinned:
                return False
            if m.author.id == member.id:
                return True
            return m.author.id == self.bot.user.id and any(x in self._msg_text(m) for x in mentions)

        total = 0
        for ch in self._join_channels():
            try:
                total += len(await ch.purge(limit=300, check=check))
            except discord.HTTPException as e:
                log.warning("purge failed in %s: %s", ch, e)
        return total

    async def sweep_join_channels(self, everything: bool = False) -> int:
        """Clean #welcome and #id-name-section: delete messages of / about people who are already verified
        or have left (everything=True: also those still waiting). Pinned messages are never touched;
        the teacher's own messages stay."""
        g = self.bot.main_guild()
        if not g:
            return 0
        mention_re = re.compile(r"<@!?(\d+)>")

        def done(uid: int) -> bool:
            if everything:
                return True
            m = g.get_member(uid)
            return m is None or self.is_verified(m)

        def check(m: discord.Message):
            if m.pinned or m.type in (discord.MessageType.pins_add, discord.MessageType.new_member):
                return not m.pinned
            if m.author.id == self.bot.user.id:
                ids = [int(x) for x in mention_re.findall(self._msg_text(m))]
                return bool(ids) and all(done(x) for x in ids)
            if m.author.bot:
                return False
            mem = g.get_member(m.author.id)
            if mem is not None and self.bot.is_admin(mem):
                return False
            return done(m.author.id)

        total = 0
        for ch in self._join_channels():
            try:
                total += len(await ch.purge(limit=1000, check=check))
            except discord.HTTPException as e:
                log.warning("sweep failed in %s: %s", ch, e)
        return total

    async def process_id_message(self, message: discord.Message, is_edit=False) -> bool:
        if message.author.bot or not self._main(message.guild):
            return False
        member = message.guild.get_member(message.author.id)
        if member is None or self.bot.is_admin(member):
            return False
        try:  # hide it at once: only the student (DM) and the teacher (log) see what was written
            await message.delete()
        except discord.HTTPException:
            pass
        if self.is_verified(member):
            return False

        sec, sid, name = parse_verification_text(message.content, list(self.s.sections), self.s.id_length)
        error = None
        if not sec:
            error = f"Section missing. Write one of: {', '.join(f'`{x}`' for x in self.s.sections)}"
        elif not sid:
            error = f"A valid **{self.s.id_length}-digit Student ID** is missing."
        elif not name:
            error = "Your **full official name** is missing."
        if error:
            await self._reply_error(message, error)
            return False

        result = await self.verify_request(message.guild, member, sec, sid, name, source="message")
        if result.status == "verified":
            return True
        await self._private_reply(message.channel, member, embed=result.embed())
        return False

    _private_results: dict = {}

    async def _private_reply(self, channel, user, embed: discord.Embed):
        """Answer only this student: DM. If DMs are closed, a short notice with a button that shows the
        answer only to them (the notice disappears after 3 minutes and contains no personal data)."""
        try:
            await user.send(embed=embed)
            return
        except discord.HTTPException:
            pass
        self._private_results[user.id] = embed
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label="See my result (only you)", emoji="🔒", style=discord.ButtonStyle.primary,
                                     custom_id=f"nubtk:idres:{user.id}"))
        v.stop()
        try:
            await channel.send(f"{user.mention} I couldn't DM you — click the button to see your answer.",
                               view=v, delete_after=180)
        except discord.HTTPException:
            pass

    @commands.Cog.listener("on_interaction")
    async def _idres_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("nubtk:idres:"):
            return
        if str(interaction.user.id) != cid.split(":")[-1]:
            await interaction.response.send_message("This button is for another student.", ephemeral=True)
            return
        emb = self._private_results.get(interaction.user.id)
        await interaction.response.send_message(embed=emb or discord.Embed(description="Nothing to show any more."),
                                                ephemeral=True)

    async def _reply_error(self, message, error):
        example = f"`{list(self.s.sections)[0]} 11250121964 Your Full Name`"
        emb = discord.Embed(title="⚠️ Verification problem", color=ORANGE,
                            description=(f"{error}\n\nYou wrote: `{(message.content or '')[:200]}`\n"
                                         f"Correct format: {example}\n\n"
                                         "Easiest way: click your **section button** in the welcome channel."))
        await self._private_reply(message.channel, message.author, embed=emb)
        await self.bot.send_log("⚠️ Verification message problem",
                                f"{message.author.mention}: {error}\n```{message.content[:800]}```", ORANGE)

    async def scan_id_channel(self, reply_errors=False) -> tuple:
        guild = self.bot.main_guild()
        ch = self.bot.channel_for("id_channel")
        if not guild or not ch:
            return 0, 0
        latest = {}
        try:
            async for m in ch.history(limit=500, oldest_first=False):
                if m.author.bot:
                    continue
                member = guild.get_member(m.author.id)
                if member is None or self.bot.is_admin(member):
                    continue
                if self.is_verified(member) or member.id in latest:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
                    continue
                latest[member.id] = m
        except discord.HTTPException as e:
            log.warning("scan failed: %s", e)
        verified = errors = 0
        for m in latest.values():
            if await self.process_id_message(m, is_edit=not reply_errors):
                verified += 1
            else:
                errors += 1
        return verified, errors

    # ---------------------------------------------------------- events
    @commands.Cog.listener()
    async def on_ready(self):
        if self._scanned:
            return
        self._scanned = True
        await asyncio.sleep(3)
        flag = DATA_DIR / ".repost_welcome"
        vfile = DATA_DIR / ".welcome_version"
        try:
            old_v = vfile.read_text().strip()
        except OSError:
            old_v = ""
        if old_v != WELCOME_VERSION:
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("1")
            try:
                vfile.write_text(WELCOME_VERSION)
            except OSError:
                pass
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
            g = self.bot.main_guild()
            if g:
                await self.post_welcome_messages(g)
                await self.bot.send_log("📌 Welcome messages re-posted", "New version / config.json was updated.")
        v, e = await self.scan_id_channel(reply_errors=False)
        n = await self.sweep_join_channels()
        if n:
            log.info("Join channels cleaned: %s old message(s) removed", n)
        if v or e:
            await self.bot.send_log("🕒 Offline messages processed", f"Verified: **{v}** · Not valid yet: **{e}**")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.type == discord.MessageType.new_member and message.channel.id in {c.id for c in self._join_channels()}:
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            return
        ch = self.bot.channel_for("id_channel")
        if ch and message.channel.id == ch.id and not message.content.startswith(self.s.prefix):
            await self.process_id_message(message)

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        ch = self.bot.channel_for("id_channel")
        if not ch or payload.channel_id != ch.id:
            return
        try:
            msg = await ch.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        await self.process_id_message(msg, is_edit=True)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.bot or not self._main(member.guild):
            return
        rec = self.bot.store.get(member.id)
        if rec and rec.get("section") in self.s.sections:
            ok, _ = await self.apply_verification(member.guild, member, rec["section"], rec["student_id"],
                                                  rec["name"], by="auto-rejoin")
            await self.bot.send_log("🔁 Student re-joined", f"{member.mention} restored to **{rec['section']}**"
                                    if ok else f"Could not restore {member.mention}", GREEN if ok else ORANGE)
            return

        wc = self.bot.channel_for("welcome_channel")
        idc = self.bot.channel_for("id_channel")
        if wc:
            try:
                await wc.send(f"👋 Welcome {member.mention}! Click your **section button** above ☝️ "
                              "and fill the form (Student ID + full name) to unlock your class channels.",
                              delete_after=self.s.welcome_ping_delete_after or None)
            except discord.HTTPException:
                pass
        try:
            await member.send(embed=discord.Embed(
                title=f"👋 Welcome to {member.guild.name}!",
                description=(f"1️⃣ Open {wc.mention if wc else '#welcome'} and click your **section** button.\n"
                             "2️⃣ Write your **Student ID** and **full official name** in the form.\n"
                             f"Button not working? Type in {idc.mention if idc else '#id-name-section'}:\n"
                             f"`{list(self.s.sections)[0]} 11250121964 Your Full Name`"),
                color=BLUE))
        except discord.HTTPException:
            pass
        await self.bot.send_log("👤 New member joined", f"{member.mention} · `{member.name}` — waiting for verification")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not self._main(member.guild):
            return
        rec = self.bot.store.get(member.id)
        if rec:
            self.bot.store.update(member.id, status="left", left_at=self.s.now_str())
            append_audit(self.s, "left", discord_id=str(member.id), section=rec.get("section"))
        p = self.bot.pending.find_by_member(member.id)
        if p:
            self.bot.pending.pop(p[0])
        desc = f"`{member.name}`" + (f" · {rec['section']} · `{rec['student_id']}` {rec['name']}" if rec else " (not verified)")
        await self.bot.send_log("🚪 Member left", desc, ORANGE)
        await self.cleanup_id_channel(member.guild, member)

    # ---------------------------------------------------------- welcome / instructions posts
    def welcome_embed(self, guild) -> discord.Embed:
        s = self.s
        idc = self.bot.channel_for("id_channel")
        ex = list(s.sections)[0]
        lines = "\n".join(f"**{sec}** → {s.section_courses_text(sec)}" + (" · *Evening*" if s.is_evening(sec) else "")
                          for sec in s.sections)
        e = discord.Embed(
            title=f"👋 Welcome to {guild.name}!",
            description=(f"Official class server of **{s.teacher}** · {s.semester}\n\n"
                         "## ✅ How to join (3 easy steps)\n"
                         "**1.** Click **your section** button below 👇\n"
                         "**2.** A small form will open. Write:\n"
                         f"  • Your **Student ID** ({s.id_length} digits), e.g. `11250121964`\n"
                         "  • Your **full name** (official, in English), e.g. `Shovon Mandal`\n"
                         "**3.** Click **Submit**. Done! 🎉 Your class channels will open right away.\n\n"
                         f"⚠️ Button not working? Go to {idc.mention if idc else '#id-name-section'} "
                         f"and type in one line:\n`{ex} 11250121964 Your Full Name`"),
            color=BLUE)
        e.add_field(name="📚 Sections and courses", value=lines[:1024], inline=False)
        e.set_footer(text="Clicked the wrong section? Tell your teacher. · Your nickname will be: last4ID_SECTION_Name")
        return e

    def id_embed(self) -> discord.Embed:
        ex = list(self.s.sections)[0]
        return discord.Embed(
            title="📝 Verify here — 2 easy ways",
            description=("**Way 1 (easy):** click your **section button** below 👇 → write your "
                         "**Student ID** (11 digits) and **full name** → **Submit**. Done! 🎉\n\n"
                         "**Way 2:** send **one message** here with your section, Student ID and full name:\n"
                         f"`{ex} 11250121964 John Doe`\n`John Doe {ex} 11250121964`\n\n"
                         "🔒 Your message is hidden right away — only you (by DM) and the teacher see it. "
                         "The answer comes in your **DM**."),
            color=GOLD)

    async def _post_pinned(self, channel, only_pinned=False, **kwargs):
        try:
            async for m in channel.history(limit=50):
                if m.author.id == self.bot.user.id and (m.pinned or not only_pinned):
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
            msg = await channel.send(**kwargs)
            await msg.pin()
            async for m in channel.history(limit=5):
                if m.type == discord.MessageType.pins_add:
                    try:
                        await m.delete()
                    except discord.HTTPException:
                        pass
            return msg
        except discord.HTTPException as e:
            log.warning("Could not post in %s: %s", channel, e)
            return None

    async def post_welcome_messages(self, guild):
        wc = self.bot.channel_for("welcome_channel")
        idc = self.bot.channel_for("id_channel")
        adm = self.bot.channel_for("admin_channel")
        if wc:
            await self._post_pinned(wc, embed=self.welcome_embed(guild), view=SectionView(self.s))
        if idc:
            await self._post_pinned(idc, only_pinned=True, embed=self.id_embed(), view=SectionView(self.s))
        panel = self.bot.get_cog("Panel")
        if adm and panel:
            await panel.post_panel(adm)
        await self.post_section_welcomes(guild)

    # ---- instructor card (shown in every announcement channel) ----
    DETAIL_KEYS = ("name", "initial", "designation", "department")
    CONTACT_LABELS = {"email": "📧 E-mail", "e-mail": "📧 E-mail", "mail": "📧 E-mail",
                      "discord": "💬 Discord", "whatsapp": "📱 WhatsApp", "phone": "📞 Phone",
                      "mobile": "📞 Mobile", "website": "🌐 Website", "web": "🌐 Website",
                      "office": "🏢 Office", "room": "🏢 Room", "facebook": "📘 Facebook",
                      "linkedin": "💼 LinkedIn", "counseling": "🕒 Counseling hours"}

    def _instructor(self) -> dict:
        """Instructor block from config.json, keys made case-insensitive."""
        return {str(k).strip().lower(): str(v).strip()
                for k, v in (self.s.raw.get("instructor", {}) or {}).items() if str(v).strip()}

    def _add_instructor_fields(self, e: discord.Embed):
        ins = self._instructor()
        details = "\n".join(f"**{k.title()}:** {ins[k]}" for k in self.DETAIL_KEYS if k in ins)
        contact = "\n".join(f"**{self.CONTACT_LABELS.get(k, k.replace('_', ' ').title())}:** {v}"
                             for k, v in ins.items() if k not in self.DETAIL_KEYS)
        if details:
            e.add_field(name="👨‍🏫 Instructor details", value=details[:1024], inline=True)
        if contact:
            e.add_field(name="📞 Contact information", value=contact[:1024], inline=True)
        e.add_field(name="\u200b", value="If you have any query, feel free to ask. 😊", inline=False)

    def _intro_line(self) -> str:
        ins = self._instructor()
        name = ins.get("name", self.s.teacher)
        initial = f" ({ins['initial']})" if ins.get("initial") else ""
        return f"I am **{name}{initial}**, your course instructor. 🙂"

    def _invite_block(self, courses_text: str, invite_url: str, sections=None) -> str:
        """Who should be invited: course + section(s)."""
        who = f"who take {courses_text}" if courses_text else "who take my courses"
        secs = list(sections or [])
        where = (f"Section {secs[0]}" if len(secs) == 1 else "Sections " + ", ".join(secs)) if secs else ""
        return (f"**👥 Invite your classmates {who}" + (f" — {where}" if where else "") + "**\n"
                f"Share this link with them:\n```{invite_url or 'ask the teacher for the link'}```")

    def _codes(self, courses) -> str:
        """['CSE2109','CSE2110'] → 'CSE 2109 / CSE 2110 (OOP II)'"""
        theory = self.s.theory_course_for(courses[0]) if courses else None
        name = f" ({self.s.courses[theory]['short']})" if theory else ""
        return " / ".join(self.s.courses[c]["code"] for c in courses) + name

    def section_welcome_embed(self, sec: str, invite_url: str) -> discord.Embed:
        s = self.s
        courses = "\n".join(f"• **{s.courses[c]['code']}** — {s.courses[c]['title']}" for c in s.section_courses(sec))
        evening = " (Evening)" if s.is_evening(sec) else ""
        e = discord.Embed(
            title=f"👋 Welcome to Section {sec}{evening}!",
            description=(f"**Your courses:**\n{courses}\n\n"
                         "This server is where we communicate about everything related to these courses — "
                         "announcements, resources, marks, presentations and lab work.\n\n"
                         f"{self._intro_line()}\n\n" + self._invite_block(self._codes(s.section_courses(sec)), invite_url, [sec])),
            color=discord.Color(s.section_color(sec)))
        self._add_instructor_fields(e)
        e.set_footer(text=f"{s.semester} · Section {sec}")
        return e

    def group_welcome_embed(self, theory: str, group: dict, invite_url: str) -> discord.Embed:
        s = self.s
        courses = "\n".join(f"• **{s.courses[c]['code']}** — {s.courses[c]['title']}" for c in group["courses"])
        e = discord.Embed(
            title=f"👋 Welcome to {s.codes_label(group['courses'])} · {s.courses[theory]['short']}!",
            description=(f"**Courses:**\n{courses}\n**Sections:** {', '.join(group['sections'])}\n\n"
                         "This category is shared by all sections of this course — common announcements "
                         "and resources are posted here.\n\n"
                         f"{self._intro_line()}\n\n" + self._invite_block(self._codes(group["courses"]), invite_url, group["sections"])),
            color=discord.Color.blurple())
        self._add_instructor_fields(e)
        e.set_footer(text=f"{s.semester} · {', '.join(group['sections'])}")
        return e

    def all_welcome_embed(self, invite_url: str) -> discord.Embed:
        s = self.s
        lines = "\n".join(f"• **{sec}** — {s.section_courses_text(sec)}" + (" (Evening)" if s.is_evening(sec) else "")
                           for sec in s.sections)
        e = discord.Embed(
            title=f"👋 Welcome to {s.server_name}!",
            description=(f"**All sections & courses:**\n{lines}\n\n"
                         "This server is where we communicate about everything related to our courses.\n\n"
                         f"{self._intro_line()}\n\n" + self._invite_block("", invite_url, list(s.sections))),
            color=discord.Color.gold())
        self._add_instructor_fields(e)
        e.set_footer(text=s.semester)
        return e

    async def post_section_welcomes(self, guild):
        url = await self.bot.get_invite_url()
        for sec in self.s.sections:
            ch = self.bot.channel_for(f"sec:{sec}:announcement")
            if ch:
                msg = await self._post_pinned(ch, only_pinned=True, embed=self.section_welcome_embed(sec, url))
                if msg:
                    self.bot.ids.set(f"sec:{sec}:welcome_msg", msg.id)
        for theory, group in self.s.course_groups().items():
            ch = self.bot.channel_for(f"group:{theory}:announcement")
            if ch:
                await self._post_pinned(ch, only_pinned=True, embed=self.group_welcome_embed(theory, group, url))
        ch = self.bot.channel_for("all:notice")
        if ch:
            await self._post_pinned(ch, only_pinned=True, embed=self.all_welcome_embed(url))

    def section_link_url(self, sec: str) -> str:
        """URL that opens the section's announcement channel at the pinned welcome card."""
        g = self.bot.main_guild()
        ch = self.bot.channel_for(f"sec:{sec}:announcement")
        if not g or not ch:
            return ""
        mid = self.bot.ids.get(f"sec:{sec}:welcome_msg")
        return f"https://discord.com/channels/{g.id}/{ch.id}" + (f"/{mid}" if mid else "")

    def go_view(self, sec: str):
        url = self.section_link_url(sec)
        if not url:
            return None
        v = discord.ui.View(timeout=None)
        v.add_item(discord.ui.Button(label=f"Open Section {sec} announcements", emoji="👉",
                                     style=discord.ButtonStyle.link, url=url))
        return v

    def section_welcome_link(self, sec: str) -> str:
        g = self.bot.main_guild()
        ch = self.bot.channel_for(f"sec:{sec}:announcement")
        mid = self.bot.ids.get(f"sec:{sec}:welcome_msg")
        if g and ch and mid:
            return f"https://discord.com/channels/{g.id}/{ch.id}/{mid}"
        return ch.mention if ch else ""

    @commands.command(name="setup_welcome")
    async def setup_welcome(self, ctx):
        await self.post_welcome_messages(ctx.guild)
        await ctx.send("✅ Re-posted: welcome buttons, ID instructions, admin help and the instructor card in every announcement channel.")


async def setup(bot):
    await bot.add_cog(Verification(bot))
