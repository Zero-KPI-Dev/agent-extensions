#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import plistlib
import subprocess
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.config import Config, resolve_app_server_executable, resolve_executable  # noqa: E402


APP_SERVER_NOFILE_SOFT_LIMIT = 4096


def target_path(config: Config) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{config.launchd_label}.plist"


def app_server_target_path(config: Config) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{config.app_server_launchd_label}.plist"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["launchctl", *args], text=True, capture_output=True, check=False)


def _bootout(label: str, target: Path) -> None:
    if not target.exists():
        return
    uid = str(os.getuid())
    result = _launchctl("bootout", f"gui/{uid}", str(target))
    if result.returncode not in (0, 36) and result.stderr.strip():
        print(f"launchd 卸载提示（{label}）：{result.stderr.strip()}", file=sys.stderr)


def unload(config: Config) -> None:
    _bootout(config.launchd_label, target_path(config))


def unload_app_server(config: Config) -> None:
    _bootout(config.app_server_launchd_label, app_server_target_path(config))


def install(config: Config, load: bool) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("这个自启动安装器只支持 macOS。")
    config.ensure_directories()
    target = target_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    python = resolve_executable(config.python_binary) or sys.executable
    codex = resolve_executable(config.codex_binary)
    if not codex:
        raise SystemExit(
            f"找不到 Codex 可执行文件：{config.codex_binary}。"
            "请在配置中填写 codex_binary 的绝对路径。"
        )
    path_entries = [str(Path(python).parent), str(Path(codex).parent)]
    path_entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    path_entries.extend(["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    launch_path = os.pathsep.join(dict.fromkeys(entry for entry in path_entries if entry))
    plist = {
        "Label": config.launchd_label,
        "ProgramArguments": [python, str(PLUGIN_ROOT / "scripts" / "start_gateway.py")],
        "WorkingDirectory": str(PLUGIN_ROOT),
        "EnvironmentVariables": {
            "FEISHU_PR_REVIEW_CONFIG": str(config.config_path),
            "PYTHONUNBUFFERED": "1",
            "CODEX_BINARY": codex,
            "CODEX_RUNNER": config.codex_runner,
            "CODEX_APP_SERVER_TRANSPORT": config.codex_app_server_transport,
            "CODEX_APP_SERVER_SOCKET": str(config.codex_app_server_socket),
            "HOME": str(Path.home()),
            "CODEX_HOME": str(Path.home() / ".codex"),
            "PATH": launch_path,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(config.log_dir / "launchd.stdout.log"),
        "StandardErrorPath": str(config.log_dir / "launchd.stderr.log"),
    }
    with target.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    if load:
        unload(config)
        uid = str(os.getuid())
        result = _launchctl("bootstrap", f"gui/{uid}", str(target))
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or "launchctl bootstrap 失败")
        _launchctl("kickstart", "-k", f"gui/{uid}/{config.launchd_label}")
    return target


def install_shared_app_server(config: Config, load: bool) -> Path:
    if sys.platform != "darwin":
        raise SystemExit("这个自启动安装器只支持 macOS。")
    config.ensure_directories()
    target = app_server_target_path(config)
    target.parent.mkdir(parents=True, exist_ok=True)
    codex = resolve_app_server_executable(config.codex_binary)
    if not codex:
        raise SystemExit(
            f"找不到用于共享 app-server 的 Codex 可执行文件：{config.codex_binary}。"
            "请在配置中填写 codex_binary 的绝对路径。"
        )
    path_entries = [str(Path(codex).parent), "/opt/homebrew/bin", "/usr/local/bin"]
    path_entries.extend(os.environ.get("PATH", "").split(os.pathsep))
    path_entries.extend(["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
    launch_path = os.pathsep.join(dict.fromkeys(entry for entry in path_entries if entry))
    plist = {
        "Label": config.app_server_launchd_label,
        "ProgramArguments": [codex, "app-server", "--listen", "unix://"],
        "WorkingDirectory": str(Path.home()),
        "EnvironmentVariables": {
            "HOME": str(Path.home()),
            "CODEX_HOME": str(Path.home() / ".codex"),
            "PATH": launch_path,
        },
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "SoftResourceLimits": {"NumberOfFiles": APP_SERVER_NOFILE_SOFT_LIMIT},
        "StandardOutPath": str(config.log_dir / "app-server.stdout.log"),
        "StandardErrorPath": str(config.log_dir / "app-server.stderr.log"),
    }
    with target.open("wb") as handle:
        plistlib.dump(plist, handle, sort_keys=False)
    if load:
        unload_app_server(config)
        uid = str(os.getuid())
        result = _launchctl("bootstrap", f"gui/{uid}", str(target))
        if result.returncode != 0:
            raise SystemExit(result.stderr.strip() or "共享 Codex app-server 启动失败")
        _launchctl("kickstart", "-k", f"gui/{uid}/{config.app_server_launchd_label}")
        env_result = _launchctl("setenv", "CODEX_APP_SERVER_USE_LOCAL_DAEMON", "1")
        if env_result.returncode != 0 and env_result.stderr.strip():
            print(f"无法设置 Codex Desktop 共享 app-server 环境：{env_result.stderr.strip()}", file=sys.stderr)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="安装或卸载 Feishu PR Review 的 macOS launchd Agent")
    parser.add_argument("--load", action="store_true", help="写入 plist 后立即加载并启动")
    parser.add_argument("--unload", action="store_true", help="卸载并删除现有 Agent")
    args = parser.parse_args()
    config = Config.load()
    if args.unload:
        unload(config)
        unload_app_server(config)
        target = target_path(config)
        if target.exists():
            target.unlink()
        app_target = app_server_target_path(config)
        if app_target.exists():
            app_target.unlink()
        _launchctl("unsetenv", "CODEX_APP_SERVER_USE_LOCAL_DAEMON")
        print(f"已卸载：{target}")
        return
    app_target = None
    if config.codex_runner == "app_server" and config.codex_app_server_transport == "shared_unix":
        app_target = install_shared_app_server(config, load=args.load)
    elif args.load:
        unload_app_server(config)
    target = install(config, load=args.load)
    print(f"已生成：{target}")
    if app_target:
        print(f"已生成共享 app-server：{app_target}")
    if not args.load:
        print("配置确认无误后，再运行同一脚本加 --load 启动网关和共享 app-server。")
    elif app_target:
        print("共享 app-server 已启动；请完全退出并重新打开 Codex App，让它连接到同一个任务通道。")


if __name__ == "__main__":
    main()
