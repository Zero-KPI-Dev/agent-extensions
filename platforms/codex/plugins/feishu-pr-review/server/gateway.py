from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from .codex_app_server import (
    CodexAppServerCancelled,
    CodexAppServerClient,
    CodexAppServerError,
    CodexAppServerTimeout,
)
from .config import BotConfig, Config, resolve_executable
from .db import StateStore
from .feishu import (
    FeishuClient,
    FeishuError,
    FeishuEvent,
    build_help_text,
    extract_pr_number,
    extract_pr_url,
    is_help_request,
    parse_event,
)
from .long_connection import LongConnectionManager
from .resource_health import app_server_resource_status


LOGGER = logging.getLogger("feishu-pr-review")


def _configure_logging(log_dir: Path) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_dir / "gateway.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
    LOGGER.setLevel(logging.INFO)
    LOGGER.addHandler(handler)
    LOGGER.addHandler(logging.StreamHandler())


def _message_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("text", "message", "value"):
            if isinstance(value.get(key), str) and value[key].strip():
                return value[key]
        content = value.get("content")
        if content is not None:
            return _message_text(content)
    if isinstance(value, list):
        return "\n".join(part for part in (_message_text(item) for item in value) if part)
    return ""


def _extract_codex_messages(output: str) -> list[str]:
    messages: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        item = event.get("item") if isinstance(event, dict) else None
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in {"agent_message", "assistant_message", "message"}:
            text = _message_text(item)
            if text:
                messages.append(text.strip())
    return messages


def _kill_process_group(pid: int, sig: int = signal.SIGTERM) -> None:
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return


def _pr_task_title(pr_url: str, repo_root: Path | None = None) -> str | None:
    """Return the review Skill's ``Repository#number`` task title."""
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    if len(parts) < 4 or parts[2].lower() != "pull" or not parts[3].isdigit():
        return None
    repository = parts[1]
    if repo_root is not None and repo_root.name.lower() == repository.lower():
        repository = repo_root.name
    return f"{repository}#{int(parts[3])}"


class ReviewWorker(threading.Thread):
    def __init__(
        self,
        config_provider: Callable[[], Config],
        client_provider: Callable[[str], FeishuClient | None],
        store: StateStore,
        stop_event: threading.Event,
        worker_number: int = 1,
    ):
        super().__init__(name=f"review-worker-{worker_number}", daemon=True)
        self.config_provider = config_provider
        self.client_provider = client_provider
        self.store = store
        self.stop_event = stop_event

    def run(self) -> None:
        LOGGER.info("review worker started: %s", self.name)
        while not self.stop_event.is_set():
            job = self.store.claim_next_job()
            if not job:
                self.stop_event.wait(1.0)
                continue
            try:
                self.execute(job)
            except Exception as exc:  # noqa: BLE001 - worker must finish the job durably
                LOGGER.exception("job %s crashed", job["job_id"])
                self._finish_failure(job, f"后台执行异常：{exc}")
        LOGGER.info("review worker stopped: %s", self.name)

    def _send(
        self,
        chat_id: str | None,
        text: str,
        *,
        pr_url: str | None = None,
        bot_key: str | None = None,
    ) -> str:
        if not chat_id:
            return "skipped"
        job_bot_key = bot_key if bot_key is not None else getattr(self, "_current_bot_key", "")
        client = self.client_provider(job_bot_key)
        config = self.config_provider()
        if client is None:
            return "failed: bot configuration is missing or disabled"
        try:
            if config.feishu_result_format == "card":
                try:
                    client.send_review_card(
                        chat_id,
                        text,
                        config.max_feishu_text_length,
                        pr_url=pr_url,
                    )
                    return "sent_card"
                except FeishuError as card_exc:
                    # Keep delivery reliable when an older tenant or an
                    # account policy rejects interactive cards.
                    LOGGER.warning("failed to send Feishu card, falling back to text: %s", card_exc)
                    client.send_text(chat_id, text, config.max_feishu_text_length)
                    return "sent_text_fallback"
            client.send_text(chat_id, text, config.max_feishu_text_length)
            return "sent"
        except FeishuError as exc:
            LOGGER.error("failed to send Feishu message: %s", exc)
            return f"failed: {exc}"

    def _send_job(self, job: dict[str, Any], text: str) -> str:
        targets = self.store.delivery_targets(job["job_id"])
        if not targets:
            return self._send(job.get("chat_id"), text, pr_url=job.get("pr_url"))
        deliveries = [
            self._send(
                target["chat_id"],
                text,
                pr_url=job.get("pr_url"),
                bot_key=target["bot_key"],
            )
            for target in targets
        ]
        if len(deliveries) == 1:
            return deliveries[0]
        failures = [status for status in deliveries if status.startswith("failed")]
        if failures:
            return f"partial_failure: {'; '.join(failures)}"
        return "sent_multiple"

    @staticmethod
    def _delivery_succeeded(delivery: str) -> bool:
        return delivery in {
            "sent",
            "sent_card",
            "sent_text_fallback",
            "sent_multiple",
            "skipped",
        }

    def _finish_failure(self, job: dict[str, Any], error: str) -> None:
        report = f"PR 检视失败\n\n{error}"
        delivery = self._send_job(job, report)
        self.store.finish(
            job["job_id"],
            status="failed",
            result_text=report,
            error_text=error,
            delivery_status=delivery,
        )

    def _prompt(self, job: dict[str, Any]) -> str:
        config = self.config_provider()
        return f"""你正在执行一个由飞书机器人触发的 GitHub PR 检视任务。

必须使用本机 Skill：{config.review_skill_path}
Skill 名称：review-pr-with-panel

目标 PR：{job['pr_url']}
用户原始请求：{job['request_text']}

请严格执行该 Skill 的完整流程：根据当前 PR 状态选择正确的 review mode，使用 Leader 加两个独立的 A/B 验证代理，保持只读，不实现修复。

这不是 report-only 请求。对于有效的 GitHub PR URL，请遵循 Skill 的 GitHub 发布规则：共识后的可行动检视意见应发布到 GitHub PR；能定位到当前 diff 行时发布行内意见，否则发布到 review body。不要自动 approve、request changes 或关闭线程。若已经存在历史检视结果，请按 Skill 的 finding lineage 与 follow-up 规则避免重复意见。

任务结束时，请返回适合飞书回传的中文摘要。以下字段必须逐项明确给出：PR、review_id、mode（精确使用 INITIAL_REVIEW、FIX_VERIFICATION、INCREMENTAL_REREVIEW 或 NO_NEW_REVISION）、结论、发现数量（按 Critical/High/Medium/Low/Suggestion 分级）、主要发现摘要、GitHub 发布状态、未发布或阻塞原因（如有）。即使某项为空或数量为 0 也不要省略；不要只返回“已完成”。"""

    def execute(self, job: dict[str, Any]) -> None:
        job_id = job["job_id"]
        self._current_bot_key = str(job.get("bot_key") or "")
        config = self.config_provider()
        repo_root = config.repo_root_for(job["pr_url"])
        if repo_root is None:
            self._finish_failure(job, f"未配置仓库映射：{job['repo_key']}。请在 config.json 的 repo_roots 中加入本地仓库目录。")
            return
        if not repo_root.is_dir() or not ((repo_root / ".git").exists() or (repo_root / ".git").is_file()):
            self._finish_failure(job, f"仓库目录不可用或不是 Git 仓库：{repo_root}")
            return
        if not config.review_skill_path.exists():
            self._finish_failure(job, f"找不到检视 Skill：{config.review_skill_path}")
            return

        executable = resolve_executable(config.codex_binary) or config.codex_binary
        env = os.environ.copy()
        panel_state_dir = config.state_dir / "review-panel-state"
        panel_state_dir.mkdir(parents=True, exist_ok=True)
        env["REVIEW_PR_PANEL_STATE_DIR"] = str(panel_state_dir)
        env["FEISHU_REVIEW_AUTOMATION"] = "1"
        env["FEISHU_REVIEW_JOB_ID"] = job_id
        env["FEISHU_REVIEW_PR_URL"] = job["pr_url"]

        log_path = config.log_dir / f"{job_id}.codex.log"
        LOGGER.info("starting job %s for %s in %s", job_id, job["pr_url"], repo_root)
        if config.codex_runner == "exec":
            self._execute_with_exec(job, repo_root, executable, env, log_path)
            return
        self._execute_with_app_server(job, repo_root, executable, env, log_path)

    def _execute_with_exec(
        self,
        job: dict[str, Any],
        repo_root: Path,
        executable: str,
        env: dict[str, str],
        log_path: Path,
    ) -> None:
        """Compatibility path for an explicit codex_runner=exec rollback."""
        job_id = job["job_id"]
        config = self.config_provider()
        args = [
            executable,
            "exec",
            "--json",
            "--sandbox",
            config.codex_sandbox,
            self._prompt(job),
        ]
        try:
            process = subprocess.Popen(
                args,
                cwd=repo_root,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                start_new_session=True,
            )
        except OSError as exc:
            self._finish_failure(job, f"无法启动 Codex：{exc}")
            return

        self.store.set_pid(job_id, process.pid)
        try:
            output, _ = process.communicate(timeout=config.job_timeout_seconds)
        except subprocess.TimeoutExpired:
            LOGGER.error("job %s exceeded timeout", job_id)
            _kill_process_group(process.pid, signal.SIGTERM)
            output, _ = process.communicate()
            self._finish_failure(job, f"检视超过 {config.job_timeout_seconds} 秒，已终止。")
            self._write_log(log_path, output)
            return
        finally:
            self.store.set_pid(job_id, None)

        self._write_log(log_path, output)
        if self.store.is_cancel_requested(job_id):
            report = "PR 检视任务已取消。"
            delivery = self._send_job(job, report)
            self.store.finish(job_id, status="cancelled", result_text=report, delivery_status=delivery)
            return

        messages = _extract_codex_messages(output)
        report = messages[-1] if messages else ""
        if process.returncode != 0:
            tail = output[-1200:].strip()
            self._finish_failure(job, f"Codex 退出码 {process.returncode}。\n\n{tail}")
            return
        if not report:
            self._finish_failure(job, "Codex 没有返回可回传的检视摘要，请查看任务日志。")
            return

        delivery = self._send_job(job, report)
        status = "succeeded"
        error = None if self._delivery_succeeded(delivery) else delivery
        self.store.finish(
            job_id,
            status=status,
            result_text=report,
            error_text=error,
            delivery_status=delivery,
        )
        LOGGER.info("job %s completed with delivery=%s", job_id, delivery)

    def _execute_with_app_server(
        self,
        job: dict[str, Any],
        repo_root: Path,
        executable: str,
        env: dict[str, str],
        log_path: Path,
    ) -> None:
        job_id = job["job_id"]
        config = self.config_provider()
        client = CodexAppServerClient(
            executable,
            transport=config.codex_app_server_transport,
            socket_path=config.codex_app_server_socket,
        )
        task_title = _pr_task_title(job["pr_url"], repo_root)

        def on_pid(pid: int | None) -> None:
            self.store.set_pid(job_id, pid)

        def on_thread_created(thread_id: str) -> None:
            self.store.set_codex_thread_id(job_id, thread_id)
            LOGGER.info("job %s created Codex App thread %s", job_id, thread_id)

        result = None
        try:
            result = client.run(
                cwd=str(repo_root),
                prompt=self._prompt(job),
                sandbox=config.codex_sandbox,
                timeout_seconds=config.job_timeout_seconds,
                env=env,
                approval_policy=config.codex_approval_policy,
                approvals_reviewer=config.codex_approvals_reviewer,
                thread_name=task_title,
                should_cancel=lambda: self.store.is_cancel_requested(job_id),
                on_pid=on_pid,
                on_thread_created=on_thread_created,
            )
        except CodexAppServerCancelled:
            report = "PR 检视任务已取消。"
            delivery = self._send_job(job, report)
            self.store.finish(job_id, status="cancelled", result_text=report, delivery_status=delivery)
            return
        except CodexAppServerTimeout:
            self._finish_failure(job, f"检视超过 {config.job_timeout_seconds} 秒，已终止。")
            return
        except CodexAppServerError as exc:
            self._finish_failure(job, f"Codex app-server 执行失败：{exc}")
            return
        finally:
            self._write_log(log_path, client.protocol_log())

        if result is None or not result.report:
            self._finish_failure(job, "Codex app-server 没有返回可回传的检视摘要，请查看任务日志。")
            return

        delivery = self._send_job(job, result.report)
        status = "succeeded"
        error = None if self._delivery_succeeded(delivery) else delivery
        self.store.finish(
            job_id,
            status=status,
            result_text=result.report,
            error_text=error,
            delivery_status=delivery,
        )
        LOGGER.info(
            "job %s completed with app-server thread=%s delivery=%s",
            job_id,
            result.thread_id,
            delivery,
        )

    @staticmethod
    def _write_log(path: Path, output: str) -> None:
        try:
            path.write_text(output, encoding="utf-8")
        except OSError as exc:
            LOGGER.error("failed to write job log %s: %s", path, exc)


class Gateway:
    def __init__(self, config: Config):
        self._config_lock = threading.RLock()
        self.config = config
        self.config.ensure_directories()
        self._config_mtime = self._config_file_mtime()
        self.store = StateStore(config.db_path)
        self.store.initialize()
        recovered = self.store.requeue_running_jobs()
        if recovered:
            LOGGER.warning("requeued %s interrupted running jobs", recovered)
        self._clients: dict[str, FeishuClient] = {}
        self.stop_event = threading.Event()
        self.workers = [
            ReviewWorker(
                self.current_config,
                self.client_for,
                self.store,
                self.stop_event,
                worker_number=index + 1,
            )
            for index in range(config.max_concurrent_jobs)
        ]
        self.long_connection = LongConnectionManager(self)

    def _config_file_mtime(self) -> int | None:
        try:
            return self.config.config_path.stat().st_mtime_ns
        except OSError:
            return None

    def _maybe_reload(self) -> None:
        current_mtime = self._config_file_mtime()
        if current_mtime == self._config_mtime:
            return
        try:
            new_config = Config.load()
            new_config.ensure_directories()
        except Exception as exc:  # noqa: BLE001 - keep serving with the last valid config
            LOGGER.error("配置文件变更但加载失败，继续使用旧配置：%s", exc)
            self._config_mtime = current_mtime
            return
        if new_config.db_path != self.store.db_path:
            LOGGER.error("运行中不支持修改 db_path，继续使用旧数据库：%s", self.store.db_path)
        with self._config_lock:
            self.config = new_config
            self._clients = {}
            self._config_mtime = current_mtime
        LOGGER.info("已热加载飞书机器人配置：%s", ", ".join(new_config.bots) or "无")

    def current_config(self) -> Config:
        self._maybe_reload()
        with self._config_lock:
            return self.config

    def config_signature(self) -> int | None:
        return self._config_file_mtime()

    def client_for(self, bot_key: str) -> FeishuClient | None:
        config = self.current_config()
        bot = config.bot(bot_key)
        if bot is None or not bot.enabled:
            return None
        with self._config_lock:
            client = self._clients.get(bot.key)
            if client is None:
                client = FeishuClient(bot.feishu_base_url, bot.app_id, bot.app_secret)
                self._clients[bot.key] = client
            return client

    def start(self) -> None:
        for worker in self.workers:
            worker.start()
        self.long_connection.start()
        config = self.current_config()
        server = ThreadingHTTPServer((config.listen_host, config.listen_port), RequestHandler)
        server.gateway = self  # type: ignore[attr-defined]
        server.daemon_threads = True
        self._server = server
        LOGGER.info("gateway listening on http://%s:%s", config.listen_host, config.listen_port)
        try:
            server.serve_forever(poll_interval=0.5)
        finally:
            server.server_close()
            self.stop_event.set()
            self.long_connection.stop()
            for worker in self.workers:
                worker.join(timeout=5)

    def stop(self) -> None:
        self.stop_event.set()
        self.long_connection.stop()
        server = getattr(self, "_server", None)
        if server:
            # BaseServer.shutdown() must run from a thread other than serve_forever().
            threading.Thread(target=server.shutdown, name="gateway-shutdown", daemon=True).start()

    def handle_payload(self, bot: BotConfig, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        config = self.current_config()
        if payload.get("encrypt"):
            return HTTPStatus.BAD_REQUEST, {
                "error": "当前 HTTP 适配器未启用 Encrypt Key 解密；请先关闭加密回调，或使用飞书长连接模式。"
            }
        if payload.get("type") == "url_verification" or payload.get("challenge"):
            token = payload.get("token")
            if bot.verification_token and token != bot.verification_token:
                return HTTPStatus.FORBIDDEN, {"error": "verification token mismatch"}
            return HTTPStatus.OK, {"challenge": payload.get("challenge", "")}

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        incoming_token = header.get("token") or payload.get("token")
        if bot.verification_token and incoming_token and incoming_token != bot.verification_token:
            return HTTPStatus.FORBIDDEN, {"error": "verification token mismatch"}

        event = parse_event(payload, bot.bot_open_id)
        if event is None or event.event_type not in {"im.message.receive_v1", "message"}:
            return HTTPStatus.OK, {"ok": True, "ignored": "not a supported message event"}
        return self.enqueue_event(bot, event)

    def enqueue_event(self, bot: BotConfig, event: FeishuEvent) -> tuple[int, dict[str, Any]]:
        config = self.current_config()
        if event.event_type not in {"im.message.receive_v1", "message"}:
            return HTTPStatus.OK, {"ok": True, "ignored": "not a supported message event"}
        if bot.require_mention and not event.mentioned_bot:
            return HTTPStatus.OK, {"ok": True, "ignored": "bot not mentioned"}

        if not self.store.record_event(event.event_id):
            return HTTPStatus.OK, {"ok": True, "duplicate": True}

        default_repo = config.default_repo_key()
        if is_help_request(event.text):
            delivery = self._send_help(bot, event.chat_id)
            return HTTPStatus.OK, {"ok": True, "help": True, "delivery": delivery}

        pr_url = extract_pr_url(event.text, default_repo=default_repo)
        if not pr_url:
            pr_number = extract_pr_number(event.text)
            if pr_number and not default_repo:
                message = (
                    f"收到 PR 号 #{pr_number}，但当前配置了多个仓库，无法判断目标仓库。"
                    "请发送完整 GitHub PR 链接，或先设置默认仓库。"
                )
            else:
                default_hint = f"当前默认仓库是 {default_repo}，" if default_repo else ""
                message = (
                    "收到。"
                    f"{default_hint}请发送 `PR 314`、`#314` 或 `pr314`；"
                    "也可以直接附上 GitHub PR 链接，例如：https://github.com/org/repo/pull/123"
                )
            delivery = self._send_help(bot, event.chat_id, notice=message)
            return HTTPStatus.OK, {
                "ok": True,
                "help": True,
                "ignored": "missing github pr url",
                "delivery": delivery,
            }

        repo_key = config.repo_key(pr_url)
        job, created = self.store.create_or_get_active_job(
            event_id=event.event_id,
            bot_key=bot.key,
            chat_id=event.chat_id,
            sender_id=event.sender_id,
            message_id=event.message_id,
            request_text=event.text,
            pr_url=pr_url,
            repo_key=repo_key,
        )
        job_id = job["job_id"]
        repo_root = config.repo_root_for(pr_url)
        pr_label = _pr_task_title(pr_url, repo_root)
        if not created:
            status_label = "正在执行" if job["status"] == "running" else "等待执行"
            ack = (
                f"PR 检视已在进行｜{pr_label or repo_key}｜任务 {job_id[:8]}｜{status_label}。"
                "本次请求已合并，不会重复创建 Codex 会话；完成后结果会正常回传。"
            )
            self._send_ack(
                bot,
                event.chat_id,
                ack,
                pr_url=pr_url,
                job_id=job_id,
                status=str(job["status"]),
                pr_label=pr_label,
                deduplicated=True,
            )
            return HTTPStatus.OK, {
                "ok": True,
                "job_id": job_id,
                "status": job["status"],
                "deduplicated": True,
            }
        if repo_root is None:
            ack = (
                f"PR 检视未启动｜{pr_label or repo_key}｜任务 {job_id[:8]}｜"
                f"本机尚未配置 {repo_key} 的仓库映射。"
            )
        else:
            ack = (
                f"PR 检视已受理｜{pr_label or repo_key}｜任务 {job_id[:8]}｜等待执行。"
                "完成后自动回传结果；共识后的可行动意见会发布到 GitHub PR。"
            )
        self.store.set_ack(job_id, ack)
        self._send_ack(
            bot,
            event.chat_id,
            ack,
            pr_url=pr_url,
            job_id=job_id,
            status=str(job["status"]),
            pr_label=pr_label,
            repo_mapping_missing=repo_root is None,
            repo_key=repo_key,
        )
        return HTTPStatus.OK, {
            "ok": True,
            "job_id": job_id,
            "status": job["status"],
            "deduplicated": False,
        }

    def _send_chat(self, bot: BotConfig, chat_id: str, text: str) -> str:
        client = self.client_for(bot.key)
        config = self.current_config()
        if client is None:
            LOGGER.error("机器人 %s 未配置或已禁用，无法发送飞书消息", bot.key)
            return "failed: bot configuration is missing or disabled"
        try:
            client.send_text(chat_id, text, config.max_feishu_text_length)
            return "sent"
        except FeishuError as exc:
            LOGGER.error("failed to send Feishu message to %s: %s", chat_id, exc)
            return f"failed: {exc}"

    def _send_help(self, bot: BotConfig, chat_id: str, notice: str | None = None) -> str:
        client = self.client_for(bot.key)
        config = self.current_config()
        if client is None:
            LOGGER.error("机器人 %s 未配置或已禁用，无法发送飞书帮助", bot.key)
            return "failed: bot configuration is missing or disabled"

        default_repo = config.default_repo_key()
        configured_repos = sorted(config.repo_roots)
        fallback_text = build_help_text(
            default_repo=default_repo,
            configured_repos=configured_repos,
            notice=notice,
        )
        if config.feishu_result_format == "card":
            try:
                client.send_help_card(
                    chat_id,
                    default_repo=default_repo,
                    configured_repos=configured_repos,
                    notice=notice,
                )
                return "sent_card"
            except FeishuError as card_exc:
                LOGGER.warning("failed to send Feishu help card, falling back to text: %s", card_exc)
        try:
            client.send_text(chat_id, fallback_text, config.max_feishu_text_length)
            return "sent"
        except FeishuError as exc:
            LOGGER.error("failed to send Feishu help to %s: %s", chat_id, exc)
            return f"failed: {exc}"

    def _send_ack(
        self,
        bot: BotConfig,
        chat_id: str,
        fallback_text: str,
        *,
        pr_url: str,
        job_id: str,
        status: str,
        pr_label: str | None,
        deduplicated: bool = False,
        repo_mapping_missing: bool = False,
        repo_key: str | None = None,
    ) -> str:
        client = self.client_for(bot.key)
        config = self.current_config()
        if client is None:
            LOGGER.error("机器人 %s 未配置或已禁用，无法发送飞书消息", bot.key)
            return "failed: bot configuration is missing or disabled"
        if config.feishu_result_format == "card":
            try:
                client.send_ack_card(
                    chat_id,
                    pr_url=pr_url,
                    job_id=job_id,
                    status=status,
                    pr_label=pr_label,
                    deduplicated=deduplicated,
                    repo_mapping_missing=repo_mapping_missing,
                    repo_key=repo_key,
                )
                return "sent_card"
            except FeishuError as card_exc:
                LOGGER.warning("failed to send Feishu acknowledgement card, falling back to text: %s", card_exc)
        try:
            client.send_text(chat_id, fallback_text, config.max_feishu_text_length)
            return "sent"
        except FeishuError as exc:
            LOGGER.error("failed to send Feishu acknowledgement to %s: %s", chat_id, exc)
            return f"failed: {exc}"

    def health(self) -> dict[str, Any]:
        config = self.current_config()
        return {
            "ok": True,
            "config": config.public_summary(),
            "app_server_resources": app_server_resource_status(config),
            "long_connection": self.long_connection.public_status(),
            "pending_jobs": self.store.pending_count(),
            "running_jobs": self.store.running_count(),
            "recent_jobs": [StateStore.public_job(job) for job in self.store.list_jobs(limit=5)],
        }


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "CodexFeishuReview/0.1"

    @property
    def gateway(self) -> Gateway:
        return self.server.gateway  # type: ignore[attr-defined]

    def _write_json(self, status: int, value: dict[str, Any]) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._write_json(HTTPStatus.OK, self.gateway.health())
            return
        if self.path == "/":
            self._write_json(HTTPStatus.OK, {"service": "codex-feishu-pr-review", "ok": True})
            return
        self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        bot = self.gateway.current_config().bot_for_path(self.path)
        if bot is None:
            self._write_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 2_000_000:
                raise ValueError("invalid content length")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            status, result = self.gateway.handle_payload(bot, payload)
            self._write_json(status, result)
        except (ValueError, json.JSONDecodeError) as exc:
            self._write_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - return a bounded error to the webhook caller
            LOGGER.exception("event handler failed")
            self._write_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        LOGGER.info("http %s", format % args)


def run() -> None:
    config = Config.load()
    _configure_logging(config.log_dir)
    gateway = Gateway(config)

    def shutdown(_signum: int, _frame: Any) -> None:
        gateway.stop()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    gateway.start()


if __name__ == "__main__":
    run()
