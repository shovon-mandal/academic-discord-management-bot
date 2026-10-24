"""NUBTK class-management Discord bot — entry point.

Run:  python bot.py          (PC)
      systemd service        (Tinkerboard, see scripts/install_tinkerboard.sh)
"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

try:  # Windows console UTF-8
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required. See README (Tinkerboard section).")

import discord
from discord.ext import commands

from core.config import LOG_DIR, Settings
from core.storage import PendingStore, ServerIds, StudentStore

RESTART_EXIT_CODE = 42   # systemd / run_windows.bat restart the bot on this code
CONFIG_ERROR_EXIT_CODE = 3  # systemd will NOT restart on this (fix .env / config first)

EXTENSIONS = (
    "cogs.server_setup",
    "cogs.verification",
    "cogs.admin",
    "cogs.attendance",
    "cogs.maintenance",
    "cogs.panel",
    "cogs.routine",
)

log = logging.getLogger("nubtk")


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(LOG_DIR / "bot.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers[:] = [fh, sh]
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)


class ClassBot(commands.Bot):
    def __init__(self, settings: Settings):
        intents = discord.Intents.default()
        intents.members = True          # needs "Server Members Intent" in Developer Portal
        intents.message_content = True  # needs "Message Content Intent" in Developer Portal
        super().__init__(
            command_prefix=settings.prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=True, users=True),
        )
        self.settings = settings
        self.store = StudentStore(settings)
        self.ids = ServerIds()
        self.pending = PendingStore()
        self.started_at = settings.now()
        self.restart_requested = False

    async def setup_hook(self) -> None:
        for ext in EXTENSIONS:
            await self.load_extension(ext)
        self.add_check(self._global_check)

    async def on_ready(self) -> None:
        g = self.main_guild()
        log.info("Logged in as %s (id %s). Guilds: %s", self.user, self.user.id,
                 ", ".join(f"{x.name} [{x.id}]" for x in self.guilds) or "none")
        if g is None:
            log.warning("Main guild not found. Set GUILD_ID in .env (bot must be invited to that server).")
            return
        await self.send_log("🤖 Bot online", f"Logged in as `{self.user}` on **{g.name}**.", discord.Color.green())

    # ------------------------------------------------------------ helpers
    def main_guild(self) -> discord.Guild | None:
        if self.settings.guild_id:
            return self.get_guild(self.settings.guild_id)
        return self.guilds[0] if len(self.guilds) == 1 else None

    def is_admin(self, user) -> bool:
        if user is None:
            return False
        if user.id in self.settings.admin_ids:
            return True
        if isinstance(user, discord.Member):
            if user.guild.owner_id == user.id:
                return True
            if not self.settings.admin_ids and user.guild_permissions.administrator:
                return True
        return False

    def channel_for(self, key: str):
        g = self.main_guild()
        cid = self.ids.get(key)
        return g.get_channel(cid) if g and cid else None

    def role_for(self, key: str) -> discord.Role | None:
        g = self.main_guild()
        if not g:
            return None
        rid = self.ids.get(key)
        role = g.get_role(rid) if rid else None
        if role is None:
            if key == "role:verified":
                role = discord.utils.get(g.roles, name=self.settings.verified_role_name)
            elif key.startswith("role:sec:"):
                role = discord.utils.get(g.roles, name=self.settings.section_role_name(key.split(":", 2)[2]))
        return role

    async def get_invite_url(self) -> str:
        """Configured invite_link, otherwise a permanent bot-made invite to the welcome channel."""
        if self.settings.raw.get("invite_link"):
            return self.settings.raw["invite_link"]
        g, wc = self.main_guild(), self.channel_for("welcome_channel")
        if not g or not wc:
            return ""
        try:
            for i in await g.invites():
                if i.channel and i.channel.id == wc.id and i.max_age == 0 and i.max_uses == 0:
                    return i.url
            return (await wc.create_invite(max_age=0, max_uses=0, reason="Student join link")).url
        except discord.HTTPException:
            return ""

    async def send_log(self, title: str, description: str = "", color=discord.Color.blurple(),
                       *, admin: bool = False, file: discord.File | None = None, view=None):
        log.info("[%s] %s", title, description.replace("\n", " | ")[:300])
        ch = self.channel_for("admin_channel" if admin else "log_channel") or self.channel_for("admin_channel")
        if ch is None:
            return None
        embed = discord.Embed(title=title, description=description[:4000], color=color, timestamp=self.settings.now())
        kwargs = {"embed": embed}
        if file is not None:
            kwargs["file"] = file
        if view is not None:
            kwargs["view"] = view
        try:
            return await ch.send(**kwargs)
        except discord.HTTPException as e:
            log.warning("Could not send log message: %s", e)
            return None

    # ------------------------------------------------------------ command gate
    async def _global_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        g = self.main_guild()
        if g is None or ctx.guild.id != g.id:
            return False
        if not self.is_admin(ctx.author):
            return False
        if ctx.command and ctx.command.qualified_name in ("setup_server", "reset_server"):  # need confirmation (see server_setup)
            return True  # allowed anywhere (first run: admin channel doesn't exist yet)
        allowed = {c.id for c in (self.channel_for("admin_channel"), self.channel_for("log_channel")) if c}
        return not allowed or ctx.channel.id in allowed

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(f"⚠️ `{error.param.name}` missing.\nUsage: `{self.settings.prefix}"
                           f"{ctx.command.qualified_name} {ctx.command.signature}`")
            return
        if isinstance(error, (commands.BadArgument, commands.UserInputError)):
            await ctx.send(f"⚠️ {error}")
            return
        original = getattr(error, "original", error)
        log.error("Command %s failed", ctx.command, exc_info=original)
        await ctx.send(f"❌ Error: `{str(original)[:400]}`")


def main() -> None:
    setup_logging()
    try:
        settings = Settings()
    except Exception as e:
        log.error("config.json problem: %s", e)
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    if not settings.token or settings.token.upper().startswith("PASTE"):
        log.error("DISCORD_TOKEN missing. Copy .env.example to .env and paste your bot token.")
        sys.exit(CONFIG_ERROR_EXIT_CODE)

    bot = ClassBot(settings)
    try:
        bot.run(settings.token, log_handler=None)
    except discord.LoginFailure:
        log.error("Invalid bot token. Reset it in the Developer Portal and update .env")
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    except discord.PrivilegedIntentsRequired:
        log.error("Enable 'Server Members Intent' and 'Message Content Intent' in Developer Portal → Bot.")
        sys.exit(CONFIG_ERROR_EXIT_CODE)
    if bot.restart_requested:
        log.info("Restart requested — exiting with code %s", RESTART_EXIT_CODE)
        sys.exit(RESTART_EXIT_CODE)


if __name__ == "__main__":
    main()
