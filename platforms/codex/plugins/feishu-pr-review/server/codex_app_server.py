from __future__ import annotations

import json
import logging
import os
import queue
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


LOGGER = logging.getLogger("feishu-pr-review.codex-app-server")

# thread/start creates the in-memory thread before the first turn has always
# been flushed to its rollout file.  thread/name/set can therefore race that
# first persistence even after turn/start returns.  Keep the retry window
# short so presentation work never delays the review itself for long.
_THREAD_NAME_RETRY_DELAYS = (0.05, 0.10, 0.25, 0.50, 1.00)


class CodexAppServerError(RuntimeError):
    """An app-server protocol or execution failure."""


class CodexAppServerTimeout(CodexAppServerError):
    """The app-server turn exceeded its configured timeout."""


class CodexAppServerCancelled(CodexAppServerError):
    """The job was cancelled while the app-server turn was running."""


@dataclass(frozen=True)
class CodexAppServerResult:
    thread_id: str
    turn_id: str | None
    report: str
    protocol_log: str


class CodexAppServerClient:
    """Small JSON-RPC client for one persisted Codex app-server thread.

    The normal transport is the user-level Unix socket shared by Codex
    Desktop and the local gateway.  Stdio remains available as an explicit
    compatibility fallback for older installations.
    """

    def __init__(
        self,
        executable: str,
        *,
        transport: str = "shared_unix",
        socket_path: str | Path | None = None,
    ):
        if transport not in {"shared_unix", "stdio"}:
            raise ValueError(f"不支持的 Codex app-server transport：{transport}")
        self.executable = executable
        self.transport = transport
        self.socket_path = Path(socket_path).expanduser() if socket_path else None
        self._request_id = 0
        self._process: subprocess.Popen[str] | None = None
        self._websocket: Any | None = None
        self._events: queue.Queue[tuple[str, str | None]] = queue.Queue()
        self._log_lock = threading.Lock()
        self._log_lines: list[str] = []
        self._stderr_lines: list[str] = []
        self._stdout_reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._agent_message_ids: list[str] = []
        self._agent_message_fragments: dict[str, list[str]] = {}
        self._completed_agent_messages: dict[str, str] = {}
        self._agent_message_phases: dict[str, str] = {}
        self._final_answer_message_ids: list[str] = []
        self._turn_completed = False
        self._turn_status: str | None = None
        self._turn_error: str | None = None
        self._thread_id: str | None = None
        self._turn_id: str | None = None

    def run(
        self,
        *,
        cwd: str,
        prompt: str,
        sandbox: str,
        timeout_seconds: int,
        env: dict[str, str],
        approval_policy: str = "on-request",
        approvals_reviewer: str = "auto_review",
        thread_name: str | None = None,
        should_cancel: Callable[[], bool] | None = None,
        on_pid: Callable[[int | None], None] | None = None,
        on_thread_created: Callable[[str], None] | None = None,
    ) -> CodexAppServerResult:
        deadline = time.monotonic() + max(1, timeout_seconds)
        if self.transport == "shared_unix":
            self._connect_socket()
            if on_pid:
                on_pid(None)
        else:
            process = self._start_process(cwd=cwd, env=env)
            self._process = process
            if on_pid:
                on_pid(process.pid)

        try:
            initialize_result = self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "feishu-pr-review",
                        "title": "Feishu PR Review",
                        "version": "0.1.0",
                    },
                },
                deadline=deadline,
                should_cancel=should_cancel,
            )
            if not isinstance(initialize_result, dict):
                raise CodexAppServerError("initialize 返回了无效结果")
            self._notify("initialized", {})

            thread_result = self._request(
                "thread/start",
                {
                    "cwd": cwd,
                    "approvalPolicy": approval_policy,
                    "approvalsReviewer": approvals_reviewer,
                    "sandbox": sandbox,
                    "serviceName": "feishu-pr-review",
                    "threadSource": "feishu-pr-review",
                },
                deadline=deadline,
                should_cancel=should_cancel,
            )
            thread = thread_result.get("thread") if isinstance(thread_result, dict) else None
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise CodexAppServerError("thread/start 没有返回 thread.id")
            self._thread_id = thread_id
            if on_thread_created:
                on_thread_created(thread_id)

            turn_result = self._request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": prompt}],
                },
                deadline=deadline,
                should_cancel=should_cancel,
            )
            turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
            turn_id = turn.get("id") if isinstance(turn, dict) else None
            self._turn_id = turn_id if isinstance(turn_id, str) else None

            if thread_name:
                self._set_thread_name(
                    thread_id,
                    thread_name,
                    deadline=deadline,
                    should_cancel=should_cancel,
                )

            if not self._turn_completed:
                self._read_until_turn_completed(deadline=deadline, should_cancel=should_cancel)

            if self._turn_status != "completed":
                detail = self._turn_error or self._turn_status or "未知状态"
                raise CodexAppServerError(f"Codex turn 未成功完成：{detail}")

            report = self._final_agent_message()
            return CodexAppServerResult(
                thread_id=thread_id,
                turn_id=turn_id if isinstance(turn_id, str) else None,
                report=report,
                protocol_log=self._protocol_log(),
            )
        except (CodexAppServerCancelled, CodexAppServerTimeout):
            self._interrupt_current_turn()
            raise
        finally:
            self._unsubscribe_current_thread()
            self._stop_process()
            if on_pid:
                on_pid(None)

    def _start_process(self, *, cwd: str, env: dict[str, str]) -> subprocess.Popen[str]:
        args = [self.executable, "app-server", "--listen", "stdio://"]
        LOGGER.info("starting Codex app-server in %s", cwd)
        try:
            process = subprocess.Popen(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            raise CodexAppServerError(f"无法启动 Codex app-server：{exc}") from exc

        if process.stdin is None or process.stdout is None or process.stderr is None:
            process.kill()
            raise CodexAppServerError("Codex app-server 未提供完整的 stdio 通道")

        self._stdout_reader = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            name="codex-app-server-stdout",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process.stderr,),
            name="codex-app-server-stderr",
            daemon=True,
        )
        self._stdout_reader.start()
        self._stderr_reader.start()
        return process

    def _set_thread_name(
        self,
        thread_id: str,
        thread_name: str,
        *,
        deadline: float,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        """Best-effort naming after the first turn starts.

        App Server may return from turn/start just before the rollout writer
        flushes the initial metadata.  Retry only that known persistence race;
        unsupported methods and other permanent errors still fail fast.  A
        separate short deadline keeps naming from consuming the review budget.
        """

        naming_deadline = min(deadline, time.monotonic() + 3.0)
        attempts = 0
        last_error: CodexAppServerError | None = None

        for delay in (0.0, *_THREAD_NAME_RETRY_DELAYS):
            if delay:
                if should_cancel and should_cancel():
                    raise CodexAppServerCancelled("任务已取消")
                remaining = naming_deadline - time.monotonic()
                if remaining <= delay:
                    break
                time.sleep(delay)

            attempts += 1
            try:
                self._request(
                    "thread/name/set",
                    {"threadId": thread_id, "name": thread_name},
                    deadline=naming_deadline,
                    should_cancel=should_cancel,
                )
                LOGGER.info(
                    "named Codex App thread %s as %s after %s attempt(s)",
                    thread_id,
                    thread_name,
                    attempts,
                )
                return
            except CodexAppServerCancelled:
                raise
            except CodexAppServerTimeout as exc:
                last_error = exc
                break
            except CodexAppServerError as exc:
                last_error = exc
                if not self._thread_name_error_is_retryable(exc):
                    break

        # Naming is a presentation improvement and must never turn a valid PR
        # review into a failed job on an older or temporarily busy server.
        LOGGER.warning(
            "failed to name Codex App thread %s after %s attempt(s): %s",
            thread_id,
            attempts,
            last_error or "retry window elapsed",
        )

    @staticmethod
    def _thread_name_error_is_retryable(exc: CodexAppServerError) -> bool:
        message = str(exc).lower()
        rollout_not_ready = "rollout" in message and any(
            marker in message
            for marker in ("is empty", "no rollout", "not found", "does not exist", "not yet")
        )
        metadata_not_ready = "session metadata" in message and any(
            marker in message for marker in ("failed to read", "missing", "not found")
        )
        return rollout_not_ready or metadata_not_ready

    def _connect_socket(self) -> None:
        socket_path = self.socket_path
        if socket_path is None:
            raise CodexAppServerError("没有配置 Codex app-server Unix socket 路径")
        try:
            from websockets.sync.client import unix_connect
        except ImportError as exc:
            raise CodexAppServerError(
                "缺少 websockets 依赖，请安装 requirements-long-connection.txt"
            ) from exc

        last_error: Exception | None = None
        retry_deadline = time.monotonic() + 10
        while time.monotonic() < retry_deadline:
            if not socket_path.exists():
                time.sleep(0.2)
                continue
            try:
                # Codex's Unix control socket rejects permessage-deflate
                # negotiation.  The official Rust client uses the default
                # uncompressed WebSocket configuration as well.
                self._websocket = unix_connect(
                    str(socket_path),
                    uri="ws://localhost/rpc",
                    compression=None,
                    open_timeout=1.5,
                    max_size=128 * 1024 * 1024,
                )
                break
            except Exception as exc:  # noqa: BLE001 - normalize transport failures for Feishu
                last_error = exc
                self._websocket = None
                time.sleep(0.2)
        if self._websocket is None:
            if last_error is None:
                raise CodexAppServerError(
                    f"找不到共享 Codex app-server socket：{socket_path}。"
                    "请先运行 install_launchd.py --load 并重启 Codex App。"
                )
            raise CodexAppServerError(f"无法连接共享 Codex app-server socket：{last_error}") from last_error
        LOGGER.info("connected to shared Codex app-server socket %s", socket_path)

    def _read_stdout(self, stream: Any) -> None:
        try:
            for line in stream:
                self._events.put(("stdout", line))
        finally:
            self._events.put(("eof", None))

    def _read_stderr(self, stream: Any) -> None:
        try:
            for line in stream:
                line = line.rstrip("\n")
                if not line:
                    continue
                with self._log_lock:
                    self._stderr_lines.append(line)
                LOGGER.debug("codex app-server stderr: %s", line)
        finally:
            stream.close()

    def _request(
        self,
        method: str,
        params: dict[str, Any],
        *,
        deadline: float,
        should_cancel: Callable[[], bool] | None,
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})

        while True:
            event = self._next_event(deadline=deadline, should_cancel=should_cancel)
            if event is None:
                raise CodexAppServerError(f"Codex app-server 在等待 {method} 响应时退出")
            if event.get("id") == request_id:
                if "error" in event:
                    raise CodexAppServerError(f"{method} 失败：{self._error_text(event.get('error'))}")
                result = event.get("result")
                if not isinstance(result, dict):
                    raise CodexAppServerError(f"{method} 返回了无效结果")
                return result
            self._handle_event(event)

    def _read_until_turn_completed(
        self,
        *,
        deadline: float,
        should_cancel: Callable[[], bool] | None,
    ) -> None:
        while not self._turn_completed:
            event = self._next_event(deadline=deadline, should_cancel=should_cancel)
            if event is None:
                raise CodexAppServerError("Codex app-server 在 turn 完成前退出")
            self._handle_event(event)

    def _next_event(
        self,
        *,
        deadline: float,
        should_cancel: Callable[[], bool] | None,
    ) -> dict[str, Any] | None:
        while True:
            if should_cancel and should_cancel():
                raise CodexAppServerCancelled("任务已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexAppServerTimeout("任务超时")
            if self._websocket is not None:
                try:
                    message = self._websocket.recv(timeout=min(0.5, remaining))
                except TimeoutError:
                    continue
                except EOFError:
                    return None
                except Exception as exc:  # noqa: BLE001 - websocket close types vary by version
                    if exc.__class__.__name__.startswith("ConnectionClosed"):
                        return None
                    raise CodexAppServerError(f"读取 Codex app-server 事件失败：{exc}") from exc
                if message is None:
                    return None
                if isinstance(message, bytes):
                    raw = message.decode("utf-8", errors="replace").strip()
                else:
                    raw = str(message).strip()
            else:
                try:
                    kind, line = self._events.get(timeout=min(0.5, remaining))
                except queue.Empty:
                    continue
                if kind == "eof":
                    return None
                if not line:
                    continue
                raw = line.strip()
            if not raw:
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                self._append_log(f"<< [非 JSON stdout] {raw}")
                continue
            if not isinstance(event, dict):
                self._append_log(f"<< {raw}")
                continue
            self._append_log(f"<< {raw}")
            if not self._event_belongs_to_this_turn(event):
                params = event.get("params") if isinstance(event.get("params"), dict) else {}
                self._append_log(
                    "<< [忽略其他 thread 事件] "
                    f"method={event.get('method')} threadId={params.get('threadId')} turnId={params.get('turnId')}"
                )
                continue
            return event

    def _event_belongs_to_this_turn(self, event: dict[str, Any]) -> bool:
        """Keep app-server notifications scoped to this client's thread.

        An app-server connection can receive notifications generated by child
        agent threads as well as the main thread.  Without this guard, an
        agent packet or turn/completed notification can be mistaken for this
        job's final result, even on the stdio transport.
        """
        # JSON-RPC responses belong to this client request id and do not carry
        # a threadId in their envelope.
        if "id" in event and ("result" in event or "error" in event):
            return True

        params = event.get("params")
        if not isinstance(params, dict):
            return False
        thread_id = params.get("threadId")
        if isinstance(thread_id, str) and thread_id:
            if self._thread_id != thread_id:
                return False
            turn_id = params.get("turnId")
            if isinstance(turn_id, str) and turn_id and self._turn_id and turn_id != self._turn_id:
                return False
            return True

        # Server requests such as approval/user-input requests may not carry
        # a thread id in older app-server versions.  Once our thread exists,
        # let the existing policy handler answer those requests.
        return "id" in event and isinstance(event.get("method"), str) and self._thread_id is not None

    def _handle_event(self, event: dict[str, Any]) -> None:
        if not self._event_belongs_to_this_turn(event):
            return
        method = event.get("method")
        if isinstance(method, str) and "id" in event and "result" not in event and "error" not in event:
            self._handle_server_request(event)
            return
        if not isinstance(method, str):
            return

        params = event.get("params")
        if not isinstance(params, dict):
            params = {}
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if isinstance(item_id, str) and isinstance(delta, str):
                if item_id not in self._agent_message_fragments:
                    self._agent_message_ids.append(item_id)
                    self._agent_message_fragments[item_id] = []
                self._agent_message_fragments[item_id].append(delta)
            return
        if method == "item/completed":
            item = params.get("item")
            if isinstance(item, dict) and item.get("type") == "agentMessage":
                item_id = item.get("id")
                text = item.get("text")
                if isinstance(item_id, str):
                    if item_id not in self._agent_message_ids:
                        self._agent_message_ids.append(item_id)
                    if isinstance(text, str):
                        self._completed_agent_messages[item_id] = text
                    phase = item.get("phase")
                    if isinstance(phase, str):
                        self._agent_message_phases[item_id] = phase
                        if phase == "final_answer" and item_id not in self._final_answer_message_ids:
                            self._final_answer_message_ids.append(item_id)
            return
        if method == "turn/completed":
            turn = params.get("turn")
            if isinstance(turn, dict):
                status = turn.get("status")
                self._turn_status = status if isinstance(status, str) else None
                error = turn.get("error")
                if isinstance(error, dict) and isinstance(error.get("message"), str):
                    self._turn_error = error["message"]
            self._turn_completed = True
            return
        if method == "error" and params.get("willRetry") is True:
            self._turn_error = self._error_text(params)
            return
        if method in {"turn/failed", "error"}:
            self._turn_error = self._error_text(params)
            self._turn_status = "failed"
            self._turn_completed = True
            return

    def _handle_server_request(self, event: dict[str, Any]) -> None:
        request_id = event.get("id")
        method = str(event.get("method", ""))
        if method == "item/permissions/requestApproval":
            result: dict[str, Any] = {
                "permissions": {"fileSystem": None, "network": None},
                "scope": "turn",
            }
        elif "requestApproval" in method:
            result = {"decision": "decline"}
        elif method == "item/tool/requestUserInput":
            result = {"answers": {}}
        elif method == "mcpServer/elicitation/request":
            result = {"action": "decline"}
        else:
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32601, "message": f"客户端不支持请求：{method}"},
                }
            )
            return
        self._send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _send(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        self._append_log(f">> {encoded}")
        try:
            if self._websocket is not None:
                self._websocket.send(encoded)
                return
            process = self._process
            if process is None or process.stdin is None:
                raise CodexAppServerError("Codex app-server stdin 不可用")
            process.stdin.write(encoded + "\n")
            process.stdin.flush()
        except CodexAppServerError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalize stdio and websocket failures
            raise CodexAppServerError(f"无法向 Codex app-server 发送请求：{exc}") from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _stop_process(self) -> None:
        websocket = self._websocket
        self._websocket = None
        if websocket is not None:
            try:
                websocket.close()
            except Exception as exc:  # noqa: BLE001 - cleanup should not mask the task result
                LOGGER.debug("failed to close shared app-server websocket: %s", exc)
            return

        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
        for reader in (self._stdout_reader, self._stderr_reader):
            if reader:
                reader.join(timeout=1)
        if process.stdin:
            process.stdin.close()
        if process.stdout:
            process.stdout.close()
        if process.stderr:
                process.stderr.close()

    def _unsubscribe_current_thread(self) -> None:
        """Release the app-server subscription before closing the transport.

        ``thread/start`` automatically subscribes the connection to the new
        thread.  Closing the WebSocket normally releases that subscription as
        well, but doing it explicitly lets app-server start its no-subscriber
        unload grace period immediately and avoids retaining thread resources
        when transport cleanup is delayed.
        """

        if not self._thread_id:
            return
        try:
            self._request(
                "thread/unsubscribe",
                {"threadId": self._thread_id},
                deadline=time.monotonic() + 3.0,
                should_cancel=None,
            )
            LOGGER.info("unsubscribed from Codex App thread %s", self._thread_id)
        except Exception as exc:  # noqa: BLE001 - cleanup must not mask the task result
            LOGGER.warning(
                "failed to unsubscribe from Codex App thread %s: %s",
                self._thread_id,
                exc,
            )

    def _interrupt_current_turn(self) -> None:
        if self._websocket is None or not self._thread_id or not self._turn_id:
            return
        try:
            self._request_id += 1
            self._send(
                {
                    "jsonrpc": "2.0",
                    "id": self._request_id,
                    "method": "turn/interrupt",
                    "params": {"threadId": self._thread_id, "turnId": self._turn_id},
                }
            )
        except Exception as exc:  # noqa: BLE001 - best effort during cancellation/timeout
            LOGGER.debug("failed to interrupt shared app-server turn: %s", exc)

    def _final_agent_message(self) -> str:
        ordered_ids = [*reversed(self._final_answer_message_ids), *reversed(self._agent_message_ids)]
        seen: set[str] = set()
        for item_id in ordered_ids:
            if item_id in seen:
                continue
            seen.add(item_id)
            text = self._completed_agent_messages.get(item_id)
            if isinstance(text, str) and text.strip():
                return text.strip()
            fragments = "".join(self._agent_message_fragments.get(item_id, []))
            if fragments.strip():
                return fragments.strip()
        return ""

    def _protocol_log(self) -> str:
        with self._log_lock:
            lines = list(self._log_lines)
            if self._stderr_lines:
                lines.append("-- stderr --")
                lines.extend(self._stderr_lines)
        return "\n".join(lines) + ("\n" if lines else "")

    def protocol_log(self) -> str:
        return self._protocol_log()

    def _append_log(self, line: str) -> None:
        with self._log_lock:
            self._log_lines.append(line)

    @staticmethod
    def _error_text(value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            for key in ("message", "error", "detail"):
                if isinstance(value.get(key), str) and value[key].strip():
                    return value[key]
            nested = value.get("error")
            if nested is not value:
                nested_text = CodexAppServerClient._error_text(nested)
                if nested_text:
                    return nested_text
            try:
                return json.dumps(value, ensure_ascii=False)
            except TypeError:
                return str(value)
        return str(value)
