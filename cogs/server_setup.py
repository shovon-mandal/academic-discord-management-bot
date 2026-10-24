"""!setup_server — builds (or re-syncs) the whole server from config.json.

Safe to run many times: existing channels/roles are reused (IDs are stored in
data/server_ids.json), only missing ones are created and permissions re-applied.
"""
from __future__ import annotations

import logging

import discord
from discord.ext import commands

log = logging.getLogger("nubtk.setup")
PO = discord.PermissionOverwrite


class ServerSetup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings

    # ------------------------------------------------------------ command
    async def _ask(self, ctx, title, text, label, run, danger=True):
        from cogs.panel import ConfirmView  # local import to avoid a cycle at load time

        async def go(interaction):
            await run(ctx)
        embed = discord.Embed(title=title, description=text,
                              color=discord.Color.red() if danger else discord.Color.orange())
        await ctx.send(embed=embed, view=ConfirmView(self, ctx.author.id, go, label=label, danger=danger, timeout=120))

    @commands.command(name="setup_server", aliases=["sync"])
    async def setup_server(self, ctx: commands.Context):
        await self._ask(
            ctx, "⚠️ Run server setup / sync?",
            "This will make the server match **config.json**:\n"
            "• create missing roles, categories and channels\n"
            "• rename channels and **reset their permissions** to the config\n"
            "• **delete channels that were removed from config.json** (their messages are lost)\n"
            "• re-post welcome buttons, instructor cards and this admin panel\n\n"
            "Students and their data are NOT affected.",
            "Yes, run setup", self._run_setup, danger=False)

    async def _run_setup(self, ctx: commands.Context):
        status = await ctx.send("⏳ Building / syncing server structure... (1–3 minutes)")
        try:
            report = await self.build(ctx.guild, status)
        except discord.Forbidden as e:
            await status.edit(content=f"❌ Permission problem: `{e}`\n"
                                      "Give the bot **Administrator** and drag the bot role to the TOP of the role list.")
            return
        await status.edit(content="✅ **Server setup complete!**\n" + report)
        await self.bot.send_log("🏗️ Server setup / sync done", report, discord.Color.green())

        ver = self.bot.get_cog("Verification")
        if ver:
            await ver.post_welcome_messages(ctx.guild)
        admin_ch = self.bot.channel_for("admin_channel")
        if admin_ch and admin_ch.id != ctx.channel.id:
            await ctx.send(f"➡️ From now on, use all admin commands here: {admin_ch.mention}")

    @commands.command(name="reset_server")
    async def reset_server(self, ctx: commands.Context, confirm: str = ""):
        """Delete EVERY channel, category and role, then build fresh from config.json."""
        await self._ask(
            ctx, "🛑 RESET the whole server?",
            "This **deletes ALL channels, categories and roles** in this server — every message is lost — "
            "and builds everything fresh from config.json.\n\n"
            "Students stay in the server and their records are kept, but they lose their roles and must "
            "verify again.\n\n**This cannot be undone.**",
            "Yes, delete everything", self._run_reset, danger=True)

    async def _run_reset(self, ctx: commands.Context):
        guild = ctx.guild
        await ctx.send("🧹 Deleting everything... the new admin channel will appear in 1–3 minutes.")
        for ch in list(guild.channels):
            if isinstance(ch, discord.CategoryChannel):
                continue
            try:
                await ch.delete(reason=f"reset_server by {ctx.author}")
            except discord.HTTPException as e:
                log.warning("Could not delete %s: %s", ch, e)
        for cat in list(guild.categories):
            try:
                await cat.delete(reason=f"reset_server by {ctx.author}")
            except discord.HTTPException as e:
                log.warning("Could not delete %s: %s", cat, e)
        for role in list(guild.roles):
            if role.is_default() or role.managed or role >= guild.me.top_role:
                continue
            try:
                await role.delete(reason=f"reset_server by {ctx.author}")
            except discord.HTTPException as e:
                log.warning("Could not delete role %s: %s", role, e)
        self.bot.ids.data.clear()
        self.bot.ids.save()
        try:
            report = await self.build(guild)
        except discord.Forbidden as e:
            log.error("reset_server build failed: %s", e)
            return
        ver = self.bot.get_cog("Verification")
        if ver:
            await ver.post_welcome_messages(guild)
        admin_ch = self.bot.channel_for("admin_channel")
        if admin_ch:
            await admin_ch.send(f"✅ **Fresh server ready!** {ctx.author.mention}\n" + report)
        await self.bot.send_log("🧹 Server reset + rebuilt", report, discord.Color.green())

    # ------------------------------------------------------------ overwrites
    def _bot_ow(self) -> PO:
        return PO(view_channel=True, send_messages=True, read_message_history=True, manage_messages=True,
                  embed_links=True, attach_files=True, add_reactions=True, connect=True, speak=True,
                  move_members=True, manage_channels=True)

    def _category_ow(self, guild, roles) -> dict:
        ow = {guild.default_role: PO(view_channel=False), guild.me: self._bot_ow()}
        for r in roles:
            ow[r] = PO(view_channel=True)
        return ow

    def _text_ow(self, guild, roles, student_write: bool) -> dict:
        ow = {guild.default_role: PO(view_channel=False), guild.me: self._bot_ow()}
        for r in roles:
            # Students: read history + react everywhere. Write / attach / threads ONLY where student_write=true
            ow[r] = PO(view_channel=True, read_message_history=True, add_reactions=True,
                       send_messages=student_write, attach_files=student_write, embed_links=student_write,
                       create_public_threads=student_write, send_messages_in_threads=student_write,
                       create_private_threads=False, use_external_emojis=True)
        return ow

    def _voice_ow(self, guild, roles) -> dict:
        ow = {guild.default_role: PO(view_channel=False), guild.me: self._bot_ow()}
        for r in roles:
            ow[r] = PO(view_channel=True, connect=True, speak=True, stream=True, use_voice_activation=True,
                       send_messages=False, add_reactions=True, read_message_history=True,
                       create_public_threads=False, create_private_threads=False, send_messages_in_threads=False)
        return ow

    # ------------------------------------------------------------ ensure helpers
    def _get(self, guild, key, cls):
        cid = self.bot.ids.get(key)
        ch = guild.get_channel(cid) if cid else None
        return ch if isinstance(ch, cls) else None

    async def _role(self, guild, key, name, color, hoist, stats):
        rid = self.bot.ids.get(key)
        role = guild.get_role(rid) if rid else None
        if role is None:
            role = discord.utils.get(guild.roles, name=name)
        if role is None:
            role = await guild.create_role(name=name, color=discord.Color(color), hoist=hoist,
                                           mentionable=False, reason="Class bot setup")
            stats["created"].append(f"role **{name}**")
        elif role.hoist != hoist or role.color.value != color:
            try:  # "Display role members separately" in the member list
                await role.edit(hoist=hoist, color=discord.Color(color), reason="Class bot sync")
                stats["updated"] += 1
            except discord.HTTPException:
                pass
        self.bot.ids.set(key, role.id)
        return role

    async def _category(self, guild, key, name, overwrites, stats):
        cat = self._get(guild, key, discord.CategoryChannel)
        if cat is None:
            cat = await guild.create_category(name, overwrites=overwrites, reason="Class bot setup")
            stats["created"].append(f"category **{name}**")
        elif cat.overwrites != overwrites or cat.name != name:
            await cat.edit(name=name, overwrites=overwrites, reason="Class bot sync")
            stats["updated"] += 1
        self.bot.ids.set(key, cat.id)
        return cat

    async def _text(self, guild, key, name, category, overwrites, stats, topic=None, slowmode=0):
        ch = self._get(guild, key, discord.TextChannel)
        if ch is None:
            kwargs = {"category": category, "overwrites": overwrites, "reason": "Class bot setup"}
            if topic:
                kwargs["topic"] = topic
            if slowmode:
                kwargs["slowmode_delay"] = slowmode
            ch = await guild.create_text_channel(name, **kwargs)
            stats["created"].append(f"#{name}")
        else:
            edits = {}
            if ch.name != name:
                edits["name"] = name
            if ch.overwrites != overwrites:
                edits["overwrites"] = overwrites
            if ch.category_id != category.id:
                edits["category"] = category
            if topic and ch.topic != topic:
                edits["topic"] = topic
            if slowmode and ch.slowmode_delay != slowmode:
                edits["slowmode_delay"] = slowmode
            if edits:
                await ch.edit(reason="Class bot sync", **edits)
                stats["updated"] += 1
        self.bot.ids.set(key, ch.id)
        return ch

    async def _voice(self, guild, key, name, category, overwrites, stats):
        ch = self._get(guild, key, discord.VoiceChannel)
        if ch is None:
            ch = await guild.create_voice_channel(name, category=category, overwrites=overwrites,
                                                  reason="Class bot setup")
            stats["created"].append(name)
        else:
            edits = {}
            if ch.name != name:
                edits["name"] = name
            if ch.overwrites != overwrites:
                edits["overwrites"] = overwrites
            if ch.category_id != category.id:
                edits["category"] = category
            if edits:
                await ch.edit(reason="Class bot sync", **edits)
                stats["updated"] += 1
        self.bot.ids.set(key, ch.id)
        return ch

    async def _channels_from_template(self, guild, prefix, templates, category, roles, stats, fmt):
        for tpl in templates:
            key = f"{prefix}:{tpl['key']}"
            if tpl.get("type") == "voice":
                await self._voice(guild, key, tpl["name"], category, self._voice_ow(guild, roles), stats)
            else:
                topic = tpl.get("topic", "")
                topic = topic.format(**fmt) if topic else None
                await self._text(guild, key, tpl["name"], category,
                                 self._text_ow(guild, roles, bool(tpl.get("student_write", False))), stats, topic=topic)

    # ------------------------------------------------------------ main build
    async def build(self, guild: discord.Guild, status_msg=None) -> str:
        s = self.s
        ch_cfg = s.channels
        stats = {"created": [], "updated": 0, "removed": []}
        everyone = guild.default_role
        wanted = set()  # channel keys that should exist after this run

        async def progress(text):
            if status_msg:
                try:
                    await status_msg.edit(content=f"⏳ {text}")
                except discord.HTTPException:
                    pass

        # 0) default @everyone permissions — students must not change the nickname the bot sets
        if s.lock_nickname:
            perms = everyone.permissions
            perms.update(change_nickname=False, mention_everyone=False)
            if perms != everyone.permissions:
                await everyone.edit(permissions=perms, reason="Students keep bot-assigned nicknames")
                stats["updated"] += 1

        # 1) roles
        await progress("Roles...")
        teacher = await self._role(guild, "role:teacher", s.raw.get("teacher_role", "Teacher"), 0xF1C40F, True, stats)
        sec_roles = {}
        for sec in s.sections:
            sec_roles[sec] = await self._role(guild, f"role:sec:{sec}", s.section_role_name(sec),
                                              s.section_color(sec), True, stats)
        verified = await self._role(guild, "role:verified", s.verified_role_name, 0x95A5A6, False, stats)

        # member list order (right sidebar): Teacher, Sec-2C, Sec-2D, ... then everyone else
        ordered = [teacher] + [sec_roles[x] for x in s.sections] + [verified]
        top = guild.me.top_role.position
        positions = {r: max(1, top - i - 1) for i, r in enumerate(ordered)}
        if any(r.position != p for r, p in positions.items()):
            try:
                await guild.edit_role_positions(positions, reason="Class bot: role-wise member list")
            except discord.HTTPException as e:
                log.warning("Could not reorder roles: %s", e)

        # give the Teacher role to the owner + ADMIN_IDS
        for uid in {guild.owner_id, *s.admin_ids}:
            m = guild.get_member(uid)
            if m and teacher not in m.roles:
                try:
                    await m.add_roles(teacher, reason="Teacher / admin")
                except discord.HTTPException:
                    pass

        # 2) welcome area: visible to everyone, hidden once a member gets the Verified role
        await progress("Welcome area...")
        bot_ow = self._bot_ow()
        welcome_ow = {
            everyone: PO(view_channel=True, send_messages=False, read_message_history=True, add_reactions=True,
                         create_public_threads=False, create_private_threads=False, send_messages_in_threads=False),
            verified: PO(view_channel=False),
            guild.me: bot_ow,
        }
        id_ow = dict(welcome_ow)
        id_ow[everyone] = PO(view_channel=True, send_messages=True, read_message_history=True, add_reactions=True,
                             create_public_threads=False, create_private_threads=False, send_messages_in_threads=False,
                             attach_files=False, embed_links=False)
        wcat = await self._category(guild, "welcome_category", ch_cfg["welcome_category"], welcome_ow, stats)
        await self._text(guild, "welcome_channel", ch_cfg["welcome"], wcat, welcome_ow, stats,
                         topic="Start here → click your section button.")
        await self._text(guild, "id_channel", ch_cfg["id_channel"], wcat, id_ow, stats,
                         topic="Button not working? Type: SECTION STUDENT_ID FULL_NAME  (e.g. 2C 11250121964 John Doe)",
                         slowmode=s.id_slowmode)

        # 3) all students
        await progress("All-students area...")
        acat = await self._category(guild, "all:category", ch_cfg["all_students_category"],
                                    self._category_ow(guild, [verified]), stats)
        wanted.update(f"all:{t['key']}" for t in ch_cfg.get("all_students", []))
        await self._channels_from_template(guild, "all", ch_cfg.get("all_students", []), acat, [verified], stats,
                                           {"section": "all", "courses": "all"})

        # 4) course groups (e.g. CSE 1205/1206 for 2C + 2D)
        if s.course_group_categories:
            for theory, group in s.course_groups().items():
                if len(group["sections"]) < 1:
                    continue
                await progress(f"Course group {s.courses[theory]['code']}...")
                roles = [sec_roles[x] for x in group["sections"]]
                wanted.update(f"group:{theory}:{t['key']}" for t in ch_cfg.get("course_group", []))
                gcat = await self._category(guild, f"group:{theory}:category",
                                            s.course_group_category_name(theory, group),
                                            self._category_ow(guild, roles), stats)
                await self._channels_from_template(
                    guild, f"group:{theory}", ch_cfg.get("course_group", []), gcat, roles, stats,
                    {"section": ", ".join(group["sections"]), "courses": s.codes_label(group["courses"])})

        # 5) sections
        for sec in s.sections:
            await progress(f"Section {sec}...")
            role = sec_roles[sec]
            cat = await self._category(guild, f"sec:{sec}:category", s.section_category_name(sec),
                                       self._category_ow(guild, [role]), stats)
            templates = list(ch_cfg.get("section", []))
            if s.section_has_lab(sec):
                templates += list(ch_cfg.get("lab", []))
            templates += list(ch_cfg.get("section_voice", []))
            wanted.update(f"sec:{sec}:{t['key']}" for t in templates)
            await self._channels_from_template(guild, f"sec:{sec}", templates, cat, [role], stats,
                                               {"section": sec, "courses": s.section_courses_text(sec)})

        # 6) admin area — only admins (Administrator permission) + bot
        await progress("Admin area...")
        admin_ow = {everyone: PO(view_channel=False), guild.me: bot_ow}
        for uid in s.admin_ids:
            m = guild.get_member(uid)
            if m:
                admin_ow[m] = PO(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        admcat = await self._category(guild, "admin_category", ch_cfg["admin_category"], admin_ow, stats)
        await self._text(guild, "admin_channel", ch_cfg["admin"], admcat, admin_ow, stats,
                         topic="Admin commands. Type !help")
        await self._text(guild, "log_channel", ch_cfg["logs"], admcat, admin_ow, stats,
                         topic="Automatic bot logs")

        # 7) remove channels the bot created earlier that are no longer in config.json
        await progress("Cleaning old channels...")
        for key in list(self.bot.ids.data):
            if (not key.startswith(("sec:", "group:", "all:")) or key.endswith((":category", ":welcome_msg"))
                    or key in wanted):
                continue
            ch = guild.get_channel(self.bot.ids.get(key))
            if ch is not None:
                try:
                    await ch.delete(reason="Removed from config.json")
                    stats["removed"].append(ch.name)
                except discord.HTTPException as e:
                    log.warning("Could not delete %s: %s", ch, e)
                    continue
            self.bot.ids.data.pop(key, None)
        self.bot.ids.save()

        created = stats["created"]
        lines = [
            f"• Created: **{len(created)}** · Updated: **{stats['updated']}** · Removed: **{len(stats['removed'])}**",
            f"• Sections: {', '.join(s.sections)}",
            f"• Admin: {self.bot.channel_for('admin_channel').mention}",
        ]
        if created:
            preview = ", ".join(created[:25]) + (" …" if len(created) > 25 else "")
            lines.append(f"• New: {preview}")
        return "\n".join(lines)


async def setup(bot):
    await bot.add_cog(ServerSetup(bot))
