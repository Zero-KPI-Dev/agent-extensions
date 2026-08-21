from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from server.codex_app_server import CodexAppServerClient, CodexAppServerError
from server.gateway import _pr_task_title


class FakeAppServerClient(CodexAppServerClient):
    def __init__(
        self,
        *,
        fail_naming: bool = False,
        transient_naming_failures: int = 0,
    ) -> None:
        super().__init__("codex", transport="shared_unix", socket_path="/tmp/fake.sock")
        self.fail_naming = fail_naming
        self.transient_naming_failures = transient_naming_failures
        self.requests: list[tuple[str, dict[str, Any]]] = []

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
            return {"thread": {"id": "thread-1"}}
        if method == "thread/name/set":
            if self.fail_naming:
                raise CodexAppServerError("unsupported")
            if self.transient_naming_failures:
                self.transient_naming_failures -= 1
                raise CodexAppServerError(
                    "failed to read session metadata: rollout at /tmp/thread.jsonl is empty"
                )
            return {}
        if method == "turn/start":
            self._turn_status = "completed"
            self._turn_completed = True
            self._agent_message_ids.append("answer-1")
            self._completed_agent_messages["answer-1"] = "done"
            return {"turn": {"id": "turn-1"}}
        if method == "thread/unsubscribe":
            return {}
        raise AssertionError(method)


class TaskTitleTests(unittest.TestCase):
    def test_title_uses_local_repository_casing(self) -> None:
        self.assertEqual(
            _pr_task_title(
                "https://github.com/tech-innovation-group/echomem/pull/345",
                Path("/Users/example/Workspaces/EchoMem"),
            ),
            "EchoMem#345",
        )

    def test_client_names_thread_after_starting_turn(self) -> None:
        client = FakeAppServerClient()
        result = client.run(
            cwd="/tmp",
            prompt="review",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            thread_name="EchoMem#345",
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
        turn_params = client.requests[2][1]
        self.assertEqual(turn_params["input"][0]["text"], "review")
        self.assertEqual(result.report, "done")

    def test_naming_failure_does_not_fail_review(self) -> None:
        client = FakeAppServerClient(fail_naming=True)
        result = client.run(
            cwd="/tmp",
            prompt="review",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            thread_name="EchoMem#345",
        )

        self.assertEqual(result.report, "done")
        turn_params = client.requests[2][1]
        self.assertEqual(turn_params["input"][0]["text"], "review")
        self.assertEqual(
            [method for method, _params in client.requests].count("thread/name/set"),
            1,
        )

    def test_transient_rollout_race_is_retried(self) -> None:
        client = FakeAppServerClient(transient_naming_failures=2)

        with patch("server.codex_app_server.time.sleep") as sleep:
            result = client.run(
                cwd="/tmp",
                prompt="review",
                sandbox="read-only",
                timeout_seconds=30,
                env={},
                thread_name="EchoMem#345",
            )

        self.assertEqual(result.report, "done")
        self.assertEqual(
            [method for method, _params in client.requests].count("thread/name/set"),
            3,
        )
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.05, 0.10])

    def test_unsubscribe_failure_does_not_mask_completed_review(self) -> None:
        client = FakeAppServerClient()
        original_request = client._request

        def fail_unsubscribe(method: str, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
            if method == "thread/unsubscribe":
                raise CodexAppServerError("temporary cleanup failure")
            return original_request(method, params, **kwargs)

        with patch.object(client, "_request", side_effect=fail_unsubscribe):
            result = client.run(
                cwd="/tmp",
                prompt="review",
                sandbox="read-only",
                timeout_seconds=30,
                env={},
            )

        self.assertEqual(result.report, "done")


if __name__ == "__main__":
    unittest.main()
