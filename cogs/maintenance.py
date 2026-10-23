"""Backups, status, logs, and remote control: !update (git pull + restart), !restart."""
from __future__ import annotations

import asyncio
import io
import logging
import platform
import sys
from datetime import time as dtime

import discord
from discord.ext import commands, tasks

from core.config import BASE_DIR, LOG_DIR, Settings
from core.system import (cpu_temp_c, disk_str, git_commit, host_uptime_str, human_duration, make_backup,
                         memory_mb, platform_str, run_cmd)

log = logging.getLogger("nubtk.maint")
MAX_UPLOAD = 8 * 1024 * 1024


class Maintenance(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.s = bot.settings
        h, m = (int(x) for x in self.s.backup_time.split(":"))
        self.daily = tasks.loop(time=dtime(hour=h, minute=m, tzinfo=self.s.tz))(self._daily_backup)

    async def cog_load(self):
        if self.s.backup_enabled:
            self.daily.start()

    async def cog_unload(self):
        self.daily.cancel()

    async def _daily_backup(self):
        await self.bot.wait_until_ready()
        try:
            path = await asyncio.to_thread(make_backup, self.s.tz, self.s.backup_keep)
        except Exception:
            log.exception("Daily backup failed")
            return
        file = discord.File(path) if path.stat().st_size < MAX_UPLOAD else None
        await self.bot.send_log("💾 Daily backup", f"`{path.name}` ({path.stat().st_size // 1024} KB)",
                                discord.Color.dark_teal(), file=file)

    async def _restart(self, reason: str):
        await self.bot.send_log("♻️ Restarting", reason, discord.Color.orange())
        self.bot.restart_requested = True
        await self.bot.close()

    # ------------------------------------------------------------ commands
    @commands.command(name="backup")
    async def backup(self, ctx):
        path = await asyncio.to_thread(make_backup, self.s.tz, self.s.backup_keep)
        if path.stat().st_size < MAX_UPLOAD:
            await ctx.send(f"💾 Backup `{path.name}`", file=discord.File(path))
        else:
            await ctx.send(f"💾 Backup saved on the device as `backups/{path.name}` (too big to upload).")

    @commands.command(name="status")
    async def status(self, ctx):
        temp = cpu_temp_c()
        mem = memory_mb()
        e = discord.Embed(title="🖥️ Bot status", color=discord.Color.blurple(), timestamp=self.s.now())
        e.add_field(name="Bot uptime", value=human_duration((self.s.now() - self.bot.started_at).total_seconds()))
        e.add_field(name="Latency", value=f"{self.bot.latency * 1000:.0f} ms")
        e.add_field(name="Host uptime", value=host_uptime_str() or "n/a")
        e.add_field(name="CPU temp", value=f"{temp:.1f} °C" if temp is not None else "n/a")
        e.add_field(name="RAM", value=f"{mem[0]} / {mem[1]} MB" if mem else "n/a")
        e.add_field(name="Disk", value=disk_str())
        e.add_field(name="Machine", value=platform_str(), inline=False)
        e.add_field(name="Python / discord.py", value=f"{platform.python_version()} / {discord.__version__}")
        e.add_field(name="Git", value=git_commit(), inline=False)
        e.add_field(name="Data", value=f"{len(self.bot.store.all_active())} students · "
                                       f"{len(self.bot.pending.items)} pending", inline=False)
        await ctx.send(embed=e)

    @commands.command(name="logs")
    async def logs(self, ctx, lines: int = 30):
        lines = max(5, min(lines, 400))
        path = LOG_DIR / "bot.log"
        if not path.exists():
            await ctx.send("No log file yet.")
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = "".join(f.readlines()[-lines:])
        if len(tail) < 1900:
            await ctx.send(f"```\n{tail}```")
        else:
            await ctx.send(file=discord.File(io.BytesIO(tail.encode()), filename="bot_log_tail.txt"))

    @commands.command(name="restart")
    async def restart(self, ctx):
        await ctx.send("♻️ Restarting... (back in ~15 s)")
        await self._restart(f"Requested by {ctx.author.mention}")

    @commands.command(name="update")
    async def update(self, ctx):
        if not (BASE_DIR / ".git").exists():
            await ctx.send("❌ This folder is not a git clone, `!update` cannot work here.")
            return
        msg = await ctx.send("⏳ `git pull` ...")
        code, out = await run_cmd("git", "pull", "--ff-only")
        if code != 0:
            await msg.edit(content=f"❌ git pull failed (code {code}):\n```{out[-1500:]}```")
            return
        if "Already up to date" in out or "Already up-to-date" in out:
            await msg.edit(content="✅ Already up to date — nothing to do.")
            return
        _, changed = await run_cmd("git", "diff", "--name-only", "ORIG_HEAD", "HEAD")
        notes = []
        if "requirements.txt" in changed:
            await msg.edit(content="⏳ requirements changed → pip install ...")
            pcode, pout = await run_cmd(sys.executable, "-m", "pip", "install", "-r", "requirements.txt", timeout=1200)
            notes.append("pip install " + ("✅" if pcode == 0 else f"❌\n```{pout[-800:]}```"))
        # safety checks before restarting into new code
        ccode, cout = await run_cmd(sys.executable, "-m", "compileall", "-q", "bot.py", "core", "cogs")
        if ccode != 0:
            await msg.edit(content=f"❌ New code has a syntax error — NOT restarting:\n```{cout[-1500:]}```")
            return
        try:
            Settings()
        except Exception as e:
            await msg.edit(content=f"❌ New config.json is invalid — NOT restarting:\n`{e}`")
            return
        await msg.edit(content=f"✅ Updated:\n```{out[-1200:]}```\nChanged: `{' '.join(changed.split())[:500]}`\n"
                               + "\n".join(notes) + "\n♻️ Restarting... then run `!setup_server` if you changed channels/sections.")
        await self._restart(f"Update by {ctx.author.mention}: {git_commit()}")


async def setup(bot):
    await bot.add_cog(Maintenance(bot))
