from __future__ import annotations

import os
import platform
import plistlib
import re
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Config


_CACHE_TTL_SECONDS = 5.0
_cache_lock = threading.Lock()
_cache_key: tuple[str, str] | None = None
_cache_at = 0.0
_cache_value: dict[str, Any] | None = None


def _launchd_pid(label: str) -> int | None:
    try:
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{label}"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"^\s*pid\s*=\s*(\d+)\s*$", result.stdout, re.MULTILINE)
    return int(match.group(1)) if match else None


def _numeric_fd_count(pid: int) -> int | None:
    lsof = shutil.which("lsof")
    if not lsof:
        return None
    try:
        result = subprocess.run(
            [lsof, "-nP", "-p", str(pid), "-Ff"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return sum(1 for line in result.stdout.splitlines() if re.fullmatch(r"f\d+[a-z]*", line))


def _configured_soft_limit(config: Config) -> int | None:
    path = Path.home() / "Library" / "LaunchAgents" / f"{config.app_server_launchd_label}.plist"
    try:
        with path.open("rb") as handle:
            plist = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return None
    limits = plist.get("SoftResourceLimits")
    value = limits.get("NumberOfFiles") if isinstance(limits, dict) else None
    return value if isinstance(value, int) and value > 0 else None


def _restart_required(config: Config, pid: int) -> bool | None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{config.app_server_launchd_label}.plist"
    try:
        plist_mtime = plist_path.stat().st_mtime
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return None
        started_at = datetime.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y").timestamp()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    return plist_mtime > started_at + 1.0


def _uncached_status(config: Config) -> dict[str, Any]:
    if platform.system() != "Darwin" or config.codex_app_server_transport != "shared_unix":
        return {"available": False, "status": "not_applicable"}

    pid = _launchd_pid(config.app_server_launchd_label)
    soft_limit = _configured_soft_limit(config)
    if pid is None:
        return {
            "available": False,
            "status": "not_running",
            "configured_soft_limit": soft_limit,
            "restart_required": None,
        }

    open_fds = _numeric_fd_count(pid)
    restart_required = _restart_required(config, pid)
    usage_percent = None
    status = "unknown"
    if restart_required:
        status = "restart_required"
    elif open_fds is not None and soft_limit:
        usage_percent = round(open_fds * 100 / soft_limit, 1)
        if usage_percent >= 90:
            status = "critical"
        elif usage_percent >= 75:
            status = "warning"
        else:
            status = "healthy"

    return {
        "available": open_fds is not None,
        "status": status,
        "pid": pid,
        "open_fds": open_fds,
        "configured_soft_limit": soft_limit,
        "restart_required": restart_required,
        "usage_percent": usage_percent,
    }


def app_server_resource_status(config: Config) -> dict[str, Any]:
    """Return a small, cached descriptor-pressure snapshot for health APIs."""

    global _cache_at, _cache_key, _cache_value
    key = (config.app_server_launchd_label, config.codex_app_server_transport)
    now = time.monotonic()
    with _cache_lock:
        if _cache_key == key and _cache_value is not None and now - _cache_at < _CACHE_TTL_SECONDS:
            return dict(_cache_value)
        value = _uncached_status(config)
        _cache_key = key
        _cache_at = now
        _cache_value = dict(value)
        return value
