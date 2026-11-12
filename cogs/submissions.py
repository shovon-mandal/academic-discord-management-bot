"""📝 Submission lists from a Google Sheet / Google Form responses.

Panel → 📝 Submission list:
  1. paste the sheet link + topic (e.g. "Assignment 1") + optional deadline
  2. the bot reads the sheet and guesses the columns (Section, ID, Name, Time) — change them if wrong
     (no Section column? the bot finds each student's section from its own verified-student list by ID)
  3. choose sections + which channel (announcement / resources / …) + options
  4. preview → ✅ Post: 3D's submissions go to 3D's channel, 3E's to 3E's …

Posting the same topic again in the same channel EDITS the old list (no new message / notification).
The sheet must be readable by link: Share → "Anyone with the link → Viewer", or File → Share → Publish to web.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from datetime import datetime

import aiohttp
import discord
from discord.ext import commands

from core.config import DATA_DIR
from core.storage import load_json, save_json

log = logging.getLogger("nubtk.subs")
POSTS_FILE = DATA_DIR / "submission_posts.json"
FROM_BOT = "__bot__"
NONE = "__none__"

TIME_FORMATS = ["%m/%d/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M",
                "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%b-%Y %H:%M:%S", "%d %b %Y %H:%M"]
DEADLINE_FORMATS = ["%d %b %Y %I:%M %p", "%d %b %Y %H:%M", "%d %B %Y %I:%M %p", "%d %B %Y %H:%M",
                    "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M", "%d/%m/%Y %I:%M %p", "%d %b %I:%M %p", "%d %b %H:%M",
                    "%d %b %Y", "%Y-%m-%d", "%d/%m/%Y"]


# ------------------------------------------------------------ sheet reading
def csv_url(link: str) -> str | None:
    link = (link or "").strip()
    gid = (re.search(r"[#?&]gid=(\d+)", link) or [None, "0"])[1]
    m = re.search(r"/spreadsheets/d/e/([\w-]+)", link)
    if m:
        return f"https://docs.google.com/spreadsheets/d/e/{m.group(1)}/pub?gid={gid}&single=true&output=csv"
    m = re.search(r"/spreadsheets/d/([\w-]+)", link)
    if m:
        return f"https://docs.google.com/spreadsheets/d/{m.group(1)}/export?format=csv&gid={gid}"
    return None


async def fetch_table(link: str) -> list:
    url = csv_url(link)
    if not url:
        raise ValueError("That is not a Google Sheets link (it should contain `/spreadsheets/d/`).")
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.get(url, allow_redirects=True) as r:
            body = await r.read()
            ctype = r.headers.get("Content-Type", "")
            if r.status != 200 or "html" in ctype:
                raise ValueError("I can't open this sheet. In Google Sheets: **Share → General access → "
                                 "Anyone with the link → Viewer** (or File → Share → Publish to web), then try again.")
    text = body.decode("utf-8-sig", errors="replace")
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if len(rows) < 2:
        raise ValueError("The sheet is empty (no responses yet?).")
    return rows


def guess_columns(header: list) -> dict:
    h = [c.strip().lower() for c in header]

    def find(*pats, avoid=()):
        for p in pats:
            for i, c in enumerate(h):
                if re.search(p, c) and not any(re.search(a, c) for a in avoid):
                    return i
        return None
    return {
        "section": find(r"\bsection\b", r"\bsec\b", r"section"),
        "id": find(r"student\s*id", r"\bid\b", r"roll", r"\bid\s*(no|number)", r"id"),
        "name": find(r"full\s*name", r"^name$", r"student\s*name", r"name", avoid=(r"user", r"file", r"e-?mail")),
        "time": find(r"timestamp", r"submi", r"time", r"date"),
    }


def parse_times(values: list) -> list:
    """Parse sheet times; decides day/month order from the data itself."""
    dm = any(re.match(r"^\s*(1[3-9]|2\d|3[01])/\d{1,2}/", v or "") for v in values)
    fmts = [f for f in TIME_FORMATS if not (dm and f.startswith("%m/"))]
    out = []
    for v in values:
        v = (v or "").strip()
        got = None
        for f in fmts:
            try:
                got = datetime.strptime(v, f)
                break
            except ValueError:
                continue
        out.append(got)
    return out


def parse_deadline(text: str, now: datetime) -> datetime | None:
    t = re.sub(r"\s+", " ", (text or "").strip())
    if not t:
        return None
    for f in DEADLINE_FORMATS:
        try:
            d = datetime.strptime(t, f)
        except ValueError:
            continue
        if "%Y" not in f:
            d = d.replace(year=now.year)
        if "%H" not in f and "%I" not in f:
            d = d.replace(hour=23, minute=59)
        return d
    return None


def digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


# ------------------------------------------------------------ the job
class Job:
    def __init__(self, link, topic, deadline_text, note):
        self.link, self.topic, self.deadline_text, self.note = link, topic, deadline_text, note
        self.rows: list = []
        self.header: list = []
        self.cols: dict = {}
        self.sections: list = []
        self.channel_key = "announcement"
        self.options = {"missing"}   # missing / mention / late
        self.deadline: datetime | None = None


class Submissions(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings

    # ---- sorting rows into sections
    def section_of(self, job: Job, row: list) -> list:
        secs = self.s.sections
        ci = job.cols.get("section")
        if ci is not None and ci != FROM_BOT and ci < len(row):
            norm = re.sub(r"[\s\-_.]", "", row[ci].upper())
            hits = [x for x in secs if re.search(rf"(?<!\d){re.escape(x.upper())}(?![A-Z])", norm)]
            if hits:
                return hits[:1]
        sid = digits(row[job.cols["id"]]) if job.cols.get("id") is not None and job.cols["id"] < len(row) else ""
        for r in self.bot.store.find_by_student_id(sid) if sid else []:
            return [r.get("section")] + list(r.get("extra_sections") or [])
        return []

    def collect(self, job: Job) -> dict:
        """→ {section: {"done": [(id, name, time_text, late)], "missing": [(id, name)], "unknown": n}}"""
        cid, cn, ct = job.cols.get("id"), job.cols.get("name"), job.cols.get("time")
        times = parse_times([r[ct] if ct is not None and ct < len(r) else "" for r in job.rows])
        latest: dict = {}
        unknown = 0
        for row, t in zip(job.rows, times):
            sid = digits(row[cid]) if cid is not None and cid < len(row) else ""
            if not sid:
                continue
            cand = self.section_of(job, row)
            sec = next((x for x in cand if x in job.sections), None)
            if sec is None:
                if not cand:
                    unknown += 1
                continue
            name = row[cn].strip() if cn not in (None, FROM_BOT) and cn < len(row) else ""
            if not name:
                rec = self.bot.store.find_by_student_id(sid)
                name = rec[0].get("name", "") if rec else ""
            old = latest.get((sec, sid))
            if old is None or (t and old[2] and t > old[2]):  # keep the latest submission
                latest[(sec, sid)] = (sid, name, t, row[ct].strip() if ct is not None and ct < len(row) else "")
        out = {sec: {"done": [], "missing": [], "unknown": unknown} for sec in job.sections}
        for (sec, sid), (_, name, t, raw) in latest.items():
            late = bool(job.deadline and t and t > job.deadline)
            out[sec]["done"].append((sid, name, t.strftime("%d %b %I:%M %p") if t else raw[:16], late, t))
        for sec in job.sections:
            out[sec]["done"].sort(key=lambda x: (x[4] is None, x[4] or datetime.max, x[1]))
            got = {x[0] for x in out[sec]["done"]}
            out[sec]["missing"] = sorted(((r.get("student_id", ""), r.get("name", ""))
                                          for r in self.bot.store.section_students(sec)
                                          if r.get("student_id") not in got), key=lambda x: x[0])
        return out

    # ---- message
    def embeds_for(self, job: Job, sec: str, data: dict) -> list:
        done, missing = data["done"], data["missing"]
        eve = " (Evening)" if self.s.is_evening(sec) else ""
        head = [f"✅ **Submitted: {len(done)}**"]
        if "missing" in job.options:
            head.append(f"❌ **Not submitted: {len(missing)}**")
        n_late = sum(1 for x in done if x[3])
        if job.deadline:
            head.append(f"⏰ Deadline: **{job.deadline.strftime('%d %b %Y, %I:%M %p')}**"
                        + (f" · late: **{n_late}**" if n_late else ""))
        desc = " · ".join(head)
        if job.note:
            desc += f"\n📝 {job.note}"
        desc += f"\n🕒 Updated: {self.s.now().strftime('%d %b %Y, %I:%M %p')}"
        rows = [f"{'#':>3}  {'ID':<11}  {'Name':<22}  Submitted"]
        for i, (sid, name, tt, late, _) in enumerate(done, 1):
            rows.append(f"{i:>3}  {sid[-11:]:<11}  {name[:22]:<22}  {tt}{'  LATE' if late else ''}")
        blocks = self._blocks(rows) if done else ["*Nobody yet.*"]
        embeds = []
        for i, b in enumerate(blocks):
            e = discord.Embed(color=discord.Color(self.s.section_color(sec)),
                              title=(f"📝 {job.topic} — Section {sec}{eve}" if i == 0 else None),
                              description=((desc + "\n") if i == 0 else "") + b)
            embeds.append(e)
        if "missing" in job.options:
            mrows = [f"{'#':>3}  {'ID':<11}  Name"] + [f"{i:>3}  {sid[-11:]:<11}  {name[:30]}"
                                                     for i, (sid, name) in enumerate(missing, 1)]
            mblocks = self._blocks(mrows) if missing else ["🎉 Everyone (verified on Discord) has submitted."]
            for i, b in enumerate(mblocks):
                embeds.append(discord.Embed(color=discord.Color.dark_grey(), description=b,
                                            title="❌ Not submitted yet (students verified on Discord)" if i == 0 else None))
        embeds[-1].set_footer(text="If your name is missing or wrong, tell your teacher in class.")
        return embeds[:10]

    @staticmethod
    def _blocks(rows: list) -> list:
        head, body = rows[0], rows[1:]
        out, cur = [], []
        for r in body:
            if sum(len(x) + 1 for x in cur) + len(r) > 3600:
                out.append("```\n" + "\n".join([head] + cur) + "\n```")
                cur = []
            cur.append(r)
        out.append("```\n" + "\n".join([head] + cur) + "\n```")
        return out

    # ---- posting (edits the old list for the same topic + channel)
    async def post(self, job: Job, data: dict, who) -> str:
        posts = load_json(POSTS_FILE, {})
        ok, bad = [], []
        for sec in job.sections:
            ch = self.bot.channel_for(f"sec:{sec}:{job.channel_key}")
            if ch is None:
                bad.append(f"{sec} (no {job.channel_key} channel)")
                continue
            embeds = self.embeds_for(job, sec, data[sec])
            content = None
            allowed = discord.AllowedMentions.none()
            if "mention" in job.options:
                role = self.bot.role_for(f"role:sec:{sec}")
                if role:
                    content, allowed = role.mention, discord.AllowedMentions(roles=[role])
            key = f"{job.topic.strip().lower()}|{ch.id}"
            msg = None
            old = posts.get(key)
            if old:
                try:
                    msg = await ch.fetch_message(int(old))
                    await msg.edit(content=content, embeds=embeds, allowed_mentions=allowed)
                except discord.HTTPException:
                    msg = None
            try:
                if msg is None:
                    msg = await ch.send(content=content, embeds=embeds, allowed_mentions=allowed)
                posts[key] = msg.id
                ok.append(f"[{sec}]({msg.jump_url}) ({len(data[sec]['done'])})")
            except discord.HTTPException as e:
                bad.append(f"{sec} ({e.text or e.status})")
        save_json(POSTS_FILE, posts)
        text = (f"✅ **{job.topic}** posted: " + ", ".join(ok)) if ok else "❌ Nothing was posted."
        if bad:
            text += "\n⚠️ " + ", ".join(bad)
        await self.bot.send_log("📝 Submission list posted", f"by {who.mention}\n{text}\nSheet: {job.link}")
        return text

    def preview(self, job: Job, data: dict) -> str:
        ch_name = next((c["name"] for c in self.s.raw["channels"]["section"] if c["key"] == job.channel_key),
                       job.channel_key)
        lines = [f"👀 **Preview — {job.topic}** (only you see this)",
                 f"📍 Posts in **{ch_name}** of: " + ", ".join(job.sections)]
        for sec in job.sections:
            d = data[sec]
            lines.append(f"• **{sec}**: ✅ {len(d['done'])} submitted"
                         + (f" · ❌ {len(d['missing'])} not submitted" if "missing" in job.options else "")
                         + (f" · ⏰ {sum(1 for x in d['done'] if x[3])} late" if job.deadline else ""))
        unk = next(iter(data.values()))["unknown"] if data else 0
        if unk:
            lines.append(f"⚠️ **{unk}** row(s): section not found (no section in the sheet and the ID is not "
                         "a verified student) — they are not in any list.")
        if job.deadline_text and not job.deadline:
            lines.append(f"⚠️ Could not read the deadline `{job.deadline_text}` — e.g. `12 Nov 2026 11:59 PM`.")
        lines.append("Posting the same topic again later **updates** these lists (no new message).")
        return "\n".join(lines)

    @commands.command(name="submissions")
    async def submissions_cmd(self, ctx):
        await ctx.send("📝 Use the panel: **🎛️ Panel → Deadlines & tests → 📝 Submission list** "
                       "(it asks for the Google Sheet link, topic and columns).")


# ============================================================ UI
class SubModal(discord.ui.Modal):
    def __init__(self, cog, user_id):
        super().__init__(title="📝 Submission list", timeout=1800)
        self.cog, self.user_id = cog, user_id
        self.link = discord.ui.TextInput(label="Google Sheet link (Form responses)", max_length=500,
                                         placeholder="https://docs.google.com/spreadsheets/d/...")
        self.topic = discord.ui.TextInput(label="Topic", max_length=100, placeholder="e.g. Assignment 1 (OOP II)")
        self.deadline = discord.ui.TextInput(label="Deadline (optional → marks LATE)", required=False, max_length=40,
                                             placeholder="e.g. 12 Nov 2026 11:59 PM")
        self.note = discord.ui.TextInput(label="Note under the title (optional)", required=False, max_length=300,
                                         style=discord.TextStyle.paragraph)
        for x in (self.link, self.topic, self.deadline, self.note):
            self.add_item(x)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        job = Job(self.link.value.strip(), self.topic.value.strip(), (self.deadline.value or "").strip(),
                  (self.note.value or "").strip())
        job.deadline = parse_deadline(job.deadline_text, self.cog.s.now().replace(tzinfo=None))
        try:
            rows = await fetch_table(job.link)
        except Exception as e:  # noqa: BLE001
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        job.header, job.rows = rows[0], rows[1:]
        job.cols = guess_columns(job.header)
        if job.cols.get("section") is None:
            job.cols["section"] = FROM_BOT
        if job.cols.get("name") is None:
            job.cols["name"] = FROM_BOT
        v = ColumnMenu(self.cog, self.user_id, job)
        await interaction.followup.send(v.text(), view=v, ephemeral=True)


class _Owned(discord.ui.View):
    def __init__(self, cog, user_id, job):
        super().__init__(timeout=1800)
        self.cog, self.user_id, self.job = cog, user_id, job

    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id


class ColumnMenu(_Owned):
    """Step 2: which column is what (guessed — change if wrong)."""
    FIELDS = [("section", "Section", True), ("id", "Student ID", False), ("name", "Name", True), ("time", "Time", False)]

    def __init__(self, cog, user_id, job):
        super().__init__(cog, user_id, job)
        for row, (key, label, can_bot) in enumerate(self.FIELDS):
            opts = []
            if can_bot:
                opts.append(discord.SelectOption(label=f"(no column) → from the bot's student list", value=FROM_BOT,
                                                 emoji="🤖", default=job.cols.get(key) == FROM_BOT))
            if key == "time":
                opts.append(discord.SelectOption(label="(no time column)", value=NONE, default=job.cols.get(key) is None))
            for i, h in enumerate(job.header[:23]):
                sample = next((r[i] for r in job.rows if i < len(r) and r[i].strip()), "")
                opts.append(discord.SelectOption(label=(h.strip() or f"Column {i + 1}")[:100], value=str(i),
                                                 description=f"e.g. {sample}"[:100], default=job.cols.get(key) == i))
            sel = discord.ui.Select(placeholder=f"{label} column", options=opts[:25], row=row)
            sel.callback = self._make(key)
            self.add_item(sel)
        nx = discord.ui.Button(label="Next: sections & channel", emoji="➡️", style=discord.ButtonStyle.primary, row=4)
        nx.callback = self._next
        self.add_item(nx)

    def text(self) -> str:
        def name(key):
            v = self.job.cols.get(key)
            if v == FROM_BOT:
                return "🤖 from the bot's student list (by ID)"
            if v is None:
                return "— (none)"
            return f"`{self.job.header[v].strip()}`"
        return (f"📝 **{self.job.topic}** — read **{len(self.job.rows)}** response(s).\n"
                "I guessed the columns — change them below if something is wrong:\n"
                + "\n".join(f"• **{label}**: {name(k)}" for k, label, _ in self.FIELDS))

    def _make(self, key):
        async def cb(interaction):
            v = interaction.data["values"][0]
            self.job.cols[key] = FROM_BOT if v == FROM_BOT else (None if v == NONE else int(v))
            await interaction.response.edit_message(content=self.text(), view=self)
        return cb

    async def _next(self, interaction):
        if self.job.cols.get("id") in (None, FROM_BOT):
            await interaction.response.send_message("Choose the **Student ID** column first.", ephemeral=True)
            return
        # sections that appear in the data are pre-selected
        self.job.sections = list(self.cog.s.sections)
        found = set()
        for r in self.job.rows:
            found.update(self.cog.section_of(self.job, r))
        self.job.sections = [x for x in self.cog.s.sections if x in found] or list(self.cog.s.sections)
        v = TargetMenu(self.cog, self.user_id, self.job)
        await interaction.response.edit_message(content=v.text(), view=v)


class TargetMenu(_Owned):
    """Step 3: sections + channel + options → preview."""

    def __init__(self, cog, user_id, job):
        super().__init__(cog, user_id, job)
        s = cog.s
        secs = discord.ui.Select(placeholder="Sections", min_values=1, max_values=len(s.sections), row=0, options=[
            discord.SelectOption(label=f"Section {x}", value=x, default=x in job.sections,
                                 description=s.section_courses_text(x)[:100] or None)
            for x in s.sections])
        secs.callback = self._secs
        self.add_item(secs)
        chans = [c for c in s.raw["channels"]["section"] if c.get("type", "text") == "text"]
        ch = discord.ui.Select(placeholder="Channel", row=1, options=[
            discord.SelectOption(label=c["name"], value=c["key"], default=c["key"] == job.channel_key) for c in chans][:25])
        ch.callback = self._ch
        self.add_item(ch)
        opt = discord.ui.Select(placeholder="Options", min_values=0, max_values=2, row=2, options=[
            discord.SelectOption(label="Also show who has NOT submitted", value="missing", emoji="❌",
                                 description="From students verified on Discord", default="missing" in job.options),
            discord.SelectOption(label="Mention the section role", value="mention", emoji="🔔",
                                 description="e.g. @3D in 3D's channel", default="mention" in job.options)])
        opt.callback = self._opt
        self.add_item(opt)
        pv = discord.ui.Button(label="Preview", emoji="👀", style=discord.ButtonStyle.primary, row=3)
        pv.callback = self._preview
        self.add_item(pv)
        cn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=3)
        cn.callback = self._cancel
        self.add_item(cn)

    def text(self):
        return (f"📝 **{self.job.topic}** — choose where to post.\n"
                "Each section gets only its own students (3D's list → 3D's channel).")

    async def _secs(self, i):
        self.job.sections = [x for x in self.cog.s.sections if x in i.data["values"]]
        await i.response.defer()

    async def _ch(self, i):
        self.job.channel_key = i.data["values"][0]
        await i.response.defer()

    async def _opt(self, i):
        self.job.options = set(i.data.get("values", []))
        await i.response.defer()

    async def _preview(self, i):
        data = self.cog.collect(self.job)
        await i.response.edit_message(content=self.cog.preview(self.job, data),
                                      view=ConfirmMenu(self.cog, self.user_id, self.job, data))
        first = next((x for x in self.job.sections), None)
        if first:
            try:
                await i.followup.send(embeds=self.cog.embeds_for(self.job, first, data[first]), ephemeral=True)
            except discord.HTTPException:
                pass

    async def _cancel(self, i):
        self.stop()
        await i.response.edit_message(content="❎ Cancelled.", view=None)


class ConfirmMenu(_Owned):
    def __init__(self, cog, user_id, job, data):
        super().__init__(cog, user_id, job)
        self.data = data
        go = discord.ui.Button(label=f"Post in {len(job.sections)} section(s)", emoji="✅",
                               style=discord.ButtonStyle.success)
        go.callback = self._go
        self.add_item(go)
        bk = discord.ui.Button(label="Back", emoji="↩️", style=discord.ButtonStyle.secondary)
        bk.callback = self._back
        self.add_item(bk)

    async def _go(self, i):
        self.stop()
        await i.response.edit_message(content="⏳ Posting...", view=None)
        text = await self.cog.post(self.job, self.data, i.user)
        await i.edit_original_response(content=text)

    async def _back(self, i):
        v = TargetMenu(self.cog, self.user_id, self.job)
        await i.response.edit_message(content=v.text(), view=v)


async def setup(bot):
    await bot.add_cog(Submissions(bot))
