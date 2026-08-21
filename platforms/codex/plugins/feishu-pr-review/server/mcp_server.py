from __future__ import annotations

import json
import os
import signal
import sys
import uuid
from pathlib import Path
from typing import Any

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

from server.config import Config  # noqa: E402
from server.db import StateStore  # noqa: E402
from server.feishu import extract_pr_url  # noqa: E402
from server.resource_health import app_server_resource_status  # noqa: E402


SERVER_INFO = {
    "name": "feishu-pr-review",
    "version": "0.1.0",
}


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except PermissionError:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return


def _text_result(value: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
        "isError": is_error,
    }


def _tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": "feishu_review_health",
            "description": "查看本机飞书 PR 检视网关、配置和队列健康状态。",
            "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "feishu_review_status",
            "description": "查看一个检视任务或最近的检视任务。",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "任务 ID；省略时返回最近任务。"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "feishu_review_submit",
            "description": "从 Codex 侧提交一个本地异步 PR 检视任务；实际检视仍由 review-pr-with-panel Skill 执行。",
            "inputSchema": {
                "type": "object",
                "required": ["pr_url"],
                "properties": {
                    "pr_url": {"type": "string", "description": "GitHub Pull Request URL。"},
                    "request": {"type": "string", "description": "检视重点或补充要求。"},
                    "bot_key": {"type": "string", "description": "可选的机器人配置 key；不传时使用默认机器人。"},
                },
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True},
        },
        {
            "name": "feishu_review_cancel",
            "description": "取消一个待执行或正在执行的 PR 检视任务。",
            "inputSchema": {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
        {
            "name": "feishu_review_retry",
            "description": "重新排队一个已经结束的 PR 检视任务。",
            "inputSchema": {
                "type": "object",
                "required": ["job_id"],
                "properties": {"job_id": {"type": "string"}},
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False},
        },
    ]


class McpApplication:
    def __init__(self) -> None:
        self.config = Config.load()
        self.config.ensure_directories()
        self.store = StateStore(self.config.db_path)
        self.store.initialize()

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "feishu_review_health":
            return _text_result(
                {
                    "ok": True,
                    "config": self.config.public_summary(),
                    "app_server_resources": app_server_resource_status(self.config),
                    "pending_jobs": self.store.pending_count(),
                    "running_jobs": self.store.running_count(),
                    "recent_jobs": [StateStore.public_job(job) for job in self.store.list_jobs(limit=5)],
                }
            )

        if name == "feishu_review_status":
            job_id = str(args.get("job_id") or "").strip()
            if job_id:
                job = self.store.get_job(job_id)
                if not job:
                    return _text_result({"error": f"找不到任务：{job_id}"}, is_error=True)
                return _text_result(StateStore.public_job(job))
            limit = int(args.get("limit", 20))
            return _text_result([StateStore.public_job(job) for job in self.store.list_jobs(limit=limit)])

        if name == "feishu_review_submit":
            pr_url = extract_pr_url(str(args.get("pr_url") or ""))
            if not pr_url:
                return _text_result({"error": "pr_url 必须是 GitHub Pull Request URL"}, is_error=True)
            repo_key = self.config.repo_key(pr_url)
            if self.config.repo_root_for(pr_url) is None:
                return _text_result({"error": f"未配置仓库映射：{repo_key}"}, is_error=True)
            bot_key = str(args.get("bot_key") or self.config.default_bot)
            if self.config.bot(bot_key) is None:
                return _text_result({"error": f"未配置机器人：{bot_key}"}, is_error=True)
            job, created = self.store.create_or_get_active_job(
                event_id=f"mcp:{uuid.uuid4().hex}",
                bot_key=bot_key,
                chat_id=None,
                sender_id="mcp",
                message_id=None,
                request_text=str(args.get("request") or "从 Codex MCP 提交的 PR 检视请求"),
                pr_url=pr_url,
                repo_key=repo_key,
            )
            return _text_result(
                {
                    "submitted": created,
                    "deduplicated": not created,
                    "job": StateStore.public_job(job),
                }
            )

        if name == "feishu_review_cancel":
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return _text_result({"error": "job_id 不能为空"}, is_error=True)
            job = self.store.request_cancel(job_id)
            if not job:
                return _text_result({"error": f"找不到可取消的任务：{job_id}"}, is_error=True)
            pid = job.get("pid")
            if job.get("status") == "running" and isinstance(pid, int) and pid > 0:
                _kill_process_group(pid)
            return _text_result({"cancel_requested": True, "job": StateStore.public_job(job)})

        if name == "feishu_review_retry":
            job_id = str(args.get("job_id") or "").strip()
            if not job_id:
                return _text_result({"error": "job_id 不能为空"}, is_error=True)
            job = self.store.retry(job_id)
            if not job:
                return _text_result({"error": f"任务不存在，或当前状态不可重试：{job_id}"}, is_error=True)
            return _text_result({"queued": True, "job": StateStore.public_job(job)})

        return _text_result({"error": f"未知工具：{name}"}, is_error=True)


def _response(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def main() -> None:
    app = McpApplication()
    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("JSON-RPC request must be an object")
            method = request.get("method")
            request_id = request.get("id")
            params = request.get("params") if isinstance(request.get("params"), dict) else {}

            if method == "initialize":
                result = {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                }
                print(json.dumps(_response(request_id, result), ensure_ascii=False), flush=True)
                continue
            if method in {"notifications/initialized", "initialized", "ping"}:
                if request_id is not None:
                    print(json.dumps(_response(request_id, {}), ensure_ascii=False), flush=True)
                continue
            if method == "tools/list":
                print(json.dumps(_response(request_id, {"tools": _tool_definitions()}), ensure_ascii=False), flush=True)
                continue
            if method == "tools/call":
                name = str(params.get("name") or "")
                args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                print(json.dumps(_response(request_id, app.call(name, args)), ensure_ascii=False), flush=True)
                continue
            print(json.dumps(_error(request_id, -32601, f"Method not found: {method}"), ensure_ascii=False), flush=True)
        except Exception as exc:  # noqa: BLE001 - MCP must stay alive for the next request
            request_id = locals().get("request_id")
            print(json.dumps(_error(request_id, -32603, str(exc)), ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
