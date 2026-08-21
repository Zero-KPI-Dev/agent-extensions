from __future__ import annotations

import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from server.db import StateStore


class ActiveJobDedupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp_dir.name) / "state.sqlite3")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def submit(
        self,
        event_id: str,
        *,
        pr_url: str = "https://github.com/org/repo/pull/42",
        chat_id: str = "chat-a",
    ) -> tuple[dict[str, object], bool]:
        return self.store.create_or_get_active_job(
            event_id=event_id,
            bot_key="review-bot",
            chat_id=chat_id,
            sender_id=f"sender-{event_id}",
            message_id=f"message-{event_id}",
            request_text="review #42",
            pr_url=pr_url,
            repo_key="org/repo",
        )

    def test_active_duplicate_reuses_job_and_fans_out_once_per_chat(self) -> None:
        original, created = self.submit("event-1")
        duplicate, duplicate_created = self.submit(
            "event-2",
            pr_url="https://github.com/ORG/REPO/pull/42/",
            chat_id="chat-b",
        )
        same_chat, same_chat_created = self.submit("event-3", chat_id="chat-a")

        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertFalse(same_chat_created)
        self.assertEqual(original["job_id"], duplicate["job_id"])
        self.assertEqual(original["job_id"], same_chat["job_id"])
        self.assertEqual(len(self.store.list_jobs()), 1)
        self.assertEqual(
            self.store.delivery_targets(str(original["job_id"])),
            [
                {"bot_key": "review-bot", "chat_id": "chat-a"},
                {"bot_key": "review-bot", "chat_id": "chat-b"},
            ],
        )

    def test_concurrent_duplicates_create_only_one_job(self) -> None:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda index: self.submit(f"event-{index}"), range(8)))

        job_ids = {str(job["job_id"]) for job, _created in results}
        created_count = sum(1 for _job, created in results if created)
        self.assertEqual(len(job_ids), 1)
        self.assertEqual(created_count, 1)
        self.assertEqual(len(self.store.list_jobs()), 1)

    def test_completed_job_does_not_block_explicit_rereview(self) -> None:
        first, first_created = self.submit("event-1")
        self.assertTrue(first_created)
        self.store.finish(str(first["job_id"]), status="succeeded")

        second, second_created = self.submit("event-2")
        self.assertTrue(second_created)
        self.assertNotEqual(first["job_id"], second["job_id"])
        self.assertEqual(len(self.store.list_jobs()), 2)


if __name__ == "__main__":
    unittest.main()
