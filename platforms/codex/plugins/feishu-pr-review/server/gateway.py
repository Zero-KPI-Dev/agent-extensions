from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
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
    is_identity_request,
    is_merge_ready_conclusion,
    notification_mention_text,
    parse_event,
    review_conclusion,
)
from .long_connection import LongConnectionManager
from .resource_health import app_server_resource_status


LOGGER = logging.getLogger("feishu-pr-review")


@dataclass(frozen=True)
class GitHubPrMetadata:
    owner: str
    repository: str
    number: int
    url: str
    title: str
    state: str
    is_draft: bool
    author: str | None
    base_ref: str
    base_sha: str
    head_ref: str
    head_sha: str


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


def _normalized_github_login(value: Any) -> str | None:
    login = str(value or "").strip().lstrip("@")
    if not login or len(login) > 100:
        return None
    if not all(character.isalnum() or character in "-_.[]" for character in login):
        return None
    return login


def _github_pr_api_url(pr_url: str) -> str | None:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    if len(parts) < 4 or parts[2].lower() != "pull" or not parts[3].isdigit():
        return None
    owner = urllib.parse.quote(parts[0], safe="")
    repository = urllib.parse.quote(parts[1], safe="")
    return f"https://api.github.com/repos/{owner}/{repository}/pulls/{int(parts[3])}"


def _github_pr_coordinates(pr_url: str) -> tuple[str, str, int] | None:
    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"github.com", "www.github.com"}:
        return None
    if len(parts) < 4 or parts[2].lower() != "pull" or not parts[3].isdigit():
        return None
    return parts[0], parts[1], int(parts[3])


def _parse_github_pr_metadata(payload: Any, pr_url: str) -> GitHubPrMetadata | None:
    coordinates = _github_pr_coordinates(pr_url)
    if not coordinates or not isinstance(payload, dict):
        return None
    owner, repository, expected_number = coordinates

    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    author = payload.get("author") if isinstance(payload.get("author"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    number = payload.get("number")
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = expected_number
    if number != expected_number:
        return None

    base_ref = str(payload.get("baseRefName") or base.get("ref") or "").strip()
    base_sha = str(payload.get("baseRefOid") or base.get("sha") or "").strip()
    head_ref = str(payload.get("headRefName") or head.get("ref") or "").strip()
    head_sha = str(payload.get("headRefOid") or head.get("sha") or "").strip()
    if not base_ref or not base_sha or not head_ref or not head_sha:
        return None
    return GitHubPrMetadata(
        owner=owner,
        repository=repository,
        number=number,
        url=str(payload.get("url") or payload.get("html_url") or pr_url).strip(),
        title=str(payload.get("title") or "").strip(),
        state=str(payload.get("state") or "").strip().upper(),
        is_draft=bool(payload.get("isDraft") if "isDraft" in payload else payload.get("draft")),
        author=_normalized_github_login(author.get("login") or user.get("login")),
        base_ref=base_ref,
        base_sha=base_sha,
        head_ref=head_ref,
        head_sha=head_sha,
    )


def resolve_github_pr_metadata(pr_url: str, repo_root: Path | None = None) -> GitHubPrMetadata | None:
    """Resolve and pin live GitHub PR identity before Codex starts.

    A review must fail closed when the target cannot be confirmed.  It must
    never infer a PR head from nearby local branches or commit timestamps.
    """

    executable = resolve_executable("gh")
    if executable:
        try:
            result = subprocess.run(
                [
                    executable,
                    "pr",
                    "view",
                    pr_url,
                    "--json",
                    (
                        "number,title,url,state,isDraft,author,"
                        "baseRefName,baseRefOid,headRefName,headRefOid"
                    ),
                ],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            if result.returncode == 0:
                metadata = _parse_github_pr_metadata(json.loads(result.stdout), pr_url)
                if metadata:
                    return metadata
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    api_url = _github_pr_api_url(pr_url)
    if not api_url:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-feishu-pr-review",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return _parse_github_pr_metadata(payload, pr_url)


def resolve_github_pr_author(pr_url: str, repo_root: Path | None = None) -> str | None:
    """Resolve the PR author's GitHub login without making delivery mandatory."""

    executable = resolve_executable("gh")
    if executable:
        try:
            result = subprocess.run(
                [executable, "pr", "view", pr_url, "--json", "author"],
                cwd=repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout)
                author = payload.get("author") if isinstance(payload, dict) else None
                if isinstance(author, dict):
                    login = _normalized_github_login(author.get("login"))
                    if login:
                        return login
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            pass

    api_url = _github_pr_api_url(pr_url)
    if not api_url:
        return None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "codex-feishu-pr-review",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(api_url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    user = payload.get("user") if isinstance(payload, dict) else None
    return _normalized_github_login(user.get("login")) if isinstance(user, dict) else None


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
        github_author: str | None = None,
    ) -> str:
        if not chat_id:
            return "skipped"
        job_bot_key = bot_key if bot_key is not None else getattr(self, "_current_bot_key", "")
        client = self.client_provider(job_bot_key)
        config = self.config_provider()
        bot = config.bot(job_bot_key)
        mention_open_ids: tuple[str, ...] = ()
        mention_kind = "author"
        if bot:
            conclusion = review_conclusion(text)
            if is_merge_ready_conclusion(conclusion):
                mention_kind = "merge_maintainers"
                repo_key = config.repo_key(pr_url) if pr_url else None
                mention_open_ids = bot.merge_maintainer_open_ids(repo_key)
            else:
                author_open_id = bot.author_open_id(github_author)
                mention_open_ids = (author_open_id,) if author_open_id else ()
        fallback_text = notification_mention_text(
            text,
            mention_open_ids,
            github_author,
            mention_kind=mention_kind,
        )
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
                        mention_open_ids=mention_open_ids,
                        mention_kind=mention_kind,
                        github_author=github_author,
                    )
                    return "sent_card"
                except FeishuError as card_exc:
                    # Keep delivery reliable when an older tenant or an
                    # account policy rejects interactive cards.
                    LOGGER.warning("failed to send Feishu card, falling back to text: %s", card_exc)
                    client.send_text(chat_id, fallback_text, config.max_feishu_text_length)
                    return "sent_text_fallback"
            client.send_text(chat_id, fallback_text, config.max_feishu_text_length)
            return "sent"
        except FeishuError as exc:
            LOGGER.error("failed to send Feishu message: %s", exc)
            return f"failed: {exc}"

    def _send_job(self, job: dict[str, Any], text: str, *, github_author: str | None = None) -> str:
        targets = self.store.delivery_targets(job["job_id"])
        if not targets:
            return self._send(
                job.get("chat_id"),
                text,
                pr_url=job.get("pr_url"),
                bot_key=job.get("bot_key"),
                github_author=github_author,
            )
        deliveries = [
            self._send(
                target["chat_id"],
                text,
                pr_url=job.get("pr_url"),
                bot_key=target["bot_key"],
                github_author=github_author,
            )
            for target in targets
        ]
        if len(deliveries) == 1:
            return deliveries[0]
        failures = [status for status in deliveries if status.startswith("failed")]
        if failures:
            return f"partial_failure: {'; '.join(failures)}"
        return "sent_multiple"

    def _github_author_for_delivery(self, job: dict[str, Any], repo_root: Path) -> str | None:
        config = self.config_provider()
        targets = self.store.delivery_targets(job["job_id"])
        bot_keys = {str(target.get("bot_key") or "") for target in targets}
        if not bot_keys:
            bot_keys = {str(job.get("bot_key") or "")}
        if not any((config.bot(bot_key).author_mappings if config.bot(bot_key) else {}) for bot_key in bot_keys):
            return None
        author = resolve_github_pr_author(str(job.get("pr_url") or ""), repo_root)
        if author:
            LOGGER.info("resolved GitHub author %s for job %s", author, job["job_id"])
        else:
            LOGGER.warning("unable to resolve GitHub author for job %s; sending without mention", job["job_id"])
        return author

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

    def _prompt(self, job: dict[str, Any], pr: GitHubPrMetadata) -> str:
        config = self.config_provider()
        return f"""你正在执行一个由飞书机器人触发的 GitHub PR 检视任务。

任务边界：仅对本地网关已配置仓库中的指定 PR 执行经发起人授权的只读、防御性代码审查。关注代码和文档的正确性、可靠性、兼容性、分布式部署影响与安全加固；即使变更涉及鉴权、密钥、恢复、网络或运维，也只分析当前 diff 的代码级风险并给出防御性修复建议，不生成复现攻击的内容。允许读取下方唯一目标 PR 的 GitHub 元数据、diff、历史 review 与 checks，并按发布规则只向该 PR 创建 COMMENT review；不得访问或操作与该 PR 无关的外部系统。

必须使用本机 Skill：{config.review_skill_path}
Skill 名称：review-pr-with-panel

目标 PR：{job['pr_url']}
用户原始请求：{job['request_text']}

网关已从 GitHub 实时确认并冻结本轮目标：
- repository：{pr.owner}/{pr.repository}
- PR number：{pr.number}
- title：{pr.title}
- state：{pr.state}{' / DRAFT' if pr.is_draft else ''}
- author：{pr.author or 'UNKNOWN'}
- base：{pr.base_ref}@{pr.base_sha}
- head：{pr.head_ref}@{pr.head_sha}

以上 base/head 是本轮唯一允许检视的代码范围。必须再次读取 GitHub 当前元数据确认 head 未变化，然后严格检视 `{pr.base_sha}...{pr.head_sha}`。不得根据本地邻近分支、提交时间、分支名称相似度或其他 PR 会话猜测 base/head；若精确对象缺失或 GitHub 无法确认，立即报告目标解析失败，不得改审其他分支。

请严格执行该 Skill 的完整流程：根据当前 PR 状态选择正确的 review mode，使用 Leader 加两个独立的 A/B 验证代理，保持只读，不实现修复。

这不是 report-only 请求。对于有效的 GitHub PR URL，请遵循 Skill 的 GitHub 发布规则：共识或 A 终审确认的可行动检视意见应发布到 GitHub PR；`FINAL_BY_A` 意见必须披露 B 异议。能定位到当前 diff 行时发布行内意见，否则发布到 review body。不要自动 approve、request changes 或关闭线程。若已经存在历史检视结果，请按 Skill 的 finding lineage 与 follow-up 规则避免重复意见。

任务结束时，请返回适合飞书回传的中文摘要。以下字段必须逐项明确给出：PR、review_id、mode（精确使用 INITIAL_REVIEW、FIX_VERIFICATION、INCREMENTAL_REREVIEW 或 NO_NEW_REVISION）、结论、当前待处理发现数量（按 Critical/High/Medium/Low/Suggestion 分级）、主要发现摘要、GitHub 发布状态、未发布或阻塞原因（如有）。发现数量只能统计当前仍需行动的开放 finding；`FIX_VERIFIED` 和 `NO_ACTIONABLE_FINDINGS` 的当前待处理数量必须全部为 0。历史 finding 即使保留原严重级别，也必须另列为“历史已验证修复”，不得计入当前待处理发现数量。即使某项为空或数量为 0 也不要省略；不要只返回“已完成”。"""

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

        pr_metadata = resolve_github_pr_metadata(job["pr_url"], repo_root)
        if pr_metadata is None:
            self._finish_failure(
                job,
                "无法从 GitHub 实时确认 PR 的 base/head，任务已在启动 Codex 前停止；不会从本地分支猜测检视目标。",
            )
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
            self._execute_with_exec(job, pr_metadata, repo_root, executable, env, log_path)
            return
        self._execute_with_app_server(job, pr_metadata, repo_root, executable, env, log_path)

    def _execute_with_exec(
        self,
        job: dict[str, Any],
        pr_metadata: GitHubPrMetadata,
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
            self._prompt(job, pr_metadata),
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

        github_author = self._github_author_for_delivery(job, repo_root)
        delivery = self._send_job(job, report, github_author=github_author)
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
        pr_metadata: GitHubPrMetadata,
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
        resume_thread_id = self.store.reusable_codex_thread_id(job_id, job["pr_url"])

        def on_pid(pid: int | None) -> None:
            self.store.set_pid(job_id, pid)

        def on_thread_ready(thread_id: str, resumed: bool) -> None:
            self.store.set_codex_thread_id(job_id, thread_id)
            LOGGER.info(
                "job %s %s Codex App thread %s",
                job_id,
                "resumed" if resumed else "created",
                thread_id,
            )

        result = None
        try:
            result = client.run(
                cwd=str(repo_root),
                prompt=self._prompt(job, pr_metadata),
                sandbox=config.codex_sandbox,
                network_access=config.codex_network_access,
                timeout_seconds=config.job_timeout_seconds,
                env=env,
                approval_policy=config.codex_approval_policy,
                approvals_reviewer=config.codex_approvals_reviewer,
                thread_name=task_title,
                resume_thread_id=resume_thread_id,
                should_cancel=lambda: self.store.is_cancel_requested(job_id),
                on_pid=on_pid,
                on_thread_ready=on_thread_ready,
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

        github_author = self._github_author_for_delivery(job, repo_root)
        delivery = self._send_job(job, result.report, github_author=github_author)
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
            "job %s completed with app-server thread=%s resumed=%s delivery=%s",
            job_id,
            result.thread_id,
            result.resumed,
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

    def config_signature(self) -> tuple[tuple[Any, ...], ...]:
        """Return only fields that require rebuilding Feishu connections.

        Author and merge-maintainer mappings are delivery-time settings and
        hot-reload safely. Using the config file mtime here rebuilt the SDK
        WebSocket for every mapping edit, which can leave its process-global
        event loop in a stale state.
        """

        config = self.current_config()
        return tuple(
            sorted(
                (
                    bot.key,
                    bot.enabled,
                    bot.transport,
                    bot.app_id,
                    bot.app_secret,
                    bot.require_mention,
                )
                for bot in config.bots.values()
            )
        )

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
        if is_identity_request(event.text):
            if not event.sender_id:
                delivery = self._send_chat(bot, event.chat_id, "当前消息事件没有提供飞书 Open ID。")
            else:
                delivery = self._send_chat(
                    bot,
                    event.chat_id,
                    f"你的飞书 Open ID：`{event.sender_id}`\n这个 ID 仅适用于机器人 `{bot.key}` 对应的飞书应用。",
                )
            return HTTPStatus.OK, {"ok": True, "identity": True, "delivery": delivery}
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
                "完成后自动回传结果；共识或 A 终审确认的可行动意见会发布到 GitHub PR。"
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
