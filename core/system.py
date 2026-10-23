"""Host info (works on Tinkerboard/Linux, degrades gracefully on Windows) + backups + shell."""
from __future__ import annotations

import asyncio
import glob
import os
import platform
import shutil
import subprocess
import zipfile
from datetime import datetime
from pathlib import Path

from .config import BACKUP_DIR, BASE_DIR, DATA_DIR


def cpu_temp_c():
    temps = []
    for p in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
        try:
            with open(p) as f:
                temps.append(int(f.read().strip()) / 1000)
        except (OSError, ValueError):
            pass
    return max(temps) if temps else None


def memory_mb():
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0])
        total = info["MemTotal"] // 1024
        avail = info.get("MemAvailable", info.get("MemFree", 0)) // 1024
        return total - avail, total
    except (OSError, KeyError, ValueError):
        return None


def host_uptime_str():
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        return human_duration(secs)
    except (OSError, ValueError):
        return None


def human_duration(secs: int) -> str:
    d, r = divmod(int(secs), 86400)
    h, r = divmod(r, 3600)
    m, _ = divmod(r, 60)
    return (f"{d}d " if d else "") + f"{h}h {m}m"


def disk_str(path: Path = BASE_DIR) -> str:
    u = shutil.disk_usage(path)
    return f"{(u.used) / 1e9:.1f} / {u.total / 1e9:.1f} GB (free {u.free / 1e9:.1f} GB)"


def platform_str() -> str:
    return f"{platform.system()} {platform.release()} · {platform.machine()} · host `{platform.node()}`"


def git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%h · %s (%cr)"],
            cwd=BASE_DIR, capture_output=True, text=True, timeout=5,
        )
        return out.stdout.strip() or "not a git repo"
    except (OSError, subprocess.SubprocessError):
        return "git not available"


async def run_cmd(*args, timeout: int = 300):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(BASE_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"command not found: {args[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode, out.decode("utf-8", "replace")


def make_backup(tz, keep: int = 14) -> Path:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    path = BACKUP_DIR / f"backup_{datetime.now(tz):%Y%m%d_%H%M%S}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        if DATA_DIR.exists():
            for p in DATA_DIR.rglob("*"):
                if p.is_file() and not p.name.endswith(".tmp"):
                    z.write(p, p.relative_to(BASE_DIR))
        cfg = BASE_DIR / "config.json"
        if cfg.exists():
            z.write(cfg, "config.json")
    old = sorted(BACKUP_DIR.glob("backup_*.zip"))
    for o in old[:-keep] if keep > 0 else []:
        try:
            o.unlink()
        except OSError:
            pass
    return path


def zip_files(paths, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in paths:
            p = Path(p)
            if p.exists():
                z.write(p, p.name)
    return dest
