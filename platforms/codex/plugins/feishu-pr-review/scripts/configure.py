#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.config import default_config_path  # noqa: E402


def load_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "listen_host": "127.0.0.1",
            "listen_port": 8787,
            "event_path": "/feishu/events",
            "default_bot": "",
            "bots": {},
            "codex_binary": "codex",
            "codex_runner": "app_server",
            "codex_app_server_transport": "shared_unix",
            "codex_app_server_socket": str(Path.home() / ".codex/app-server-control/app-server-control.sock"),
            "codex_sandbox": "read-only",
            "codex_approval_policy": "on-request",
            "codex_approvals_reviewer": "auto_review",
            "max_concurrent_jobs": 4,
            "job_timeout_seconds": 7200,
            "max_feishu_text_length": 3500,
            "feishu_result_format": "card",
            "state_dir": str(path.parent),
            "repo_roots": {},
            "default_repo": "",
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"无法读取配置：{exc}") from exc
    if not isinstance(value, dict):
        raise SystemExit("配置必须是 JSON 对象")
    value.setdefault("bots", {})
    value.setdefault("repo_roots", {})
    return value


def save_raw(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def secret(label: str, default: str = "") -> str:
    suffix = " [已配置，回车保留]" if default else ""
    value = getpass.getpass(f"{label}{suffix}: ").strip()
    return value or default


def yes_no(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    value = input(f"{label} ({suffix}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes", "1", "true", "是"}


def add_bot(path: Path, key: str) -> None:
    raw = load_raw(path)
    bots = raw.setdefault("bots", {})
    existing = bots.get(key, {}) if isinstance(bots.get(key), dict) else {}
    print(f"配置机器人：{key}（直接回车保留已有值）")
    transport = prompt("接入方式（long_connection/webhook）", str(existing.get("transport", "long_connection")))
    if transport not in {"long_connection", "webhook"}:
        raise SystemExit("接入方式只能是 long_connection 或 webhook")
    bot = {
        "display_name": prompt("显示名称", str(existing.get("display_name", key))),
        "event_path": prompt("事件路径", str(existing.get("event_path", f"/feishu/events/{key}"))),
        "transport": transport,
        "feishu_base_url": prompt("飞书 API 地址", str(existing.get("feishu_base_url", "https://open.feishu.cn"))),
        "app_id": prompt("App ID", str(existing.get("app_id", existing.get("feishu_app_id", "")))),
        "app_secret": secret("App Secret", str(existing.get("app_secret", existing.get("feishu_app_secret", "")))),
        "verification_token": (
            secret("Verification Token", str(existing.get("verification_token", existing.get("feishu_verification_token", ""))))
            if transport == "webhook"
            else str(existing.get("verification_token", existing.get("feishu_verification_token", "")))
        ),
        # The long-connection SDK resolves the bot identity from App ID and
        # App Secret after connecting. Webhook parsing has no SDK identity
        # context, so keep the manual Open ID only for that fallback mode.
        "bot_open_id": (
            prompt("机器人 Open ID", str(existing.get("bot_open_id", existing.get("feishu_bot_open_id", ""))))
            if transport == "webhook"
            else str(existing.get("bot_open_id", existing.get("feishu_bot_open_id", "")))
        ),
        "require_mention": yes_no("必须 @ 机器人才能触发", bool(existing.get("require_mention", True))),
        "enabled": yes_no("启用这个机器人", bool(existing.get("enabled", True))),
    }
    duplicate = [other_key for other_key, other in bots.items() if other_key != key and isinstance(other, dict) and other.get("event_path") == bot["event_path"]]
    if duplicate:
        raise SystemExit(f"事件路径已被使用：{', '.join(duplicate)}")
    bots[key] = bot
    raw.setdefault("default_bot", key)
    if not raw.get("default_bot"):
        raw["default_bot"] = key
    save_raw(path, raw)
    print(f"已保存机器人 {key}：{path}")
    print("网关会自动热加载；如果刚安装 launchd，运行 install_launchd.py --load 即可启动。")


def remove_bot(path: Path, key: str) -> None:
    raw = load_raw(path)
    bots = raw.setdefault("bots", {})
    if key not in bots:
        raise SystemExit(f"找不到机器人：{key}")
    if raw.get("default_bot") == key:
        remaining = [other for other in bots if other != key]
        raw["default_bot"] = remaining[0] if remaining else ""
    del bots[key]
    save_raw(path, raw)
    print(f"已删除机器人：{key}")


def list_bots(path: Path) -> None:
    raw = load_raw(path)
    bots = raw.get("bots", {})
    if not bots:
        print("还没有配置机器人。")
        return
    for key, bot in bots.items():
        if not isinstance(bot, dict):
            continue
        marker = " (默认)" if raw.get("default_bot") == key else ""
        state = "启用" if bot.get("enabled", True) else "停用"
        transport = bot.get("transport", "long_connection")
        print(f"- {key}{marker}: {bot.get('display_name', key)} | {state} | {transport} | {bot.get('event_path', '')}")


def set_repo(path: Path, repo_key: str, repo_root: str) -> None:
    normalized = repo_key.strip().lower()
    if normalized.count("/") != 1:
        raise SystemExit("仓库 key 必须是 owner/repo")
    root = Path(repo_root).expanduser().resolve()
    if not root.is_dir():
        raise SystemExit(f"目录不存在：{root}")
    raw = load_raw(path)
    roots = raw.setdefault("repo_roots", {})
    roots[normalized] = str(root)
    if not raw.get("default_repo") and len(roots) == 1:
        raw["default_repo"] = normalized
    save_raw(path, raw)
    print(f"已映射 {normalized} -> {root}")


def remove_repo(path: Path, repo_key: str) -> None:
    raw = load_raw(path)
    roots = raw.setdefault("repo_roots", {})
    normalized = repo_key.strip().lower()
    if normalized not in roots:
        raise SystemExit(f"找不到仓库映射：{normalized}")
    del roots[normalized]
    if raw.get("default_repo") == normalized:
        raw["default_repo"] = next(iter(roots), "") if len(roots) == 1 else ""
    save_raw(path, raw)
    print(f"已删除仓库映射：{normalized}")


def set_default_repo(path: Path, repo_key: str) -> None:
    raw = load_raw(path)
    roots = raw.setdefault("repo_roots", {})
    normalized = repo_key.strip().lower()
    if normalized not in roots:
        raise SystemExit(f"找不到仓库映射：{normalized}")
    raw["default_repo"] = normalized
    save_raw(path, raw)
    print(f"已设置默认仓库：{normalized}")


def list_repos(path: Path) -> None:
    raw = load_raw(path)
    roots = raw.get("repo_roots", {})
    if not roots:
        print("还没有配置本地仓库映射。")
        return
    for key, value in roots.items():
        marker = " (默认)" if raw.get("default_repo") == key else ""
        print(f"- {key}{marker}: {value}")


def set_concurrency(path: Path, count: int) -> None:
    if not 1 <= count <= 8:
        raise SystemExit("并行任务数必须在 1 到 8 之间")
    raw = load_raw(path)
    raw["max_concurrent_jobs"] = count
    save_raw(path, raw)
    print(f"已设置最大并行任务数为 {count}：{path}")
    print("该设置会在飞书网关重启后生效。")


def main() -> int:
    parser = argparse.ArgumentParser(description="交互式管理 Feishu PR Review 配置")
    parser.add_argument("--config", type=Path, default=default_config_path())
    subparsers = parser.add_subparsers(dest="resource", required=True)

    bot_parser = subparsers.add_parser("bot", help="管理机器人")
    bot_subparsers = bot_parser.add_subparsers(dest="action", required=True)
    add_parser = bot_subparsers.add_parser("add", help="新增或修改机器人")
    add_parser.add_argument("key")
    remove_parser = bot_subparsers.add_parser("remove", help="删除机器人")
    remove_parser.add_argument("key")
    bot_subparsers.add_parser("list", help="列出机器人")

    repo_parser = subparsers.add_parser("repo", help="管理本地仓库映射")
    repo_subparsers = repo_parser.add_subparsers(dest="action", required=True)
    set_parser = repo_subparsers.add_parser("set", help="新增或修改仓库映射")
    set_parser.add_argument("repo_key")
    set_parser.add_argument("repo_root")
    remove_repo_parser = repo_subparsers.add_parser("remove", help="删除仓库映射")
    remove_repo_parser.add_argument("repo_key")
    default_repo_parser = repo_subparsers.add_parser("default", help="设置默认仓库，用于只输入 PR 号")
    default_repo_parser.add_argument("repo_key")
    repo_subparsers.add_parser("list", help="列出仓库映射")

    runtime_parser = subparsers.add_parser("runtime", help="管理后台执行参数")
    runtime_subparsers = runtime_parser.add_subparsers(dest="action", required=True)
    concurrency_parser = runtime_subparsers.add_parser("concurrency", help="设置最大并行检视任务数（1-8）")
    concurrency_parser.add_argument("count", type=int)

    args = parser.parse_args()
    if args.resource == "bot":
        if args.action == "add":
            add_bot(args.config, args.key)
        elif args.action == "remove":
            remove_bot(args.config, args.key)
        else:
            list_bots(args.config)
    elif args.resource == "repo":
        if args.action == "set":
            set_repo(args.config, args.repo_key, args.repo_root)
        elif args.action == "remove":
            remove_repo(args.config, args.repo_key)
        elif args.action == "default":
            set_default_repo(args.config, args.repo_key)
        else:
            list_repos(args.config)
    elif args.resource == "runtime":
        set_concurrency(args.config, args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
