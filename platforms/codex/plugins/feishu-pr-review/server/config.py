from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def default_state_dir() -> Path:
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Codex" / "feishu-pr-review"
    return Path.home() / ".local" / "share" / "codex" / "feishu-pr-review"


def default_config_path() -> Path:
    return default_state_dir() / "config.json"


def default_codex_app_server_socket() -> Path:
    return Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"


def bundled_review_skill_path() -> Path:
    return Path(__file__).resolve().parents[1] / "skills" / "review-pr-with-panel" / "SKILL.md"


def _is_legacy_external_review_skill_path(path: Path) -> bool:
    """Recognize the external per-user path used before the Skill was bundled."""

    return tuple(path.parts[-4:]) == (".codex", "skills", "review-pr-with-panel", "SKILL.md")


def _review_skill_path(file_config: dict[str, Any]) -> Path:
    override = os.environ.get("REVIEW_SKILL_PATH", "").strip()
    if override:
        return Path(override).expanduser()

    configured = str(file_config.get("review_skill_path") or "").strip()
    if configured:
        candidate = Path(configured).expanduser()
        if not _is_legacy_external_review_skill_path(candidate):
            return candidate
    return bundled_review_skill_path()


def _env(name: str, default: Any = None) -> Any:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def resolve_executable(value: str) -> str | None:
    """Resolve a command for both an interactive shell and launchd."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    found = shutil.which(raw)
    if found:
        return found
    if raw == "codex":
        for fallback in (
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / ".codex" / "bin" / "codex",
        ):
            if fallback.is_file():
                return str(fallback)
    return None


def resolve_app_server_executable(value: str) -> str | None:
    """Prefer the Codex binary shipped with Desktop for the shared daemon."""
    raw = str(value or "").strip()
    if not raw:
        return None
    candidate = Path(raw).expanduser()
    if candidate.is_absolute():
        return str(candidate) if candidate.is_file() else None
    if raw == "codex":
        for fallback in (
            Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
            Path.home() / ".codex" / "plugins" / ".plugin-appserver" / "codex",
            Path.home() / ".codex" / "packages" / "standalone" / "current" / "bin" / "codex",
        ):
            if fallback.is_file():
                return str(fallback)
    return resolve_executable(raw)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"配置文件必须是 JSON 对象: {path}")
    return value


def _repo_key_from_url(pr_url: str) -> str:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2].lower() != "pull":
        return ""
    return f"{parts[0]}/{parts[1]}".lower()


@dataclass(frozen=True)
class Config:
    config_path: Path
    state_dir: Path
    db_path: Path
    log_dir: Path
    listen_host: str
    listen_port: int
    bots: dict[str, "BotConfig"]
    default_bot: str
    python_binary: str
    codex_binary: str
    codex_runner: str
    codex_app_server_transport: str
    codex_app_server_socket: Path
    review_skill_path: Path
    codex_sandbox: str
    codex_approval_policy: str
    codex_approvals_reviewer: str
    max_concurrent_jobs: int
    job_timeout_seconds: int
    max_feishu_text_length: int
    feishu_result_format: str
    repo_roots: dict[str, str]
    default_repo: str
    launchd_label: str
    app_server_launchd_label: str

    @classmethod
    def load(cls) -> "Config":
        configured_path = _env("FEISHU_PR_REVIEW_CONFIG")
        config_path = Path(configured_path).expanduser() if configured_path else default_config_path()
        file_config = _read_json(config_path)

        state_dir_value = _env("FEISHU_PR_REVIEW_STATE_DIR", file_config.get("state_dir"))
        state_dir = Path(state_dir_value).expanduser() if state_dir_value else default_state_dir()

        repo_roots: Any = _env("REVIEW_REPO_ROOTS_JSON", file_config.get("repo_roots", {}))
        if isinstance(repo_roots, str):
            try:
                repo_roots = json.loads(repo_roots)
            except json.JSONDecodeError as exc:
                raise RuntimeError("REVIEW_REPO_ROOTS_JSON 不是有效的 JSON") from exc
        if not isinstance(repo_roots, dict):
            raise RuntimeError("repo_roots 必须是 owner/repo 到本地目录的 JSON 映射")
        normalized_roots = {
            str(key).strip().lower(): str(value).strip()
            for key, value in repo_roots.items()
            if str(key).strip() and str(value).strip()
        }
        default_repo = str(_env("REVIEW_DEFAULT_REPO", file_config.get("default_repo", ""))).strip().lower()

        bots = _load_bots(file_config)
        default_bot = str(_env("FEISHU_DEFAULT_BOT", file_config.get("default_bot", ""))).strip()
        if default_bot not in bots:
            default_bot = next(iter(bots), "")

        codex_runner = str(_env("CODEX_RUNNER", file_config.get("codex_runner", "app_server"))).strip().lower()
        if codex_runner not in {"app_server", "exec"}:
            raise RuntimeError("codex_runner 必须是 app_server 或 exec")

        codex_app_server_transport = str(
            _env("CODEX_APP_SERVER_TRANSPORT", file_config.get("codex_app_server_transport", "shared_unix"))
        ).strip().lower()
        if codex_app_server_transport not in {"shared_unix", "stdio"}:
            raise RuntimeError("codex_app_server_transport 必须是 shared_unix 或 stdio")
        socket_value = _env(
            "CODEX_APP_SERVER_SOCKET",
            file_config.get("codex_app_server_socket", default_codex_app_server_socket()),
        )
        codex_app_server_socket = Path(socket_value).expanduser() if socket_value else default_codex_app_server_socket()

        codex_approval_policy = str(
            _env("CODEX_APPROVAL_POLICY", file_config.get("codex_approval_policy", "on-request"))
        ).strip().lower()
        if codex_approval_policy not in {"untrusted", "on-request", "never"}:
            raise RuntimeError("codex_approval_policy 必须是 untrusted、on-request 或 never")

        codex_approvals_reviewer = str(
            _env("CODEX_APPROVALS_REVIEWER", file_config.get("codex_approvals_reviewer", "auto_review"))
        ).strip().lower()
        if codex_approvals_reviewer not in {"user", "auto_review", "guardian_subagent"}:
            raise RuntimeError("codex_approvals_reviewer 必须是 user、auto_review 或 guardian_subagent")

        max_concurrent_jobs = _int(
            _env("REVIEW_MAX_CONCURRENT_JOBS", file_config.get("max_concurrent_jobs", 4)),
            4,
        )
        if not 1 <= max_concurrent_jobs <= 8:
            raise RuntimeError("max_concurrent_jobs 必须在 1 到 8 之间")

        launchd_label = str(
            _env("FEISHU_REVIEW_LAUNCHD_LABEL", file_config.get("launchd_label", "com.openai.codex.feishu-pr-review"))
        )
        feishu_result_format = str(
            _env("FEISHU_RESULT_FORMAT", file_config.get("feishu_result_format", "card"))
        ).strip().lower()
        if feishu_result_format not in {"card", "text"}:
            raise RuntimeError("feishu_result_format 必须是 card 或 text")
        return cls(
            config_path=config_path,
            state_dir=state_dir,
            db_path=Path(_env("FEISHU_PR_REVIEW_DB", file_config.get("db_path", state_dir / "state.sqlite3"))).expanduser(),
            log_dir=Path(_env("FEISHU_PR_REVIEW_LOG_DIR", file_config.get("log_dir", state_dir / "logs"))).expanduser(),
            listen_host=str(_env("FEISHU_PR_REVIEW_HOST", file_config.get("listen_host", "127.0.0.1"))),
            listen_port=_int(_env("FEISHU_PR_REVIEW_PORT", file_config.get("listen_port", 8787)), 8787),
            bots=bots,
            default_bot=default_bot,
            python_binary=str(_env("FEISHU_PR_REVIEW_PYTHON", file_config.get("python_binary", sys.executable))),
            codex_binary=str(_env("CODEX_BINARY", file_config.get("codex_binary", "codex"))),
            codex_runner=codex_runner,
            codex_app_server_transport=codex_app_server_transport,
            codex_app_server_socket=codex_app_server_socket,
            review_skill_path=_review_skill_path(file_config),
            codex_sandbox=str(_env("CODEX_SANDBOX", file_config.get("codex_sandbox", "read-only"))),
            codex_approval_policy=codex_approval_policy,
            codex_approvals_reviewer=codex_approvals_reviewer,
            max_concurrent_jobs=max_concurrent_jobs,
            job_timeout_seconds=_int(_env("REVIEW_JOB_TIMEOUT_SECONDS", file_config.get("job_timeout_seconds", 7200)), 7200),
            max_feishu_text_length=_int(_env("FEISHU_MAX_TEXT_LENGTH", file_config.get("max_feishu_text_length", 3500)), 3500),
            feishu_result_format=feishu_result_format,
            repo_roots=normalized_roots,
            default_repo=default_repo,
            launchd_label=launchd_label,
            app_server_launchd_label=str(
                _env(
                    "CODEX_APP_SERVER_LAUNCHD_LABEL",
                    file_config.get("app_server_launchd_label", f"{launchd_label}.app-server"),
                )
            ),
        )

    def ensure_directories(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def repo_key(self, pr_url: str) -> str:
        return _repo_key_from_url(pr_url)

    def default_repo_key(self) -> str | None:
        if self.default_repo and self.default_repo in self.repo_roots:
            return self.default_repo
        if len(self.repo_roots) == 1:
            return next(iter(self.repo_roots))
        return None

    def repo_root_for(self, pr_url: str) -> Path | None:
        key = self.repo_key(pr_url)
        raw = self.repo_roots.get(key)
        if not raw:
            return None
        return Path(raw).expanduser()

    def public_summary(self) -> dict[str, Any]:
        return {
            "config_path": str(self.config_path),
            "state_dir": str(self.state_dir),
            "listen": f"{self.listen_host}:{self.listen_port}",
            "default_bot": self.default_bot,
            "bots": {key: bot.public_summary() for key, bot in self.bots.items()},
            "python_binary": self.python_binary,
            "codex_binary": self.codex_binary,
            "codex_runner": self.codex_runner,
            "codex_app_server_transport": self.codex_app_server_transport,
            "codex_app_server_socket": str(self.codex_app_server_socket),
            "codex_approval_policy": self.codex_approval_policy,
            "codex_approvals_reviewer": self.codex_approvals_reviewer,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "review_skill_path": str(self.review_skill_path),
            "review_skill_bundled": self.review_skill_path == bundled_review_skill_path(),
            "review_skill_exists": self.review_skill_path.exists(),
            "feishu_result_format": self.feishu_result_format,
            "repo_mappings": sorted(self.repo_roots),
            "default_repo": self.default_repo_key(),
            "launchd_label": self.launchd_label,
            "app_server_launchd_label": self.app_server_launchd_label,
        }

    def bot_for_path(self, path: str) -> "BotConfig | None":
        normalized = path.split("?", 1)[0].rstrip("/") or "/"
        for bot in self.bots.values():
            if bot.enabled and bot.event_path.rstrip("/") == normalized:
                return bot
        return None

    def bot(self, key: str | None) -> "BotConfig | None":
        return self.bots.get(key or self.default_bot)


@dataclass(frozen=True)
class BotConfig:
    key: str
    display_name: str
    event_path: str
    feishu_base_url: str
    app_id: str
    app_secret: str
    verification_token: str
    bot_open_id: str
    transport: str = "long_connection"
    require_mention: bool = True
    enabled: bool = True

    def public_summary(self) -> dict[str, Any]:
        return {
            "display_name": self.display_name,
            "event_path": self.event_path,
            "transport": self.transport,
            "enabled": self.enabled,
            "credentials_configured": bool(self.app_id and self.app_secret),
            "verification_token_configured": bool(self.verification_token),
            "bot_open_id_configured": bool(self.bot_open_id),
            "require_mention": self.require_mention,
        }


def _load_bots(file_config: dict[str, Any]) -> dict[str, BotConfig]:
    raw_bots = file_config.get("bots")
    if not isinstance(raw_bots, dict) or not raw_bots:
        # Backward-compatible single-bot shape for the first version.
        raw_bots = {
            "default": {
                "display_name": "PR 检视机器人",
                "event_path": file_config.get("event_path", "/feishu/events"),
                "transport": file_config.get("transport", "long_connection"),
                "feishu_base_url": file_config.get("feishu_base_url", "https://open.feishu.cn"),
                "app_id": file_config.get("feishu_app_id", _env("FEISHU_APP_ID", "")),
                "app_secret": file_config.get("feishu_app_secret", _env("FEISHU_APP_SECRET", "")),
                "verification_token": file_config.get("feishu_verification_token", _env("FEISHU_VERIFICATION_TOKEN", "")),
                "bot_open_id": file_config.get("feishu_bot_open_id", _env("FEISHU_BOT_OPEN_ID", "")),
                "require_mention": file_config.get("require_mention", _env("FEISHU_REQUIRE_MENTION", True)),
            }
        }

    bots: dict[str, BotConfig] = {}
    for raw_key, raw_value in raw_bots.items():
        if not isinstance(raw_value, dict):
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        event_path = str(raw_value.get("event_path", f"/feishu/events/{key}")).strip()
        if not event_path.startswith("/"):
            event_path = f"/{event_path}"
        transport = str(raw_value.get("transport", "long_connection")).strip().lower()
        if transport not in {"long_connection", "webhook"}:
            raise RuntimeError(f"机器人 {key} 的 transport 必须是 long_connection 或 webhook")
        bots[key] = BotConfig(
            key=key,
            display_name=str(raw_value.get("display_name", key)),
            event_path=event_path.rstrip("/") or "/",
            feishu_base_url=str(raw_value.get("feishu_base_url", "https://open.feishu.cn")).rstrip("/"),
            app_id=str(raw_value.get("app_id", raw_value.get("feishu_app_id", ""))),
            app_secret=str(raw_value.get("app_secret", raw_value.get("feishu_app_secret", ""))),
            verification_token=str(raw_value.get("verification_token", raw_value.get("feishu_verification_token", ""))),
            bot_open_id=str(raw_value.get("bot_open_id", raw_value.get("feishu_bot_open_id", ""))),
            transport=transport,
            require_mention=_bool(raw_value.get("require_mention", True), True),
            enabled=_bool(raw_value.get("enabled", True), True),
        )
    return bots
