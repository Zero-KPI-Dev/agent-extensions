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
        resume_failure: str | None = None,
        archived_once: bool = False,
    ) -> None:
        super().__init__("codex", transport="shared_unix", socket_path="/tmp/fake.sock")
        self.fail_naming = fail_naming
        self.transient_naming_failures = transient_naming_failures
        self.resume_failure = resume_failure
        self.archived_once = archived_once
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
        if method == "thread/resume":
            if self.archived_once:
                self.archived_once = False
                raise CodexAppServerError(
                    f"session {params['threadId']} is archived. Run `codex unarchive` first."
                )
            if self.resume_failure:
                raise CodexAppServerError(self.resume_failure)
            return {"thread": {"id": params["threadId"]}}
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
        self.assertFalse(result.resumed)

    def test_client_resumes_existing_pr_thread(self) -> None:
        client = FakeAppServerClient()
        ready: list[tuple[str, bool]] = []

        result = client.run(
            cwd="/tmp",
            prompt="review again",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            thread_name="EchoMem#345",
            resume_thread_id="thread-existing",
            on_thread_ready=lambda thread_id, resumed: ready.append((thread_id, resumed)),
        )

        self.assertEqual(
            [method for method, _params in client.requests],
            [
                "initialize",
                "thread/resume",
                "turn/start",
                "thread/name/set",
                "thread/unsubscribe",
            ],
        )
        resume_params = client.requests[1][1]
        self.assertEqual(resume_params["threadId"], "thread-existing")
        self.assertNotIn("excludeTurns", resume_params)
        self.assertEqual(ready, [("thread-existing", True)])
        self.assertEqual(result.thread_id, "thread-existing")
        self.assertTrue(result.resumed)

    def test_missing_pr_thread_starts_replacement(self) -> None:
        client = FakeAppServerClient(resume_failure="thread/resume 失败：thread not found")

        result = client.run(
            cwd="/tmp",
            prompt="review again",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            resume_thread_id="thread-deleted",
        )

        self.assertEqual(
            [method for method, _params in client.requests],
            [
                "initialize",
                "thread/resume",
                "thread/start",
                "turn/start",
                "thread/unsubscribe",
            ],
        )
        self.assertEqual(result.thread_id, "thread-1")
        self.assertFalse(result.resumed)

    def test_archived_pr_thread_starts_new_thread_without_unarchiving(self) -> None:
        client = FakeAppServerClient(archived_once=True)
        ready: list[tuple[str, bool]] = []

        result = client.run(
            cwd="/tmp",
            prompt="review again",
            sandbox="read-only",
            timeout_seconds=30,
            env={},
            resume_thread_id="thread-archived",
            on_thread_ready=lambda thread_id, resumed: ready.append((thread_id, resumed)),
        )

        self.assertEqual(
            [method for method, _params in client.requests],
            [
                "initialize",
                "thread/resume",
                "thread/start",
                "turn/start",
                "thread/unsubscribe",
            ],
        )
        self.assertEqual(result.thread_id, "thread-1")
        self.assertFalse(result.resumed)
        self.assertEqual(ready, [("thread-1", False)])

    def test_resume_resource_failure_does_not_hide_problem_with_new_thread(self) -> None:
        client = FakeAppServerClient(resume_failure="Too many open files (os error 24)")

        with self.assertRaisesRegex(CodexAppServerError, "Too many open files"):
            client.run(
                cwd="/tmp",
                prompt="review again",
                sandbox="read-only",
                timeout_seconds=30,
                env={},
                resume_thread_id="thread-existing",
            )

        self.assertEqual(
            [method for method, _params in client.requests],
            ["initialize", "thread/resume"],
        )

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
