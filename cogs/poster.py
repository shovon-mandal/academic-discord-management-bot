"""📤 Post as bot — the teacher writes, the bot posts.

Panel button "📤 Post as bot":
  1. pick channel(s) (many), role(s) (optional), mention mode, style (plain / card)
  2. ✍️ Write → text (any Discord formatting), optional title, files (up to 10), link buttons
  3. preview (only you see it) → ✅ Post now → posted in every selected channel

Text command (good for big files from a PC):
  !post #channel1 #channel2 @Role your message      (attach files to the same message)
  !post #channel1 audience your message             (mention the roles that can see each channel)
  !post #channel1 buttons your message              (every link → a button, link written under it)
  (keywords can be combined: audience buttons card)

Link buttons: one per line in the "Buttons" box, or inside the text:
  Attendance sheet | https://docs.google.com/...
  [[Open slides | https://...]]
"""
from __future__ import annotations

import io
import logging
import re

import discord
from discord.ext import commands

log = logging.getLogger("nubtk.poster")

URL_RE = re.compile(r"^https?://\S+$")
INLINE_BTN = re.compile(r"\[\[\s*([^|\]]+?)\s*\|\s*(https?://[^\]\s]+)\s*\]\]")
MENTION_MODES = [
    ("none", "No mention", "🔕", "Just post the message"),
    ("roles", "Mention the roles I select", "🏷️", "Pings the roles chosen above"),
    ("audience", "Mention who can see each channel", "👥", "e.g. 3D channel → @3D role (automatic per channel)"),
    ("both", "Both: selected roles + channel's roles", "📣", "Selected roles and each channel's own roles"),
]


def parse_buttons(text: str) -> tuple:
    """→ (clean_text, [(label, url)]) — takes [[Label | url]] out of the text."""
    btns = [(m.group(1)[:80], m.group(2)) for m in INLINE_BTN.finditer(text or "")]
    clean = INLINE_BTN.sub("", text or "").strip()
    return clean, btns


def parse_button_lines(text: str) -> tuple:
    """'Label | url' per line → ([(label, url)], [bad lines])."""
    out, bad = [], []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        m = INLINE_BTN.fullmatch(line)
        if m:
            out.append((m.group(1)[:80], m.group(2)))
            continue
        if "|" in line:
            label, url = (x.strip() for x in line.split("|", 1))
        elif " " in line and URL_RE.match(line.split()[-1]):
            label, url = line.rsplit(" ", 1)
        else:
            label, url = "Open link", line
        if URL_RE.match(url):
            out.append(((label or "Open link")[:80], url))
        else:
            bad.append(line)
    return out, bad


def audience_roles(channel) -> list:
    """Roles that can see a channel (students of that section / course), not admin or bot roles."""
    def from_ow(ow):
        return [t for t, o in ow.items() if isinstance(t, discord.Role) and o.view_channel
                and not t.is_default() and not t.managed and not t.permissions.administrator]
    roles = from_ow(channel.overwrites)
    if not roles and getattr(channel, "category", None):
        roles = from_ow(channel.category.overwrites)
    everyone = channel.guild.default_role
    ow = channel.overwrites_for(everyone)
    if not roles and ow.view_channel is not False and channel.permissions_for(everyone).view_channel:
        return [everyone]
    return roles


class Draft:
    def __init__(self):
        self.channels: list = []     # channel ids
        self.roles: list = []        # role ids
        self.mention = "none"
        self.style = "plain"
        self.text = ""
        self.title = ""
        self.buttons: list = []      # (label, url)
        self.files: list = []        # (filename, bytes)
        self.linkbuttons = False     # every link in the text → a button (link shown under it)


class Poster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_ready(self):
        """Make sure the admin-only 🧪┃bot-test channel exists (to try posts before sending to students)."""
        if getattr(self, "_ready", False):
            return
        self._ready = True
        if self.bot.channel_for("test_channel"):
            return
        adm = self.bot.channel_for("admin_channel")
        if adm is None:
            return
        name = (self.bot.settings.raw.get("channels", {}) or {}).get("test", "🧪┃bot-test")
        existing = discord.utils.get(adm.guild.text_channels, name=name)
        try:
            ch = existing or await adm.clone(name=name, reason="Admin-only test channel")
            if not existing:
                await ch.edit(topic="Only admins see this — try 📤 Post as bot, files, buttons here first")
                await ch.send("🧪 **Test channel** — only admins can see this. Choose it in **📤 Post as bot** "
                              "to try a post (files, buttons, mentions) before sending it to students.")
            self.bot.ids.set("test_channel", ch.id)
        except discord.HTTPException as e:
            log.warning("Could not create test channel: %s", e)

    # ------------------------------------------------------------ building + sending
    def _mentions(self, d: Draft, channel) -> tuple:
        g = channel.guild
        roles = []
        if d.mention in ("roles", "both"):
            roles += [g.get_role(r) for r in d.roles]
        if d.mention in ("audience", "both"):
            roles += audience_roles(channel)
        roles = list(dict.fromkeys(r for r in roles if r))
        text = " ".join("@everyone" if r.is_default() else r.mention for r in roles)
        allowed = discord.AllowedMentions(everyone=any(r.is_default() for r in roles), roles=[r for r in roles
                                          if not r.is_default()], users=False)
        return text, allowed

    def _view(self, d: Draft):
        if not d.buttons:
            return None
        v = discord.ui.View(timeout=None)
        for label, url in d.buttons[:25]:
            v.add_item(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link))
        v.stop()
        return v

    def _payloads(self, d: Draft, mention_text: str) -> list:
        """List of send() kwargs (long plain text is split into several messages; files go with the last one)."""
        if d.style == "card":
            e = discord.Embed(title=d.title[:256] or None, description=d.text[:4096] or None,
                              color=discord.Color.blurple())
            e.set_footer(text=self.bot.settings.teacher)
            return [{"content": mention_text or None, "embed": e}]
        body = (f"**{d.title}**\n" if d.title else "") + d.text
        chunks, cur = [], (mention_text + "\n" if mention_text else "")
        for para in body.split("\n"):
            while len(para) > 1900:
                chunks.append(cur + para[:1900])
                cur, para = "", para[1900:]
            if len(cur) + len(para) + 1 > 1900:
                chunks.append(cur)
                cur = ""
            cur += para + "\n"
        if cur.strip():
            chunks.append(cur)
        return [{"content": c.rstrip() or None} for c in chunks] or [{"content": None}]

    LINK_RE = re.compile(r"https?://\S+")
    IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".webp")

    @staticmethod
    def _label_for(before: str) -> str:
        lab = re.sub(r"[*_`~>#]", "", before).strip().rstrip(":：-–—|=→>").strip()
        lab = re.sub(r"^[\-•·\d.)\s]+", "", lab).strip() or "Open link"
        return lab[:80]

    def build_layout(self, d: Draft, mention_text: str = ""):
        """Components-V2 message: text, and for every link a BUTTON with the link written under it."""
        items, buf = [], []

        def flush():
            txt = "\n".join(buf).strip("\n")
            if txt.strip():
                items.append(discord.ui.TextDisplay(txt[:3900]))
            buf.clear()

        if mention_text:
            buf.append(mention_text)
        if d.title:
            buf.append(f"## {d.title}")
        n_btn = 0
        for line in (d.text or "").split("\n"):
            urls = self.LINK_RE.findall(line)
            if not urls or n_btn >= 20:
                buf.append(line)
                continue
            flush()
            pos = 0
            for u in urls:
                u = u.rstrip(").,;>")
                i = line.find(u, pos)
                before = line[pos:i] if i >= 0 else ""
                pos = (i + len(u)) if i >= 0 else pos
                items.append(discord.ui.ActionRow(discord.ui.Button(label=self._label_for(before), url=u,
                                                                    style=discord.ButtonStyle.link, emoji="🔗")))
                items.append(discord.ui.TextDisplay(u))
                n_btn += 1
            rest = line[pos:].strip()
            if rest:
                buf.append(rest)
        for label, url in d.buttons[:max(0, 20 - n_btn)]:
            flush()
            items.append(discord.ui.ActionRow(discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link,
                                                                emoji="🔗")))
            items.append(discord.ui.TextDisplay(url))
        flush()
        imgs = [n for n, _ in d.files if n.lower().endswith(self.IMG_EXT)]
        if imgs:
            items.append(discord.ui.MediaGallery(*[discord.MediaGalleryItem(f"attachment://{n}") for n in imgs[:10]]))
        for n, _ in d.files:
            if n not in imgs:
                items.append(discord.ui.File(f"attachment://{n}"))
        v = discord.ui.LayoutView(timeout=None)
        if d.style == "card":
            v.add_item(discord.ui.Container(*items, accent_colour=discord.Color.blurple()))
        else:
            for it in items:
                v.add_item(it)
        v.stop()
        return v

    async def send_to(self, d: Draft, channel) -> discord.Message | None:
        mention_text, allowed = self._mentions(d, channel)
        if d.linkbuttons:
            files = [discord.File(io.BytesIO(b), filename=n) for n, b in d.files]
            return await channel.send(view=self.build_layout(d, mention_text), files=files, allowed_mentions=allowed)
        payloads = self._payloads(d, mention_text)
        last = None
        for i, p in enumerate(payloads):
            kw = dict(p, allowed_mentions=allowed)
            if i == len(payloads) - 1:
                if d.files:
                    kw["files"] = [discord.File(io.BytesIO(b), filename=n) for n, b in d.files]
                v = self._view(d)
                if v:
                    kw["view"] = v
            if not any(kw.get(k) for k in ("content", "embed", "files")):
                continue
            last = await channel.send(**kw)
        return last

    async def post(self, d: Draft, guild, who) -> str:
        ok, bad = [], []
        for cid in d.channels:
            ch = guild.get_channel(cid)
            if ch is None:
                continue
            try:
                msg = await self.send_to(d, ch)
                ok.append(f"[#{ch.name}]({msg.jump_url})" if msg else f"#{ch.name}")
            except discord.HTTPException as e:
                bad.append(f"#{ch.name} ({e.text or e.status})")
        summary = (f"✅ Posted in **{len(ok)}** channel(s): " + ", ".join(ok)) if ok else "❌ Nothing was posted."
        if bad:
            summary += "\n⚠️ Failed: " + ", ".join(bad)
        await self.bot.send_log("📤 Posted as bot", f"by {who.mention}\n{summary}\n"
                                f"Files: {len(d.files)} · Buttons: {len(d.buttons)} · Mention: {d.mention}\n"
                                f"```{(d.title + ' — ' if d.title else '') + d.text[:600]}```")
        return summary

    def preview_text(self, d: Draft, guild) -> str:
        chans = [guild.get_channel(c) for c in d.channels]
        chans = [c for c in chans if c]
        lines = ["👀 **Preview** — only you see this. Nothing is posted yet.",
                 f"📍 Channels ({len(chans)}): " + ", ".join(c.mention for c in chans)]
        if chans:
            mt, _ = self._mentions(d, chans[0])
            lines.append(f"🔔 Mention: **{dict((m[0], m[1]) for m in MENTION_MODES)[d.mention]}**"
                         + (f" — in {chans[0].mention}: {mt}" if mt else ""))
        nlinks = len(self.LINK_RE.findall(d.text or "")) if d.linkbuttons else 0
        lines.append(f"🎨 Style: **{'Card' if d.style == 'card' else 'Plain message'}"
                     f"{' + link buttons' if d.linkbuttons else ''}** · 📎 Files: **{len(d.files)}** · "
                     f"🔗 Buttons: **{len(d.buttons) + nlinks}**")
        return "\n".join(lines)

    # ------------------------------------------------------------ text command
    @commands.command(name="post")
    async def post_cmd(self, ctx, *, rest: str = ""):
        """!post #ch1 #ch2 [@Role …] [audience] message  (+ attach files)"""
        d = Draft()
        d.channels = [c.id for c in ctx.message.channel_mentions]
        d.roles = [r.id for r in ctx.message.role_mentions]
        if not d.channels:
            raise commands.BadArgument("Mention at least one channel, e.g. "
                                       "`!post #announcement #resources @3D Attendance sheet: https://...`")
        text = re.sub(r"<#\d+>|<@&\d+>", "", rest)
        aud = False
        while True:
            m = re.match(r"(?i)^\s*(audience|buttons|card)\b", text)
            if not m:
                break
            word = m.group(1).lower()
            aud = aud or word == "audience"
            d.linkbuttons = d.linkbuttons or word == "buttons"
            d.style = "card" if word == "card" else d.style
            text = text[m.end():]
        d.mention = ("both" if d.roles else "audience") if aud else ("roles" if d.roles else "none")
        d.text, d.buttons = parse_buttons(text.strip())
        for a in ctx.message.attachments[:10]:
            d.files.append((a.filename, await a.read()))
        if not (d.text or d.files):
            raise commands.BadArgument("Write a message or attach a file.")
        v = PreviewView(self, ctx.author.id, d)
        await ctx.send(self.preview_text(d, ctx.guild), view=v, allowed_mentions=discord.AllowedMentions.none())


# ============================================================ UI
class _Owned(discord.ui.View):
    def __init__(self, cog, user_id, timeout=900):
        super().__init__(timeout=timeout)
        self.cog, self.user_id = cog, user_id

    async def interaction_check(self, interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This belongs to someone else.", ephemeral=True)
            return False
        return True


class PostMenu(_Owned):
    """Step 1: where + who + style."""

    def __init__(self, cog, user_id):
        super().__init__(cog, user_id)
        self.d = Draft()
        ch = discord.ui.ChannelSelect(placeholder="📍 Choose channel(s) — many allowed", min_values=1, max_values=25,
                                      channel_types=[discord.ChannelType.text, discord.ChannelType.news], row=0)
        ch.callback = self._ch
        self.add_item(ch)
        rs = discord.ui.RoleSelect(placeholder="🏷️ Roles to mention (optional, many allowed)", min_values=0,
                                   max_values=25, row=1)
        rs.callback = self._roles
        self.add_item(rs)
        ms = discord.ui.Select(placeholder="🔔 Mention: No mention", row=2, options=[
            discord.SelectOption(label=l, value=v, emoji=e, description=desc, default=(v == "none"))
            for v, l, e, desc in MENTION_MODES])
        ms.callback = self._mention
        self.add_item(ms)
        ss = discord.ui.Select(placeholder="🎨 Style: Plain message", row=3, options=[
            discord.SelectOption(label="Plain message", value="plain", emoji="💬", default=True,
                                 description="Exactly as you write it (bold, lists, links …)"),
            discord.SelectOption(label="Plain + link buttons", value="plain+btn", emoji="🔗",
                                 description="Every link becomes a button, link written under it"),
            discord.SelectOption(label="Card (box with title)", value="card", emoji="🪧",
                                 description="Coloured box with a title"),
            discord.SelectOption(label="Card + link buttons", value="card+btn", emoji="🧷",
                                 description="Box + every link becomes a button")])
        ss.callback = self._style
        self.add_item(ss)
        w = discord.ui.Button(label="Write message / add files", emoji="✍️", style=discord.ButtonStyle.primary, row=4)
        w.callback = self._write
        self.add_item(w)
        c = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
        c.callback = self._cancel
        self.add_item(c)

    async def _ch(self, i):
        self.d.channels = [int(x) for x in i.data.get("values", [])]
        await i.response.defer()

    async def _roles(self, i):
        self.d.roles = [int(x) for x in i.data.get("values", [])]
        if self.d.roles and self.d.mention == "none":
            self.d.mention = "roles"
        await i.response.defer()

    async def _mention(self, i):
        self.d.mention = i.data["values"][0]
        await i.response.defer()

    async def _style(self, i):
        v = i.data["values"][0]
        self.d.style = v.split("+")[0]
        self.d.linkbuttons = v.endswith("+btn")
        await i.response.defer()

    async def _write(self, i):
        if not self.d.channels:
            await i.response.send_message("📍 Choose at least one channel first.", ephemeral=True)
            return
        if self.d.mention in ("roles", "both") and not self.d.roles:
            await i.response.send_message("🏷️ You chose to mention roles — select the role(s) first "
                                          "(or change the mention option).", ephemeral=True)
            return
        await i.response.send_modal(PostModal(self.cog, self.user_id, self.d))

    async def _cancel(self, i):
        self.stop()
        await i.response.edit_message(content="❎ Cancelled.", view=None)


class PostModal(discord.ui.Modal):
    def __init__(self, cog, user_id, d: Draft):
        super().__init__(title="📤 Post as bot", timeout=1800)
        self.cog, self.user_id, self.d = cog, user_id, d
        self.title_in = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=200,
                                             default=d.title or None, placeholder="e.g. Attendance sheet — Week 6")
        self.text_in = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=4000,
                                            default=d.text or None,
                                            placeholder="Write like in Discord: **bold**, lists, links …")
        self.files_in = discord.ui.FileUpload(required=False, min_values=0, max_values=10)
        self.btn_in = discord.ui.TextInput(style=discord.TextStyle.paragraph, required=False, max_length=1500,
                                           default="\n".join(f"{l} | {u}" for l, u in d.buttons) or None,
                                           placeholder="Attendance sheet | https://docs.google.com/...")
        self.add_item(discord.ui.Label(text="Title (optional)", component=self.title_in))
        self.add_item(discord.ui.Label(text="Message", description="Leave empty if you only send files",
                                       component=self.text_in))
        self.add_item(discord.ui.Label(text="Files (optional, up to 10)", component=self.files_in))
        self.add_item(discord.ui.Label(text="Link buttons (optional)", description="One per line:  Label | link",
                                       component=self.btn_in))

    async def on_submit(self, interaction: discord.Interaction):
        d = self.d
        d.title = (self.title_in.value or "").strip()
        d.text, inline = parse_buttons(self.text_in.value or "")
        lines, bad = parse_button_lines(self.btn_in.value or "")
        d.buttons = (inline + lines)[:25]
        await interaction.response.defer(ephemeral=True, thinking=True)
        new_files = []
        for a in (self.files_in.values or [])[:10]:
            try:
                new_files.append((a.filename, await a.read()))
            except discord.HTTPException:
                pass
        if new_files:
            d.files = new_files
        if not (d.text or d.files or d.title):
            await interaction.followup.send("⚠️ Write a message or add a file.", ephemeral=True)
            return
        text = self.cog.preview_text(d, interaction.guild)
        if bad:
            text += "\n⚠️ Not a link (ignored): " + ", ".join(f"`{b[:60]}`" for b in bad)
        await interaction.followup.send(text, ephemeral=True, view=PreviewView(self.cog, self.user_id, d))
        # show exactly how it will look (only to the teacher)
        sample = Draft()
        sample.__dict__.update(d.__dict__)
        sample.mention = "none"
        if d.linkbuttons:
            try:
                await interaction.followup.send(ephemeral=True, view=self.cog.build_layout(sample),
                                                files=[discord.File(io.BytesIO(b), filename=n) for n, b in d.files])
            except discord.HTTPException as e:
                await interaction.followup.send(f"⚠️ Preview problem: `{e.text or e}`", ephemeral=True)
            return
        for p in self.cog._payloads(sample, "")[:3]:
            kw = {k: v for k, v in p.items() if v}
            v = self.cog._view(d)
            if v:
                kw["view"] = v
            if d.files:
                kw["files"] = [discord.File(io.BytesIO(b), filename=n) for n, b in d.files]
            if kw:
                try:
                    await interaction.followup.send(ephemeral=True, **kw)
                except discord.HTTPException:
                    pass


class PreviewView(_Owned):
    def __init__(self, cog, user_id, d: Draft):
        super().__init__(cog, user_id)
        self.d = d
        go = discord.ui.Button(label=f"Post now in {len(d.channels)} channel(s)", emoji="✅",
                               style=discord.ButtonStyle.success)
        go.callback = self._go
        self.add_item(go)
        ed = discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary)
        ed.callback = self._edit
        self.add_item(ed)
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.danger)
        no.callback = self._no
        self.add_item(no)

    async def _go(self, i):
        self.stop()
        await i.response.edit_message(content="⏳ Posting...", view=None)
        summary = await self.cog.post(self.d, i.guild, i.user)
        try:
            await i.edit_original_response(content=summary)
        except discord.HTTPException:
            await i.followup.send(summary, ephemeral=True)

    async def _edit(self, i):
        await i.response.send_modal(PostModal(self.cog, self.user_id, self.d))

    async def _no(self, i):
        self.stop()
        await i.response.edit_message(content="❎ Cancelled — nothing was posted.", view=None)


async def setup(bot):
    await bot.add_cog(Poster(bot))
