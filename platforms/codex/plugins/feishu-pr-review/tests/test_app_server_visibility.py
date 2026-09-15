from __future__ import annotations

import unittest
from typing import Any
from server.codex_app_server import CodexAppServerClient


class StreamingAppServerClient(CodexAppServerClient):
    def __init__(self) -> None:
        super().__init__("codex", transport="shared_unix", socket_path="/tmp/fake.sock")
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.subscription_states_while_streaming: list[bool] = []
        self.events = iter(
            [
                {
                    "method": "item/completed",
                    "params": {
                        "threadId": "thread-visible",
                        "turnId": "turn-visible",
                        "item": {
                            "id": "answer-visible",
                            "type": "agentMessage",
                            "phase": "final_answer",
                            "text": "review complete",
                        },
                    },
                },
                {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "thread-visible",
                        "turnId": "turn-visible",
                        "turn": {"id": "turn-visible", "status": "completed"},
                    },
                },
            ]
        )

    def _connect_socket(self) -> None:
        return

    def _stop_process(self) -> None:
        return

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        return

    def _request(self, method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "initialize":
            return {}
        if method == "thread/start":
            return {"thread": {"id": "thread-visible"}}
        if method == "turn/start":
            return {"turn": {"id": "turn-visible"}}
        if method in {"thread/unsubscribe", "thread/name/set"}:
            return {}
        raise AssertionError(method)

    def _next_event(self, **_kwargs: Any) -> dict[str, Any] | None:
        self.subscription_states_while_streaming.append(self._thread_subscribed)
        return next(self.events, None)


class AppServerVisibilityTests(unittest.TestCase):
    def test_shared_transport_keeps_subscription_until_turn_completes(self) -> None:
        client = StreamingAppServerClient()

        result = client.run(
            cwd="/tmp",
            prompt="review",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            thread_name="Example#413",
        )

        methods = [method for method, _params in client.requests]
        self.assertEqual(
            methods,
            [
                "initialize",
                "thread/start",
                "turn/start",
                "thread/name/set",
                "thread/unsubscribe",
            ],
        )
        self.assertEqual(result.report, "review complete")
        self.assertEqual(client.subscription_states_while_streaming, [True, True])
        self.assertFalse(client._thread_subscribed)

    def test_turn_can_enable_network_while_repository_remains_read_only(self) -> None:
        client = StreamingAppServerClient()

        client.run(
            cwd="/tmp",
            prompt="review",
            sandbox="read-only",
            network_access=True,
            timeout_seconds=30,
            env={},
        )

        thread_params = next(params for method, params in client.requests if method == "thread/start")
        turn_params = next(params for method, params in client.requests if method == "turn/start")
        self.assertEqual(thread_params["sandbox"], "read-only")
        self.assertEqual(
            turn_params["sandboxPolicy"],
            {"type": "readOnly", "networkAccess": True},
        )

    def test_persisted_interrupted_turn_is_reconciled(self) -> None:
        client = StreamingAppServerClient()
        client._thread_id = "thread-visible"
        client._turn_id = "turn-visible"

        def request(method: str, params: dict[str, Any], **_kwargs: Any) -> dict[str, Any]:
            self.assertEqual(method, "thread/turns/list")
            self.assertEqual(params["itemsView"], "notLoaded")
            return {
                "data": [
                    {
                        "id": "turn-visible",
                        "status": "interrupted",
                        "items": [],
                    }
                ]
            }

        client._request = request  # type: ignore[method-assign]
        client._refresh_persisted_turn_status(
            deadline=30.0,
            should_cancel=None,
        )

        self.assertTrue(client._turn_completed)
        self.assertEqual(client._turn_status, "interrupted")
        self.assertEqual(client._turn_error, "Codex turn 已在 App 中中断")


if __name__ == "__main__":
    unittest.main()
