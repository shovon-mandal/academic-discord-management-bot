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

from core.config import BASE_DIR, DATA_DIR, LOG_DIR, Settings
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
        au = self.s.raw.get("auto_update", {}) or {}
        self.auto_update_on = bool(au.get("enabled", True))
        self.auto = tasks.loop(minutes=max(2, int(au.get("check_minutes", 5))))(self._auto_update)

    async def cog_load(self):
        if self.s.backup_enabled:
            self.daily.start()
        if self.auto_update_on:
            self.auto.start()

    async def cog_unload(self):
        self.daily.cancel()
        self.auto.cancel()

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

    # ------------------------------------------------------------ update (manual + automatic)
    async def _do_update(self, report, who: str) -> None:
        """git pull → (pip) → safety checks → restart. `report(text)` shows progress."""
        if not (BASE_DIR / ".git").exists():
            await report("❌ This folder is not a git clone, update cannot work here.")
            return
        code, out = await run_cmd("git", "pull", "--ff-only")
        if code != 0:
            await report(f"❌ git pull failed (code {code}):\n```{out[-1500:]}```")
            return
        if "Already up to date" in out or "Already up-to-date" in out:
            await report("✅ Already up to date — nothing to do.")
            return
        _, changed = await run_cmd("git", "diff", "--name-only", "ORIG_HEAD", "HEAD")
        notes = []
        if "requirements.txt" in changed:
            await report("⏳ requirements changed → pip install ...")
            pcode, pout = await run_cmd(sys.executable, "-m", "pip", "install", "-r", "requirements.txt", timeout=1800)
            notes.append("pip install " + ("✅" if pcode == 0 else f"❌\n```{pout[-800:]}```"))
        ccode, cout = await run_cmd(sys.executable, "-m", "compileall", "-q", "bot.py", "core", "cogs")
        if ccode != 0:
            await report(f"❌ New code has a syntax error — NOT restarting:\n```{cout[-1500:]}```")
            return
        try:
            Settings()
        except Exception as e:
            await report(f"❌ New config.json is invalid — NOT restarting:\n`{e}`")
            return
        if "config.json" in changed:
            # after restart, re-post welcome + instructor cards automatically
            (DATA_DIR / ".repost_welcome").parent.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / ".repost_welcome").write_text("1")
            notes.append("config.json changed → welcome messages will be re-posted automatically. "
                         "If you changed channels/sections, run `!setup_server`.")
        await report(f"✅ Updated:\n```{out[-1200:]}```\nChanged: `{' '.join(changed.split())[:500]}`\n"
                     + "\n".join(notes) + "\n♻️ Restarting...")
        await self._restart(f"Update by {who}: {git_commit()}")

    @commands.command(name="update")
    async def update(self, ctx):
        msg = await ctx.send("⏳ `git pull` ...")

        async def report(text):
            await msg.edit(content=text[:1990])
        await self._do_update(report, ctx.author.mention)

    async def _auto_update(self):
        await self.bot.wait_until_ready()
        if not (BASE_DIR / ".git").exists():
            return
        code, _ = await run_cmd("git", "fetch", "--quiet", timeout=120)
        if code != 0:
            return
        code, count = await run_cmd("git", "rev-list", "--count", "HEAD..@{u}")
        if code != 0 or not count.strip().isdigit() or int(count.strip()) == 0:
            return
        log.info("Auto-update: %s new commit(s) on GitHub", count.strip())

        async def report(text):
            await self.bot.send_log("🔄 Auto-update", text[:3900], discord.Color.blurple())
        await self._do_update(report, "auto-update")


async def setup(bot):
    await bot.add_cog(Maintenance(bot))
