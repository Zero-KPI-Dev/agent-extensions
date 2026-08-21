from __future__ import annotations

import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from review_runtime import (  # noqa: E402
    ProtocolError,
    ReviewRuntimeStore,
    resolve_state_db,
)


class ReviewRuntimeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "runs.sqlite3"
        self.store = ReviewRuntimeStore(self.db_path)

    def tearDown(self) -> None:
        self.store.close()
        self.temp_dir.cleanup()

    def create_run(self) -> str:
        return self.store.create_run(
            repository="/tmp/example",
            pr_number=223,
            base_sha="base",
            head_sha="head",
        )

    def test_create_register_heartbeat_progress_and_cursor(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)

        heartbeat = self.store.heartbeat(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=1,
            phase="diff_analysis",
            activity="working",
            lease_seconds=60,
        )
        self.assertEqual(heartbeat["last_seq"], 1)

        self.store.progress(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=2,
            phase="test_verification",
            message="running focused tests",
        )

        page, cursor = self.store.list_events(run_id, after_event_id=0, limit=10)
        self.assertEqual([item["event_type"] for item in page], ["heartbeat", "progress"])
        self.assertEqual(cursor, page[-1]["event_id"])

        repeated, repeated_cursor = self.store.list_events(
            run_id,
            after_event_id=page[0]["event_id"],
            limit=10,
        )
        self.assertEqual([item["event_type"] for item in repeated], ["progress"])
        self.assertEqual(repeated_cursor, page[-1]["event_id"])

    def test_leader_wait_observation_is_separate_from_agent_heartbeat(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)

        first = self.store.record_wait_observation(
            run_id=run_id,
            target_agent_id="A",
            phase="waiting_for_a_initial",
            wait_seconds=30,
            timed_out=True,
        )
        second = self.store.record_wait_observation(
            run_id=run_id,
            target_agent_id="A",
            phase="waiting_for_a_initial",
            wait_seconds=30,
            timed_out=True,
        )

        self.assertEqual(first["seq"], 1)
        self.assertEqual(second["seq"], 2)
        events, _ = self.store.list_events(run_id, limit=10)
        self.assertEqual([item["event_type"] for item in events], ["observation", "observation"])
        self.assertEqual(events[0]["agent_id"], "LEADER")
        self.assertEqual(events[0]["payload"]["target_agent_id"], "A")
        self.assertTrue(events[0]["payload"]["timed_out"])
        self.assertEqual(self.store.get_run(run_id)["status"], "RUNNING")

    def test_duplicate_seq_is_idempotent_but_stale_seq_is_rejected(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)

        first = self.store.heartbeat(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=1,
            phase="starting",
            activity="working",
            lease_seconds=60,
        )
        duplicate = self.store.heartbeat(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=1,
            phase="starting",
            activity="working",
            lease_seconds=60,
        )
        self.assertFalse(first["duplicate"])
        self.assertTrue(duplicate["duplicate"])

        with self.assertRaises(ProtocolError):
            self.store.heartbeat(
                run_id=run_id,
                agent_id="A",
                epoch=1,
                seq=0,
                phase="starting",
                activity="working",
                lease_seconds=60,
            )

        with self.assertRaises(ProtocolError):
            self.store.heartbeat(
                run_id=run_id,
                agent_id="A",
                epoch=0,
                seq=2,
                phase="starting",
                activity="working",
                lease_seconds=60,
            )

    def test_concurrent_heartbeat_writes_are_serialized(self) -> None:
        run_id = self.create_run()
        for index in range(4):
            self.store.register_agent(run_id, f"A{index}", "primary", epoch=1)

        def write_agent(agent_id: str) -> None:
            for seq in range(1, 11):
                self.store.heartbeat(
                    run_id=run_id,
                    agent_id=agent_id,
                    epoch=1,
                    seq=seq,
                    phase="diff_analysis",
                    activity="working",
                    lease_seconds=60,
                )

        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(write_agent, [f"A{index}" for index in range(4)]))

        events, _ = self.store.list_events(run_id, limit=100)
        self.assertEqual(len(events), 40)
        for index in range(4):
            self.assertEqual(self.store.get_agent(run_id, f"A{index}")["last_seq"], 10)

    def test_terminal_run_rejects_late_events(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)
        self.store.close_run(run_id, status="COMPLETED", summary={"findings": 0})

        with self.assertRaises(ProtocolError):
            self.store.progress(
                run_id=run_id,
                agent_id="A",
                epoch=1,
                seq=1,
                phase="done",
                message="late",
            )
        with self.assertRaises(ProtocolError):
            self.store.complete_agent(run_id, "A", epoch=1)

        self.assertEqual(self.store.get_run(run_id)["status"], "COMPLETED")

    def test_expired_agent_is_observable_before_replacement(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)
        self.store.heartbeat(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=1,
            phase="model_call",
            activity="waiting_model_response",
            lease_seconds=60,
        )

        expired = self.store.expired_agents(
            now=datetime.now(timezone.utc) + timedelta(seconds=61)
        )
        self.assertEqual([(item["agent_id"], item["status"]) for item in expired], [("A", "RUNNING")])

        self.store.request_settle(run_id, "A", epoch=1)
        self.store.request_settle(run_id, "A", epoch=1)
        self.assertEqual(self.store.get_agent(run_id, "A")["status"], "SETTLING")
        events, _ = self.store.list_events(run_id, limit=10)
        self.assertEqual([item["event_type"] for item in events], ["heartbeat", "lifecycle"])

    def test_duplicate_packet_delivery_is_idempotent(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)
        packet = {"packet_type": "A_INITIAL", "findings": []}

        first = self.store.record_packet(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            packet_type="A_INITIAL",
            packet=packet,
            validation_status="VALID",
        )
        duplicate = self.store.record_packet(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            packet_type="A_INITIAL",
            packet=packet,
            validation_status="VALID",
        )

        self.assertEqual(first, duplicate)
        events, _ = self.store.list_events(run_id, limit=10)
        self.assertEqual(len(events), 1)
        self.assertEqual(self.store.get_agent(run_id, "A")["last_seq"], 1)

    def test_packet_is_persisted_and_cleanup_keeps_summary_then_removes_run(self) -> None:
        run_id = self.create_run()
        self.store.register_agent(run_id, "A", "primary", epoch=1)
        self.store.progress(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            seq=1,
            phase="review",
            message="done",
        )
        self.store.record_packet(
            run_id=run_id,
            agent_id="A",
            epoch=1,
            packet_type="A_INITIAL",
            packet={"packet_type": "A_INITIAL", "findings": []},
            validation_status="VALID",
        )
        self.store.close_run(run_id, status="COMPLETED", summary={"status": "NO_ACTIONABLE_FINDINGS"})

        first_cleanup = self.store.cleanup(
            now=datetime.now(timezone.utc) + timedelta(days=2),
            event_retention=timedelta(days=1),
            terminal_retention=timedelta(days=30),
        )
        self.assertGreaterEqual(first_cleanup["events_deleted"], 1)
        self.assertIsNotNone(self.store.get_run(run_id))
        self.assertEqual(self.store.get_run(run_id)["summary"]["status"], "NO_ACTIONABLE_FINDINGS")

        second_cleanup = self.store.cleanup(
            now=datetime.now(timezone.utc) + timedelta(days=31),
            event_retention=timedelta(days=1),
            terminal_retention=timedelta(days=30),
        )
        self.assertEqual(second_cleanup["runs_deleted"], 1)
        with self.assertRaises(KeyError):
            self.store.get_run(run_id)

    def test_cleanup_does_not_delete_active_run(self) -> None:
        run_id = self.create_run()
        result = self.store.cleanup(
            now=datetime.now(timezone.utc) + timedelta(days=365),
            event_retention=timedelta(days=1),
            terminal_retention=timedelta(days=1),
        )
        self.assertEqual(result["runs_deleted"], 0)
        self.assertEqual(self.store.get_run(run_id)["status"], "CREATED")


class StatePathTests(unittest.TestCase):
    def test_explicit_state_directory_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"REVIEW_PR_PANEL_STATE_DIR": temp_dir}):
                self.assertEqual(
                    resolve_state_db(),
                    Path(temp_dir) / "runs.sqlite3",
                )

    def test_explicit_database_path_is_used(self) -> None:
        path = Path("/tmp/review-pr-panel-test.sqlite3")
        self.assertEqual(resolve_state_db(path), path)


if __name__ == "__main__":
    unittest.main()
