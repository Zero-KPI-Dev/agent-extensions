#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.config import Config, resolve_app_server_executable, resolve_executable  # noqa: E402


def main() -> int:
    config = Config.load()
    bots = list(config.bots.values())
    long_connection_required = any(bot.enabled and bot.transport == "long_connection" for bot in bots)
    try:
        import lark_channel  # type: ignore[import-not-found]
        del lark_channel
        long_connection_sdk = True
    except ImportError:
        long_connection_sdk = not long_connection_required
    codex_path = (
        resolve_app_server_executable(config.codex_binary)
        if config.codex_runner == "app_server"
        else resolve_executable(config.codex_binary)
    )
    app_server_supported = True
    if config.codex_runner == "app_server" and codex_path:
        probe = subprocess.run(
            [codex_path, "app-server", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        app_server_supported = probe.returncode == 0
    elif config.codex_runner == "app_server":
        app_server_supported = False
    checks = {
        "config_exists": config.config_path.exists(),
        "bots_configured": bool(bots),
        "feishu_credentials": bool(bots) and all(bot.app_id and bot.app_secret for bot in bots if bot.enabled),
        "verification_tokens": bool(bots) and all(
            bot.verification_token for bot in bots if bot.enabled and bot.transport == "webhook"
        ),
        "bot_open_ids": bool(bots) and all(
            bot.bot_open_id
            for bot in bots
            if bot.enabled and bot.require_mention and bot.transport == "webhook"
        ),
        "unique_event_paths": len({bot.event_path for bot in bots}) == len(bots),
        "long_connection_sdk": long_connection_sdk,
        "codex_found": bool(codex_path),
        "codex_app_server": app_server_supported,
        "shared_app_server_socket": (
            config.codex_app_server_transport == "stdio" or config.codex_app_server_socket.exists()
        ),
        "review_skill_exists": config.review_skill_path.exists(),
        "repo_mappings": bool(config.repo_roots),
        "mapped_repositories_exist": all(Path(value).expanduser().is_dir() for value in config.repo_roots.values()),
    }
    print(json.dumps({"checks": checks, "config": config.public_summary()}, ensure_ascii=False, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
