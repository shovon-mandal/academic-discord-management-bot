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
    ("audience", "Mention who can see each channel", "👥", "Automatic: 3D channel → @3D, 4B channel → @4B"),
    ("roles", "Mention the roles I select", "🏷️", "Each channel pings ONLY the selected roles that can see it"),
    ("both", "Both: selected roles + channel's roles", "📣", "Still only roles that can see that channel"),
]


def ch_label(c) -> str:
    """#channel (Category) — same-named channels in different sections are easy to tell apart."""
    cat = c.category.name if getattr(c, "category", None) else ""
    return f"{c.mention} ({cat})" if cat else c.mention


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
        self.expire = ""             # auto-delete: "today", "3h", "2d" … ("" = keep)


def draft_to_dict(d: Draft) -> dict:
    return {k: v for k, v in d.__dict__.items() if k != "files"} | {"file_names": [n for n, _ in d.files]}


def draft_from_dict(data: dict, folder) -> Draft:
    d = Draft()
    for k, v in data.items():
        if k != "file_names":
            setattr(d, k, v)
    d.buttons = [tuple(x) for x in d.buttons]
    d.files = []
    for n in data.get("file_names", []):
        try:
            d.files.append((n, (folder / n).read_bytes()))
        except OSError:
            pass
    return d


class Poster(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        from discord.ext import tasks
        self.cleaner = tasks.loop(minutes=5)(self._clean_expired)

    async def cog_load(self):
        self.cleaner.start()

    async def cog_unload(self):
        self.cleaner.cancel()

    EXP_FILE = None

    def _exp_path(self):
        from core.config import DATA_DIR
        return DATA_DIR / "expiring_posts.json"

    def expire_at(self, text: str):
        """'today' → 23:59 today · '3h' / '45m' / '2d' → from now · '' → None."""
        from datetime import timedelta
        t = (text or "").strip().lower()
        now = self.bot.settings.now()
        if not t or t in ("no", "never", "-"):
            return None
        if t in ("today", "aj", "end of today"):
            return now.replace(hour=23, minute=59, second=0, microsecond=0)
        if t in ("tomorrow", "kal"):
            return (now + timedelta(days=1)).replace(hour=23, minute=59, second=0, microsecond=0)
        m = re.fullmatch(r"(\d+)\s*([mhd])", t)
        if m:
            n, u = int(m.group(1)), m.group(2)
            return now + (timedelta(minutes=n) if u == "m" else timedelta(hours=n) if u == "h" else timedelta(days=n))
        return None

    def remember_expiry(self, msgs: list, at):
        from core.storage import load_json, save_json
        data = load_json(self._exp_path(), [])
        data += [{"ch": m.channel.id, "msg": m.id, "at": at.isoformat()} for m in msgs]
        save_json(self._exp_path(), data)

    async def _clean_expired(self):
        from datetime import datetime as _dt
        from core.storage import load_json, save_json
        await self.bot.wait_until_ready()
        data = load_json(self._exp_path(), [])
        if not data:
            return
        now, keep = self.bot.settings.now(), []
        for it in data:
            if _dt.fromisoformat(it["at"]) > now:
                keep.append(it)
                continue
            ch = self.bot.get_channel(int(it["ch"]))
            try:
                if ch:
                    await ch.get_partial_message(int(it["msg"])).delete()
            except discord.NotFound:
                pass
            except discord.HTTPException:
                keep.append(it)
        save_json(self._exp_path(), keep)

    @commands.Cog.listener()
    async def on_ready(self):
        """Make sure the admin-only 🤖┃bot-test channel exists (to try posts before sending to students)."""
        if getattr(self, "_ready", False):
            return
        self._ready = True
        name = (self.bot.settings.raw.get("channels", {}) or {}).get("test", "🤖┃bot-test")
        cur = self.bot.channel_for("test_channel")
        if cur:
            if cur.name != name:
                try:
                    await cur.edit(name=name, reason="Test channel name from config.json")
                except discord.HTTPException as e:
                    log.warning("Could not rename test channel: %s", e)
            return
        adm = self.bot.channel_for("admin_channel")
        if adm is None:
            return
        existing = discord.utils.find(lambda c: c.name == name or "bot-test" in c.name, adm.guild.text_channels)
        try:
            ch = existing or await adm.clone(name=name, reason="Admin-only test channel")
            if not existing:
                await ch.edit(topic="Only admins see this — try 📤 Post as bot, files, buttons here first")
                await ch.send("🤖 **Test channel** — only admins can see this. Choose it in **📤 Post as bot** "
                              "to try a post (files, buttons, mentions) before sending it to students.")
            self.bot.ids.set("test_channel", ch.id)
        except discord.HTTPException as e:
            log.warning("Could not create test channel: %s", e)

    # ------------------------------------------------------------ building + sending
    def _mentions(self, d: Draft, channel) -> tuple:
        g = channel.guild
        roles = []
        if d.mention in ("roles", "both"):
            # only the selected roles that can actually SEE this channel (3D role is not pinged in 4B's channel)
            for r in (g.get_role(x) for x in d.roles):
                try:
                    if r and channel.permissions_for(r).view_channel:
                        roles.append(r)
                except Exception:  # noqa: BLE001
                    pass
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
        guild = guild or self.bot.main_guild()
        ok, bad = [], []
        exp = self.expire_at(d.expire)
        sent_msgs = []
        for cid in d.channels:
            ch = guild.get_channel(cid)
            if ch is None:
                continue
            try:
                msg = await self.send_to(d, ch)
                cat = f" ({ch.category.name})" if ch.category else ""
                ok.append(f"[#{ch.name}{cat}]({msg.jump_url})" if msg else f"#{ch.name}{cat}")
                if msg:
                    sent_msgs.append(msg)
            except discord.HTTPException as e:
                bad.append(f"#{ch.name} ({e.text or e.status})")
        summary = (f"✅ Posted in **{len(ok)}** channel(s): " + ", ".join(ok)) if ok else "❌ Nothing was posted."
        if exp and sent_msgs:
            self.remember_expiry(sent_msgs, exp)
            summary += f"\n🧹 Will be deleted automatically on **{exp:%d %b, %I:%M %p}**."
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
                 f"📍 Channels ({len(chans)}): " + ", ".join(ch_label(c) for c in chans)]
        if chans:
            lines.append(f"🔔 Mention: **{dict((m[0], m[1]) for m in MENTION_MODES)[d.mention]}**")
            if d.mention != "none":
                for c in chans[:15]:
                    mt, _ = self._mentions(d, c)
                    lines.append(f"   • {ch_label(c)} → {mt or '*(nobody — no selected role can see it)*'}")
        nlinks = len(self.LINK_RE.findall(d.text or "")) if d.linkbuttons else 0
        exp = self.expire_at(d.expire)
        if exp:
            lines.append(f"🧹 Auto-delete: **{exp:%d %b, %I:%M %p}**")
        elif d.expire:
            lines.append(f"⚠️ Could not read auto-delete `{d.expire}` — use today / tomorrow / 3h / 2d (it will be kept).")
        lines.append(f"🎨 Style: **{'Card' if d.style == 'card' else 'Plain message'}"
                     f"{' + link buttons' if d.linkbuttons else ''}** · 📎 Files: **{len(d.files)}** · "
                     f"🔗 Buttons: **{len(d.buttons) + nlinks}**")
        return "\n".join(lines)

    # ------------------------------------------------------------ preview in 🤖┃bot-test before posting
    def _pv_path(self):
        from core.config import DATA_DIR
        return DATA_DIR / "preview_posts.json"

    def _pv_files(self, pid):
        from core.config import DATA_DIR
        return DATA_DIR / "preview_files" / pid

    def _pv_load(self):
        from core.storage import load_json
        return load_json(self._pv_path(), {})

    def _pv_save(self, data):
        from core.storage import save_json
        save_json(self._pv_path(), data)

    def test_channel(self):
        return self.bot.channel_for("test_channel")

    def _pv_view(self, pid, kind, n_targets):
        v = discord.ui.View(timeout=None)
        label = {"rem_ok": "Approve (send at its time)", "job": "Approve (post at its time)"}.get(
            kind, f"Post now ({n_targets})")
        v.add_item(discord.ui.Button(label=label, emoji="✅", style=discord.ButtonStyle.success,
                                     custom_id=f"nubtk:pv:post:{pid}"))
        if kind in ("post", "event"):
            v.add_item(discord.ui.Button(label="Edit", emoji="✏️", style=discord.ButtonStyle.secondary,
                                         custom_id=f"nubtk:pv:edit:{pid}"))
        v.add_item(discord.ui.Button(label="Cancel", emoji="❌", style=discord.ButtonStyle.danger,
                                     custom_id=f"nubtk:pv:cancel:{pid}"))
        v.stop()
        return v

    def find_pending(self, kind, **match):
        return [pid for pid, it in self._pv_load().items()
                if it["kind"] == kind and all(it["data"].get(k) == v for k, v in match.items())]

    async def stage(self, kind: str, data: dict, header: discord.Embed, render, n_targets: int, by, files=None,
                    replace: str | None = None) -> str | None:
        """Post a PREVIEW in 🤖┃bot-test (header: where / who is mentioned + the message itself) with
        ✅ Post now / ✏️ Edit / ❌ Cancel. Returns the preview id, or None if there is no test channel."""
        import secrets
        ch = self.test_channel()
        if ch is None:
            return None
        allp = self._pv_load()
        if replace and replace in allp:
            await self._pv_close(replace, "♻️ Replaced by a newer preview below.")
            allp = self._pv_load()
        pid = secrets.token_hex(4)
        if files:
            folder = self._pv_files(pid)
            folder.mkdir(parents=True, exist_ok=True)
            for n, b in files:
                (folder / n).write_bytes(b)
        header.set_footer(text="Nothing is posted until you click ✅ Post now · only admins see this channel")
        msg = await ch.send(embed=header, view=self._pv_view(pid, kind, n_targets),
                            allowed_mentions=discord.AllowedMentions.none())
        try:
            await render(ch)
        except discord.HTTPException as e:
            await ch.send(f"⚠️ Preview problem: `{e.text or e}`")
        allp = self._pv_load()  # reload: other previews may have changed while this one was being sent
        allp[pid] = {"kind": kind, "data": data, "by": getattr(by, "id", 0), "msg": [ch.id, msg.id],
                     "created": self.bot.settings.now_str()}
        self._pv_save(allp)
        return pid

    async def _pv_close(self, pid, text):
        import shutil
        allp = self._pv_load()
        it = allp.pop(pid, None)
        self._pv_save(allp)
        shutil.rmtree(self._pv_files(pid), ignore_errors=True)
        if not it:
            return
        try:
            m = self.bot.get_channel(int(it["msg"][0])).get_partial_message(int(it["msg"][1]))
            await m.edit(content=text, view=None)
        except (discord.HTTPException, AttributeError):
            pass

    # ---- generic gate used by every part of the bot that posts for students
    async def gate(self, kind, data, title, info_lines, render, n_targets, by, files=None, replace=None,
                   buttons="post") -> str | None:
        e = discord.Embed(title=f"👀 PREVIEW — {title} (not posted yet)", color=discord.Color.gold(),
                          description="\n".join(info_lines)[:4000])
        return await self.stage(kind, data, e, render, n_targets, by, files=files, replace=replace)

    async def notify_staged(self, ctx_or_inter, pid):
        ch = self.test_channel()
        text = (f"👀 Preview posted in {ch.mention if ch else '#bot-test'} — it shows exactly how it will look, "
                "where it goes and who is mentioned. Click **✅ Post now** there (or ✏️ Edit / ❌ Cancel).")
        try:
            if isinstance(ctx_or_inter, discord.Interaction):
                if ctx_or_inter.response.is_done():
                    await ctx_or_inter.followup.send(text, ephemeral=True)
                else:
                    await ctx_or_inter.response.send_message(text, ephemeral=True)
            else:
                await ctx_or_inter.send(text)
        except discord.HTTPException:
            pass

    def approved_ctx(self, user, files=None):
        bot = self.bot
        adm = bot.channel_for("admin_channel")

        class _Att:
            def __init__(self, n, b):
                self.filename, self._b = n, b

            async def read(self):
                return self._b

            async def to_file(self):
                return discord.File(io.BytesIO(self._b), filename=self.filename)

        class _Ctx:
            approved = True
            guild = bot.main_guild()
            author = user
            channel = adm
            message = type("Msg", (), {"attachments": [_Att(n, b) for n, b in (files or [])]})()

            @staticmethod
            async def send(content=None, **kw):
                return await adm.send(content, **kw) if adm else None
        return _Ctx

    async def run_approved(self, kind, data, user, files):
        """The work behind ✅ Post now for every kind of preview. Returns a short result text."""
        r = self.bot.get_cog("Routine")
        admin = self.bot.get_cog("Admin")
        panel = self.bot.get_cog("Panel")
        if kind == "announce":
            await admin._announce(self.approved_ctx(user, files), data["target"], data["text"], data["ping"],
                                  title=data.get("title") or "📢 Announcement")
            return f"✅ Announcement posted → {data['target']}"
        if kind == "cmd":
            cmd = self.bot.get_command(data["name"])
            await cmd.callback(cmd.cog, self.approved_ctx(user, files), *data.get("args", []), **data.get("kwargs", {}))
            return f"✅ `{data['name']}` done — the summary is in the admin channel."
        if kind == "event":
            secs = await r.announce_event(data["eid"], data.get("mode", "new"))
            return f"✅ Announced in: {', '.join(secs) or 'nothing (channel missing?)'}"
        if kind == "change":
            ok = await r.announce_change(data["cid"])
            return "✅ Announced in the section." if ok else "⚠️ Could not post (announcement channel missing?)."
        if kind == "change_bulk":
            results = await r.announce_changes_bulk(data["cids"], data.get("shared_note", ""))
            ok_secs = [s for s, v in results.items() if v]
            fail_secs = [s for s, v in results.items() if not v]
            out = f"✅ Announced in: {', '.join(ok_secs) or 'nothing'}"
            if fail_secs:
                out += f"\n⚠️ Could not post in: {', '.join(fail_secs)} (channel missing?)"
            return out
        if kind == "reminder":
            secs = await r.send_reminder(data["eid"], data.get("secs") or None, approved=True)
            return f"✅ Reminder sent to: {', '.join(secs) or 'nothing'}"
        if kind == "rem_ok":
            r.approve_reminder(data["eid"], data["which"])
            return "✅ Approved — it will be sent automatically at its time."
        if kind == "job":
            if data["jid"] in panel.jobs:
                panel.jobs[data["jid"]]["ok"] = True
                panel.save_jobs()
                return "✅ Approved — it will be posted at its time."
            return "⚠️ That scheduled post no longer exists."
        if kind == "msgrem":
            await panel.post_reminder(data["target"], data["link"], data["snapshot"], data.get("note", ""))
            return f"✅ Reminder posted → {data['target']}"
        if kind == "subs":
            sub = self.bot.get_cog("Submissions")
            return await sub.post_prepared(data, user)
        if kind == "attendance":
            att = self.bot.get_cog("Attendance")
            return await att.start_session(data["sec"], data["course"], data["minutes"], data.get("by") or user.name,
                                           approved=True)
        if kind == "edit_post":
            ch_id = data.get("edit_ch_id")
            msg_id = data.get("edit_msg_id")
            if not ch_id or not msg_id:
                return "⚠️ Edit reference missing — could not apply the edit."
            d = draft_from_dict(data, files)
            ch = self.bot.main_guild().get_channel(int(ch_id))
            if ch is None:
                return f"⚠️ Channel <#{ch_id}> not found."
            try:
                orig = await ch.fetch_message(int(msg_id))
            except discord.HTTPException:
                return "⚠️ Original message not found (deleted?)."
            if orig.author.id != self.bot.user.id:
                return "⚠️ That message was not posted by the bot — cannot edit."
            # Detect whether the original message uses Components V2 (IS_COMPONENTS_V2 flag).
            # Discord does not allow mixing content/embed with Components V2 in the same edit call.
            is_v2 = bool(getattr(orig.flags, "is_components_v2", False) or
                         (orig.flags.value & (1 << 15)))  # flag bit 15 = IS_COMPONENTS_V2
            try:
                if d.linkbuttons or is_v2:
                    # Always re-render as Components V2 when the original was V2,
                    # or when the new draft explicitly uses link buttons.
                    await orig.edit(view=self.build_layout(d))
                elif d.style == "card" and d.title:
                    emb = discord.Embed(title=d.title, description=d.text, color=discord.Color.blurple())
                    await orig.edit(embed=emb, content=None, view=None)
                else:
                    payloads = self._payloads(d, "")
                    text_payload = payloads[0] if payloads else {}
                    await orig.edit(
                        content=text_payload.get("content") or discord.utils.MISSING,
                        embed=text_payload.get("embed") or None,
                        view=None,
                    )
            except discord.HTTPException as exc:
                return f"⚠️ Edit failed: {exc.text or exc}"
            return f"✅ Message edited in <#{ch_id}> — [jump]({orig.jump_url})"
        if kind == "routine":
            rep, _ = await r.check(force_repost=bool(data.get("force")), apply=True)
            return "✅ Routine updated in the sections."
        return "?"

    # ---- previews for "Post as bot"
    def post_header(self, d: Draft, guild) -> discord.Embed:
        e = discord.Embed(title="👀 PREVIEW — Post as bot (not posted yet)", color=discord.Color.gold(),
                          description=self.preview_text(d, guild).split("\n", 1)[1] if "\n" in self.preview_text(d, guild)
                          else "")
        return e

    async def stage_post(self, d: Draft, guild, by, replace=None):
        sample = Draft()
        sample.__dict__.update(d.__dict__)
        sample.mention = "none"

        async def render(ch):
            if d.linkbuttons:
                await ch.send(view=self.build_layout(sample),
                              files=[discord.File(io.BytesIO(b), filename=n) for n, b in d.files])
                return
            payloads = self._payloads(sample, "")
            for i, p in enumerate(payloads):
                kw = {k: v for k, v in p.items() if v}
                if i == len(payloads) - 1:
                    v = self._view(d)
                    if v:
                        kw["view"] = v
                    if d.files:
                        kw["files"] = [discord.File(io.BytesIO(b), filename=n) for n, b in d.files]
                if kw:
                    await ch.send(allowed_mentions=discord.AllowedMentions.none(), **kw)
        return await self.stage("post", draft_to_dict(d), self.post_header(d, guild), render, len(d.channels), by,
                                files=d.files, replace=replace)

    async def stage_edit_post(self, d: Draft, ch_id: int, msg_id: int, guild, by, replace=None):
        """Stage a preview for editing an existing bot post. The preview shows the new content in bot-test;
        on approval the original message is edited in-place."""
        sample = Draft()
        sample.__dict__.update(d.__dict__)
        sample.mention = "none"

        async def render(ch):
            """Render the preview in bot-test so the teacher sees how it will look."""
            header = discord.Embed(
                title="👀 PREVIEW — Edit bot post (not applied yet)",
                color=discord.Color.orange(),
                description=f"Will edit the message in <#{ch_id}>",
            )
            await ch.send(embed=header)
            if d.linkbuttons:
                await ch.send(view=self.build_layout(sample),
                              files=[discord.File(io.BytesIO(b), filename=n) for n, b in d.files])
            else:
                payloads = self._payloads(sample, "")
                for i, p in enumerate(payloads):
                    kw = {k: v for k, v in p.items() if v}
                    if i == len(payloads) - 1 and d.files:
                        kw["files"] = [discord.File(io.BytesIO(b), filename=n) for n, b in d.files]
                    if kw:
                        await ch.send(allowed_mentions=discord.AllowedMentions.none(), **kw)

        edit_data = {**draft_to_dict(d), "edit_ch_id": ch_id, "edit_msg_id": msg_id}
        e = discord.Embed(
            title="👀 PREVIEW — Edit bot post (not applied yet)",
            color=discord.Color.orange(),
            description=f"Will edit the original message in <#{ch_id}>",
        )
        return await self.stage("edit_post", edit_data, e, render, 1, by, files=d.files, replace=replace)

    @commands.Cog.listener("on_interaction")
    async def _pv_interaction(self, interaction: discord.Interaction):
        if interaction.type != discord.InteractionType.component:
            return
        cid = (interaction.data or {}).get("custom_id", "")
        if not cid.startswith("nubtk:pv:"):
            return
        if not self.bot.is_admin(interaction.user):
            await interaction.response.send_message("Only the teacher can use this.", ephemeral=True)
            return
        _, _, action, pid = cid.split(":", 3)
        it = self._pv_load().get(pid)
        if it is None:  # already finished / replaced → just take the buttons away
            try:
                await interaction.response.edit_message(view=None)
            except discord.HTTPException:
                pass
            try:
                await interaction.followup.send("This preview was already finished or replaced — nothing more to do.",
                                                ephemeral=True)
            except discord.HTTPException:
                pass
            return
        kind, data = it["kind"], it["data"]
        r = self.bot.get_cog("Routine")
        if action == "cancel":
            if kind == "rem_ok" and r:
                r.approve_reminder(data["eid"], data["which"], ok=False)
            if kind == "job":
                self.bot.get_cog("Panel").jobs.pop(data["jid"], None)
                self.bot.get_cog("Panel").save_jobs()
            await interaction.response.defer()
            await self._pv_close(pid, f"❌ Cancelled by {interaction.user.mention} — nothing was posted.")
            return
        if action == "edit":
            if kind in ("post", "edit_post"):
                d = draft_from_dict(data, self._pv_files(pid))
                edit_ref = (data["edit_ch_id"], data["edit_msg_id"]) if kind == "edit_post" else None
                await interaction.response.send_modal(PostModal(self, interaction.user.id, d, replace=pid,
                                                                edit_ref=edit_ref))
            elif kind == "event":
                from cogs.panel import EventEditModal
                await interaction.response.send_modal(EventEditModal(self.bot.get_cog("Panel"), data["eid"], None,
                                                                     restage=pid))
            return
        # post now
        await interaction.response.defer()
        try:
            if kind == "post":
                d = draft_from_dict(data, self._pv_files(pid))
                text = await self.post(d, interaction.guild, interaction.user)
            else:
                files = []
                folder = self._pv_files(pid)
                if folder.exists():
                    files = [(f.name, f.read_bytes()) for f in sorted(folder.iterdir())]
                text = await self.run_approved(kind, data, interaction.user, files)
        except Exception as e:  # noqa: BLE001
            log.exception("approve %s failed", kind)
            await interaction.followup.send(f"❌ Failed: `{str(e)[:300]}` — the preview is kept, try again.",
                                            ephemeral=True)
            return
        await self._pv_close(pid, f"{text}\nby {interaction.user.mention}")

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
        if await self.stage_post(d, ctx.guild, ctx.author):
            await ctx.send(f"👀 Preview posted in {self.test_channel().mention} — check it and click **✅ Post now** there.")
            return
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
    """Step 1: choose ROLE(S) first → then the channels those roles can see → mention + style."""

    STYLES = [("plain", "Plain message", "💬", "Exactly as you write it (bold, lists, links …)"),
              ("plain+btn", "Plain + link buttons", "🔗", "Every link becomes a button, link written under it"),
              ("card", "Card (box with title)", "🪧", "Coloured box with a title"),
              ("card+btn", "Card + link buttons", "🧷", "Box + every link becomes a button")]

    def __init__(self, cog, user_id, guild=None):
        super().__init__(cog, user_id)
        self.d = Draft()
        self.d.mention = "roles"
        self.groups: list = []        # [(channel name, [channel ids])]
        self.picked: set = set()      # chosen group indexes
        self.guild = guild
        self._build()

    def _style_value(self):
        return self.d.style + ("+btn" if self.d.linkbuttons else "")

    def _compute_groups(self):
        g = self.guild
        roles = [r for r in (g.get_role(x) for x in self.d.roles) if r] if g else []
        found: dict = {}
        if roles:
            chans = sorted(g.text_channels, key=lambda c: ((c.category.position if c.category else -1), c.position))
            for c in chans:
                try:
                    if any(c.permissions_for(r).view_channel for r in roles):
                        found.setdefault(c.name, []).append(c)
                except Exception:  # noqa: BLE001
                    pass
        total = sum(len(v) for v in found.values())
        if total <= 25:  # every channel on its own line, with its category (Section 3D …)
            self.groups = [(f"{c.name} · {c.category.name if c.category else '—'}", [c])
                           for chans in found.values() for c in chans]
        else:
            self.groups = list(found.items())[:25]
        self.picked = {i for i in self.picked if i < len(self.groups)}

    def _build(self):
        self.clear_items()
        g = self.guild
        defaults = [discord.Object(id=x, type=discord.Role) for x in self.d.roles]
        rs = discord.ui.RoleSelect(placeholder="1️⃣ Choose role(s) first — e.g. 3D, 3E (many allowed)",
                                   min_values=1, max_values=25, row=0, default_values=defaults)
        rs.callback = self._roles
        self.add_item(rs)
        if self.groups:
            opts = []
            for idx, (name, chans) in enumerate(self.groups):
                where = ", ".join(sorted({(c.category.name if c.category else "—") for c in chans}))
                opts.append(discord.SelectOption(label=name[:100], value=str(idx), default=idx in self.picked,
                                                 description=f"{len(chans)} channel(s): {where}"[:100]))
            cs = discord.ui.Select(placeholder="2️⃣ Choose channel(s) these roles can see", row=1,
                                   min_values=1, max_values=len(opts), options=opts)
        else:
            cs = discord.ui.Select(placeholder="2️⃣ Channels appear here after you choose role(s)", row=1,
                                   disabled=True, options=[discord.SelectOption(label="—", value="none")])
        cs.callback = self._chans
        self.add_item(cs)
        ms = discord.ui.Select(placeholder="3️⃣ Mention", row=2, options=[
            discord.SelectOption(label=l, value=v, emoji=e, description=desc, default=(v == self.d.mention))
            for v, l, e, desc in MENTION_MODES])
        ms.callback = self._mention
        self.add_item(ms)
        ss = discord.ui.Select(placeholder="4️⃣ Style", row=3, options=[
            discord.SelectOption(label=l, value=v, emoji=e, description=desc, default=(v == self._style_value()))
            for v, l, e, desc in self.STYLES])
        ss.callback = self._style
        self.add_item(ss)
        w = discord.ui.Button(label="Write message / add files", emoji="✍️", style=discord.ButtonStyle.primary, row=4)
        w.callback = self._write
        self.add_item(w)
        t = discord.ui.Button(label="Only bot-test (try first)", emoji="🤖", style=discord.ButtonStyle.secondary, row=4)
        t.callback = self._test
        self.add_item(t)
        c = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=4)
        c.callback = self._cancel
        self.add_item(c)

    def _sync_channels(self):
        ids = []
        for i in sorted(self.picked):
            ids += [c.id for c in self.groups[i][1]]
        self.d.channels = list(dict.fromkeys(ids))

    def status_text(self) -> str:
        g = self.guild
        roles = [r for r in (g.get_role(x) for x in self.d.roles) if r] if g else []
        lines = ["📤 **Post as bot**",
                 "1️⃣ Role(s) → 2️⃣ channel(s) they can see → 3️⃣ mention → 4️⃣ style → **✍️ Write**",
                 f"🏷️ Roles: {', '.join(r.mention for r in roles) or '—'}"]
        chans = [c for c in (g.get_channel(x) for x in self.d.channels) if c] if g else []
        lines.append(f"📍 Channels ({len(chans)}): " + (", ".join(ch_label(c) for c in chans[:30]) or "—"))
        return "\n".join(lines)

    async def _roles(self, i):
        self.guild = i.guild
        self.d.roles = [int(x) for x in i.data.get("values", [])]
        self._compute_groups()
        self._sync_channels()
        self._build()
        await i.response.edit_message(content=self.status_text(), view=self,
                                      allowed_mentions=discord.AllowedMentions.none())

    async def _chans(self, i):
        self.guild = i.guild
        self.picked = {int(x) for x in i.data.get("values", []) if x.isdigit()}
        self._sync_channels()
        self._build()
        await i.response.edit_message(content=self.status_text(), view=self,
                                      allowed_mentions=discord.AllowedMentions.none())

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
            await i.response.send_message("📍 Choose role(s) and then channel(s) first "
                                          "(or click 🤖 Only bot-test).", ephemeral=True)
            return
        await i.response.send_modal(PostModal(self.cog, self.user_id, self.d))

    async def _cancel(self, i):
        self.stop()
        await i.response.edit_message(content="❎ Cancelled.", view=None)

    async def _test(self, i):
        ch = self.cog.bot.channel_for("test_channel")
        if ch is None:
            name = (self.cog.bot.settings.raw.get("channels", {}) or {}).get("test", "🤖┃bot-test")
            ch = discord.utils.find(lambda c: "bot-test" in c.name or c.name == name, i.guild.text_channels)
            if ch:
                self.cog.bot.ids.set("test_channel", ch.id)
        if ch is None:
            await i.response.send_message("🤖 Test channel not found — restart the bot or run `!setup_server`.",
                                          ephemeral=True)
            return
        self.d.channels = [ch.id]
        await i.response.send_message(f"🤖 Channel set to {ch.mention} only (the channel list above is ignored). "
                                      "Now click **✍️ Write message / add files**.", ephemeral=True)


class PostModal(discord.ui.Modal):
    def __init__(self, cog, user_id, d: Draft, replace=None, edit_ref=None):
        super().__init__(title="✏️ Edit bot post" if edit_ref else "📤 Post as bot", timeout=1800)
        self.cog, self.user_id, self.d, self.replace = cog, user_id, d, replace
        self.edit_ref = edit_ref  # (channel_id, message_id) when editing an existing bot post
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
        self.exp_in = discord.ui.TextInput(style=discord.TextStyle.short, required=False, max_length=20,
                                           default=d.expire or None, placeholder="today · tomorrow · 3h · 2d  (empty = keep)")
        self.add_item(discord.ui.Label(text="Auto-delete the notice? (optional)",
                                       description="For short-lived notices (room change, today only…)",
                                       component=self.exp_in))

    async def on_submit(self, interaction: discord.Interaction):
        d = self.d
        d.title = (self.title_in.value or "").strip()
        d.text, inline = parse_buttons(self.text_in.value or "")
        lines, bad = parse_button_lines(self.btn_in.value or "")
        d.expire = (self.exp_in.value or "").strip()
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
        warn = ("\n⚠️ Not a link (ignored): " + ", ".join(f"`{b[:60]}`" for b in bad)) if bad else ""
        # editing an existing bot post
        if self.edit_ref:
            ch_id, msg_id = self.edit_ref
            pid = await self.cog.stage_edit_post(d, ch_id, msg_id, interaction.guild, interaction.user,
                                                  replace=self.replace)
            if pid:
                await interaction.followup.send(
                    f"👀 Edit preview posted in {self.cog.test_channel().mention} — "
                    "it shows how the edited message will look. Click **✅ Post now** there to apply the edit."
                    + warn, ephemeral=True)
            else:
                await interaction.followup.send("⚠️ Could not stage the edit preview.", ephemeral=True)
            return
        pid = await self.cog.stage_post(d, interaction.guild, interaction.user, replace=self.replace)
        if pid:
            await interaction.followup.send(f"👀 Preview posted in {self.cog.test_channel().mention} — it shows where it "
                                            "goes and who is mentioned. Click **✅ Post now** there (or ✏️ Edit / ❌ Cancel)."
                                            + warn, ephemeral=True)
            return
        text = self.cog.preview_text(d, interaction.guild) + warn
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


class EditPostLinkModal(discord.ui.Modal):
    """Step 1: teacher pastes the message link they want to edit."""

    def __init__(self, cog, user_id):
        super().__init__(title="✏️ Edit a bot post", timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.link_in = discord.ui.TextInput(
            label="Message link",
            placeholder="Right-click the message → Copy Message Link → paste here",
            max_length=200,
        )
        self.add_item(self.link_in)

    async def on_submit(self, interaction: discord.Interaction):
        raw = (self.link_in.value or "").strip()
        await interaction.response.defer(ephemeral=True, thinking=True)
        # resolve the link to a message
        import re as _re
        m = _re.search(r"channels/(\d+)/(\d+)/(\d+)", raw)
        if not m:
            await interaction.followup.send(
                "⚠️ Not a valid message link.\n"
                "Right-click the message → **Copy Message Link** and paste here.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        ch = guild.get_channel(int(m.group(2))) or guild.get_thread(int(m.group(2)))
        if ch is None:
            await interaction.followup.send("⚠️ That channel is not in this server.", ephemeral=True)
            return
        try:
            msg = await ch.fetch_message(int(m.group(3)))
        except discord.HTTPException:
            await interaction.followup.send("⚠️ Message not found — it may have been deleted.", ephemeral=True)
            return
        # must be the bot's own message
        if msg.author.id != interaction.guild.me.id:
            await interaction.followup.send(
                "⚠️ That message was not posted by the bot — only bot messages can be edited.",
                ephemeral=True,
            )
            return
        # reconstruct the Draft from the existing message
        d = Draft()
        d.channels = [ch.id]
        d.style = "plain"
        d.mention = "none"
        # pull text from embed or plain content
        if msg.embeds:
            e = msg.embeds[0]
            d.title = e.title or ""
            d.text = e.description or ""
            d.style = "card"
        elif msg.content:
            d.text = msg.content
        else:
            # Components V2 — extract TextDisplay nodes
            lines = []
            for c in getattr(msg, "components", []):
                if hasattr(c, "components"):
                    for sub in c.components:
                        if hasattr(sub, "content"):
                            lines.append(sub.content)
                elif hasattr(c, "content"):
                    lines.append(c.content)
            d.text = "\n".join(lines)
            d.style = "plain"
        # extract buttons from components
        btns = []
        for c in getattr(msg, "components", []):
            comp_list = getattr(c, "components", [c])
            for sub in comp_list:
                if getattr(sub, "url", None):
                    btns.append((sub.label or "Link", sub.url))
        d.buttons = btns
        d.linkbuttons = bool(btns)
        # store the original message reference in the draft for the edit path
        d._edit_msg_id = msg.id
        d._edit_ch_id = ch.id
        # open the PostModal pre-filled so the teacher can edit
        await interaction.followup.send(
            f"✏️ Editing bot post in {ch.mention} — the form below shows the current content. "
            "Change what you want and click **Submit** — a preview will appear in bot-test.",
            ephemeral=True,
            view=_EditPostMenuView(self.cog, self.user_id, d, msg),
        )


class _EditPostMenuView(_Owned):
    """Shows a single '✏️ Edit' button that opens PostModal pre-filled.
    The resulting stage call carries the original message reference so run_approved edits instead of posts."""

    def __init__(self, cog, user_id, d: Draft, orig_msg: discord.Message):
        super().__init__(cog, user_id)
        self.d = d
        self.orig_msg = orig_msg
        btn = discord.ui.Button(label="Open edit form", emoji="✏️", style=discord.ButtonStyle.primary)
        btn.callback = self._open
        self.add_item(btn)

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            PostModal(self.cog, self.user_id, self.d,
                      replace=None, edit_ref=(self.orig_msg.channel.id, self.orig_msg.id))
        )


async def setup(bot):
    await bot.add_cog(Poster(bot))
