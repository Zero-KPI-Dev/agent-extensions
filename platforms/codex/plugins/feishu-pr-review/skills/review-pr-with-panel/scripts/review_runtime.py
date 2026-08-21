"""SQLite-backed runtime coordination for review-pr-with-panel.

The module is intentionally independent from the Codex agent transport. It
stores run state, leases, lifecycle events, and review packets so a Leader can
recover from delayed or duplicated messages without treating a wait timeout as
an agent failure.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional, Sequence, Tuple

LOGGER = logging.getLogger(__name__)

TERMINAL_RUN_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "ABANDONED"})
ACTIVE_AGENT_STATUSES = frozenset({"REGISTERED", "RUNNING", "SETTLING"})
TERMINAL_AGENT_STATUSES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "STALE"})


class ProtocolError(RuntimeError):
    """Raised when an event violates the run/agent protocol."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _decode(value: Optional[str], default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _platform_state_root() -> Path:
    explicit = os.environ.get("REVIEW_PR_PANEL_STATE_DIR")
    if explicit:
        return Path(explicit).expanduser()

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "Codex" / "review-pr-with-panel"

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Codex" / "review-pr-with-panel"

    base = os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base).expanduser() / "codex" / "review-pr-with-panel"
    return Path.home() / ".local" / "state" / "codex" / "review-pr-with-panel"


def resolve_state_db(path: Optional[Path] = None) -> Path:
    """Resolve the shared database without requiring a service or repo path."""

    if path is not None:
        return Path(path).expanduser()
    explicit_db = os.environ.get("REVIEW_PR_PANEL_STATE_DB")
    if explicit_db:
        return Path(explicit_db).expanduser()
    return _platform_state_root() / "runs.sqlite3"


class ReviewRuntimeStore:
    """Small transactional store used by the Skill-owned review runner."""

    def __init__(self, db_path: Optional[Path] = None, *, timeout: float = 5.0) -> None:
        self.db_path = resolve_state_db(db_path)
        self.timeout = timeout
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def close(self) -> None:
        """Compatibility hook; connections are short-lived per transaction."""

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            str(self.db_path),
            timeout=self.timeout,
            isolation_level=None,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        if write:
            connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if write:
                connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection(write=False) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    skill_name TEXT NOT NULL,
                    repository TEXT NOT NULL,
                    pr_number INTEGER,
                    base_sha TEXT,
                    head_sha TEXT,
                    status TEXT NOT NULL,
                    current_epoch INTEGER NOT NULL DEFAULT 1,
                    summary_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS agents (
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    last_seq INTEGER NOT NULL DEFAULT 0,
                    last_heartbeat_at TEXT,
                    last_progress_at TEXT,
                    lease_until TEXT,
                    phase TEXT,
                    activity TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, agent_id),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    phase TEXT,
                    status TEXT,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (run_id, agent_id, epoch, seq),
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS packets (
                    packet_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    epoch INTEGER NOT NULL,
                    packet_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    validation_status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS events_run_cursor
                    ON events(run_id, event_id);
                CREATE INDEX IF NOT EXISTS events_retention
                    ON events(event_type, created_at);
                CREATE INDEX IF NOT EXISTS runs_retention
                    ON runs(status, closed_at);
                CREATE INDEX IF NOT EXISTS agents_lease
                    ON agents(status, lease_until);
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO schema_meta(key, value) VALUES('schema_version', '1')"
            )

    def create_run(
        self,
        *,
        repository: str,
        pr_number: Optional[int],
        base_sha: Optional[str],
        head_sha: Optional[str],
        skill_name: str = "review-pr-with-panel",
        run_id: Optional[str] = None,
    ) -> str:
        run_id = run_id or f"R-{_utc_now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:12]}"
        now = _iso(_utc_now())
        with self._connection(write=True) as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, skill_name, repository, pr_number, base_sha, head_sha,
                        status, created_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, 'CREATED', ?, ?)
                    """,
                    (run_id, skill_name, repository, pr_number, base_sha, head_sha, now, now),
                )
                connection.execute(
                    """
                    INSERT INTO agents(
                        run_id, agent_id, role, epoch, status, updated_at
                    ) VALUES(?, 'LEADER', 'orchestrator', 1, 'REGISTERED', ?)
                    """,
                    (run_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ProtocolError(f"run_id already exists: {run_id}") from exc
        return run_id

    def register_agent(
        self,
        run_id: str,
        agent_id: str,
        role: str,
        *,
        epoch: int = 1,
    ) -> Mapping[str, Any]:
        now = _iso(_utc_now())
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            existing = connection.execute(
                "SELECT * FROM agents WHERE run_id = ? AND agent_id = ?",
                (run_id, agent_id),
            ).fetchone()
            if existing is not None:
                if int(existing["epoch"]) > epoch:
                    raise ProtocolError("agent epoch is older than the registered epoch")
                if int(existing["epoch"]) == epoch:
                    return self._agent_dict(existing)
                connection.execute(
                    """
                    UPDATE agents
                    SET epoch = ?, status = 'REGISTERED', last_seq = 0,
                        last_heartbeat_at = NULL, last_progress_at = NULL,
                        lease_until = NULL, phase = NULL, activity = ?, updated_at = ?
                    WHERE run_id = ? AND agent_id = ?
                    """,
                    (epoch, "epoch_restarted", now, run_id, agent_id),
                )
            else:
                connection.execute(
                    """
                    INSERT INTO agents(
                        run_id, agent_id, role, epoch, status, updated_at
                    ) VALUES(?, ?, ?, ?, 'REGISTERED', ?)
                    """,
                    (run_id, agent_id, role, epoch, now),
                )
            connection.execute(
                "UPDATE runs SET status = 'RUNNING', updated_at = ? WHERE run_id = ?",
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE runs
                SET current_epoch = CASE
                    WHEN current_epoch < ? THEN ?
                    ELSE current_epoch
                END,
                updated_at = ?
                WHERE run_id = ?
                """,
                (epoch, epoch, now, run_id),
            )
            row = connection.execute(
                "SELECT * FROM agents WHERE run_id = ? AND agent_id = ?",
                (run_id, agent_id),
            ).fetchone()
            return self._agent_dict(row)

    def heartbeat(
        self,
        *,
        run_id: str,
        agent_id: str,
        epoch: int,
        seq: int,
        phase: str,
        activity: str,
        lease_seconds: int = 60,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        return self._append_event(
            run_id=run_id,
            agent_id=agent_id,
            epoch=epoch,
            seq=seq,
            event_type="heartbeat",
            phase=phase,
            status="RUNNING",
            payload=payload or {"activity": activity},
            lease_seconds=lease_seconds,
            activity=activity,
            progress=False,
        )

    def progress(
        self,
        *,
        run_id: str,
        agent_id: str,
        epoch: int,
        seq: int,
        phase: str,
        message: str,
        payload: Optional[Mapping[str, Any]] = None,
    ) -> Mapping[str, Any]:
        data = dict(payload or {})
        data.setdefault("message", message)
        return self._append_event(
            run_id=run_id,
            agent_id=agent_id,
            epoch=epoch,
            seq=seq,
            event_type="progress",
            phase=phase,
            status="RUNNING",
            payload=data,
            lease_seconds=60,
            activity="progress",
            progress=True,
        )

    def record_wait_observation(
        self,
        *,
        run_id: str,
        target_agent_id: str,
        phase: str,
        wait_seconds: int,
        timed_out: bool,
    ) -> Mapping[str, Any]:
        """Record that Leader completed one bounded wait window.

        This is deliberately not an Agent heartbeat. It records the Leader's
        observation of the transport boundary so a short wait timeout cannot
        be mistaken for an Agent failure when no runtime callback is exposed.
        """

        if wait_seconds < 1:
            raise ProtocolError("wait_seconds must be positive")
        now_text = _iso(_utc_now())
        payload_text = _json(
            {
                "target_agent_id": target_agent_id,
                "wait_seconds": wait_seconds,
                "timed_out": timed_out,
            }
        )
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            self._ensure_leader_agent(connection, run_id, now_text)
            leader = self._require_agent(connection, run_id, "LEADER")
            seq = int(leader["last_seq"]) + 1
            cursor = connection.execute(
                """
                INSERT INTO events(
                    run_id, agent_id, epoch, seq, event_type, phase, status,
                    payload_json, created_at
                ) VALUES(?, 'LEADER', 1, ?, 'observation', ?, 'WAITING', ?, ?)
                """,
                (run_id, seq, phase, payload_text, now_text),
            )
            connection.execute(
                """
                UPDATE agents
                SET status = 'RUNNING', last_seq = ?, phase = ?,
                    activity = 'wait_window_observed', updated_at = ?
                WHERE run_id = ? AND agent_id = 'LEADER'
                """,
                (seq, phase, now_text, run_id),
            )
            connection.execute(
                "UPDATE runs SET status = 'RUNNING', updated_at = ? WHERE run_id = ?",
                (now_text, run_id),
            )
            return {
                "duplicate": False,
                "event_id": int(cursor.lastrowid),
                "seq": seq,
            }

    def _append_event(
        self,
        *,
        run_id: str,
        agent_id: str,
        epoch: int,
        seq: int,
        event_type: str,
        phase: str,
        status: str,
        payload: Mapping[str, Any],
        lease_seconds: int,
        activity: str,
        progress: bool,
    ) -> Mapping[str, Any]:
        if seq < 1:
            raise ProtocolError("event seq must be positive")
        now = _utc_now()
        now_text = _iso(now)
        payload_text = _json(payload)
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            agent = self._require_agent(connection, run_id, agent_id)
            if int(agent["epoch"]) != epoch:
                raise ProtocolError(
                    f"stale epoch for {agent_id}: received={epoch} current={agent['epoch']}"
                )
            existing = connection.execute(
                """
                SELECT event_id, payload_json
                FROM events
                WHERE run_id = ? AND agent_id = ? AND epoch = ? AND seq = ?
                """,
                (run_id, agent_id, epoch, seq),
            ).fetchone()
            if existing is not None:
                if existing["payload_json"] != payload_text:
                    raise ProtocolError("duplicate seq has a different payload")
                return {"duplicate": True, "event_id": int(existing["event_id"]), "last_seq": seq}
            if seq <= int(agent["last_seq"]):
                raise ProtocolError(
                    f"event seq must increase: received={seq} last={agent['last_seq']}"
                )
            lease_until = _iso(now + timedelta(seconds=max(1, lease_seconds)))
            cursor = connection.execute(
                """
                INSERT INTO events(
                    run_id, agent_id, epoch, seq, event_type, phase, status,
                    payload_json, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    agent_id,
                    epoch,
                    seq,
                    event_type,
                    phase,
                    status,
                    payload_text,
                    now_text,
                ),
            )
            if progress:
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'RUNNING', last_seq = ?, last_progress_at = ?,
                        last_heartbeat_at = ?, lease_until = ?, phase = ?,
                        activity = ?, updated_at = ?
                    WHERE run_id = ? AND agent_id = ?
                    """,
                    (
                        seq,
                        now_text,
                        now_text,
                        lease_until,
                        phase,
                        activity,
                        now_text,
                        run_id,
                        agent_id,
                    ),
                )
            else:
                connection.execute(
                    """
                    UPDATE agents
                    SET status = 'RUNNING', last_seq = ?, last_heartbeat_at = ?,
                        lease_until = ?, phase = ?, activity = ?, updated_at = ?
                    WHERE run_id = ? AND agent_id = ?
                    """,
                    (
                        seq,
                        now_text,
                        lease_until,
                        phase,
                        activity,
                        now_text,
                        run_id,
                        agent_id,
                    ),
                )
            connection.execute(
                "UPDATE runs SET updated_at = ? WHERE run_id = ?",
                (now_text, run_id),
            )
            return {"duplicate": False, "event_id": int(cursor.lastrowid), "last_seq": seq}

    def record_packet(
        self,
        *,
        run_id: str,
        agent_id: str,
        epoch: int,
        packet_type: str,
        packet: Mapping[str, Any],
        validation_status: str,
    ) -> int:
        now_text = _iso(_utc_now())
        packet_text = _json(packet)
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            agent = self._require_agent(connection, run_id, agent_id)
            if int(agent["epoch"]) != epoch:
                raise ProtocolError("packet belongs to an old agent epoch")
            existing = connection.execute(
                """
                SELECT packet_id
                FROM packets
                WHERE run_id = ? AND agent_id = ? AND epoch = ?
                  AND packet_type = ? AND payload_json = ?
                  AND validation_status = ?
                ORDER BY packet_id DESC
                LIMIT 1
                """,
                (run_id, agent_id, epoch, packet_type, packet_text, validation_status),
            ).fetchone()
            if existing is not None:
                return int(existing["packet_id"])
            seq = int(agent["last_seq"]) + 1
            packet_cursor = connection.execute(
                """
                INSERT INTO packets(
                    run_id, agent_id, epoch, packet_type, payload_json,
                    validation_status, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, agent_id, epoch, packet_type, packet_text, validation_status, now_text),
            )
            connection.execute(
                """
                INSERT INTO events(
                    run_id, agent_id, epoch, seq, event_type, phase, status,
                    payload_json, created_at
                ) VALUES(?, ?, ?, ?, 'packet', 'packet', ?, ?, ?)
                """,
                (run_id, agent_id, epoch, seq, validation_status, packet_text, now_text),
            )
            connection.execute(
                """
                UPDATE agents
                SET last_seq = ?, phase = 'packet', activity = ?,
                    last_progress_at = ?, updated_at = ?
                WHERE run_id = ? AND agent_id = ?
                """,
                (seq, packet_type, now_text, now_text, run_id, agent_id),
            )
            return int(packet_cursor.lastrowid)

    def complete_agent(
        self,
        run_id: str,
        agent_id: str,
        *,
        epoch: int,
        status: str = "COMPLETED",
    ) -> None:
        if status not in TERMINAL_AGENT_STATUSES:
            raise ProtocolError(f"invalid terminal agent status: {status}")
        now_text = _iso(_utc_now())
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            agent = self._require_agent(connection, run_id, agent_id)
            if int(agent["epoch"]) != epoch:
                raise ProtocolError("agent belongs to an old epoch")
            connection.execute(
                """
                UPDATE agents
                SET status = ?, lease_until = NULL, updated_at = ?
                WHERE run_id = ? AND agent_id = ?
                """,
                (status, now_text, run_id, agent_id),
            )

    def request_settle(self, run_id: str, agent_id: str, *, epoch: int) -> None:
        now_text = _iso(_utc_now())
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            self._ensure_active_run(run)
            agent = self._require_agent(connection, run_id, agent_id)
            if int(agent["epoch"]) != epoch:
                raise ProtocolError("agent belongs to an old epoch")
            if agent["status"] == "SETTLING":
                return
            if agent["status"] not in {"REGISTERED", "RUNNING"}:
                raise ProtocolError(f"cannot settle agent in status {agent['status']}")
            seq = int(agent["last_seq"]) + 1
            connection.execute(
                """
                INSERT INTO events(
                    run_id, agent_id, epoch, seq, event_type, phase, status,
                    payload_json, created_at
                ) VALUES(?, ?, ?, ?, 'lifecycle', 'settle', 'SETTLING', ?, ?)
                """,
                (run_id, agent_id, epoch, seq, _json({"action": "settle_requested"}), now_text),
            )
            connection.execute(
                """
                UPDATE agents
                SET status = 'SETTLING', last_seq = ?, activity = 'settle_requested',
                    updated_at = ?
                WHERE run_id = ? AND agent_id = ? AND status IN ('REGISTERED', 'RUNNING')
                """,
                (seq, now_text, run_id, agent_id),
            )

    def expired_agents(self, *, now: Optional[datetime] = None) -> Sequence[Mapping[str, Any]]:
        now_text = _iso(now or _utc_now())
        with self._connection(write=False) as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM agents
                WHERE status IN ('REGISTERED', 'RUNNING', 'SETTLING')
                  AND lease_until IS NOT NULL
                  AND lease_until < ?
                ORDER BY lease_until
                """,
                (now_text,),
            ).fetchall()
            return [self._agent_dict(row) for row in rows]

    def list_events(
        self,
        run_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 100,
    ) -> Tuple[Sequence[Mapping[str, Any]], int]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._connection(write=False) as connection:
            self._require_run(connection, run_id)
            rows = connection.execute(
                """
                SELECT *
                FROM events
                WHERE run_id = ? AND event_id > ?
                ORDER BY event_id
                LIMIT ?
                """,
                (run_id, after_event_id, limit),
            ).fetchall()
            items = [self._event_dict(row) for row in rows]
            return items, (items[-1]["event_id"] if items else after_event_id)

    def get_run(self, run_id: str) -> Mapping[str, Any]:
        with self._connection(write=False) as connection:
            return self._run_dict(self._require_run(connection, run_id))

    def get_agent(self, run_id: str, agent_id: str) -> Mapping[str, Any]:
        with self._connection(write=False) as connection:
            return self._agent_dict(self._require_agent(connection, run_id, agent_id))

    def close_run(
        self,
        run_id: str,
        *,
        status: str,
        summary: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if status not in TERMINAL_RUN_STATUSES:
            raise ProtocolError(f"invalid terminal run status: {status}")
        now_text = _iso(_utc_now())
        with self._connection(write=True) as connection:
            run = self._require_run(connection, run_id)
            if run["status"] in TERMINAL_RUN_STATUSES:
                if run["status"] != status:
                    raise ProtocolError("run is already closed with another status")
                return
            connection.execute(
                """
                UPDATE runs
                SET status = ?, summary_json = ?, closed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (status, _json(summary or {}), now_text, now_text, run_id),
            )
            connection.execute(
                """
                UPDATE agents
                SET status = CASE
                    WHEN status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'STALE') THEN status
                    ELSE 'CANCELLED'
                END,
                lease_until = NULL,
                updated_at = ?
                WHERE run_id = ?
                """,
                (now_text, run_id),
            )

    def cleanup(
        self,
        *,
        now: Optional[datetime] = None,
        event_retention: timedelta = timedelta(days=1),
        terminal_retention: timedelta = timedelta(days=30),
    ) -> Mapping[str, int]:
        current = now or _utc_now()
        event_cutoff = _iso(current - event_retention)
        terminal_cutoff = _iso(current - terminal_retention)
        with self._connection(write=True) as connection:
            events_deleted = connection.execute(
                """
                DELETE FROM events
                WHERE event_type IN ('heartbeat', 'progress')
                  AND created_at < ?
                """,
                (event_cutoff,),
            ).rowcount
            old_runs = connection.execute(
                """
                SELECT run_id
                FROM runs
                WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED', 'ABANDONED')
                  AND closed_at IS NOT NULL
                  AND closed_at < ?
                """,
                (terminal_cutoff,),
            ).fetchall()
            run_ids = [row["run_id"] for row in old_runs]
            for old_run_id in run_ids:
                connection.execute("DELETE FROM runs WHERE run_id = ?", (old_run_id,))
            result = {"events_deleted": events_deleted, "runs_deleted": len(run_ids)}
        if events_deleted or run_ids:
            with self._connection(write=False) as connection:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        return result

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> Mapping[str, Any]:
        item = dict(row)
        item["summary"] = _decode(item.pop("summary_json"), {})
        return item

    @staticmethod
    def _agent_dict(row: sqlite3.Row) -> Mapping[str, Any]:
        return dict(row)

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> Mapping[str, Any]:
        item = dict(row)
        item["payload"] = _decode(item.pop("payload_json"), {})
        return item

    @staticmethod
    def _require_run(connection: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"unknown run_id: {run_id}")
        return row

    @staticmethod
    def _require_agent(
        connection: sqlite3.Connection,
        run_id: str,
        agent_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM agents WHERE run_id = ? AND agent_id = ?",
            (run_id, agent_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown agent: {run_id}/{agent_id}")
        return row

    @staticmethod
    def _ensure_leader_agent(
        connection: sqlite3.Connection,
        run_id: str,
        now_text: str,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO agents(
                run_id, agent_id, role, epoch, status, updated_at
            ) VALUES(?, 'LEADER', 'orchestrator', 1, 'REGISTERED', ?)
            """,
            (run_id, now_text),
        )

    @staticmethod
    def _ensure_active_run(run: sqlite3.Row) -> None:
        if run["status"] in TERMINAL_RUN_STATUSES:
            raise ProtocolError(f"run is terminal: {run['status']}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=None, help="SQLite path override")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("state-path")

    create = subparsers.add_parser("create")
    create.add_argument("--repository", required=True)
    create.add_argument("--pr-number", type=int)
    create.add_argument("--base-sha")
    create.add_argument("--head-sha")
    create.add_argument("--skill-name", default="review-pr-with-panel")
    create.add_argument("--run-id")

    register = subparsers.add_parser("register-agent")
    register.add_argument("--run-id", required=True)
    register.add_argument("--agent-id", required=True)
    register.add_argument("--role", required=True)
    register.add_argument("--epoch", type=int, default=1)

    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("--run-id", required=True)
    heartbeat.add_argument("--agent-id", required=True)
    heartbeat.add_argument("--epoch", type=int, required=True)
    heartbeat.add_argument("--seq", type=int, required=True)
    heartbeat.add_argument("--phase", required=True)
    heartbeat.add_argument("--activity", required=True)
    heartbeat.add_argument("--lease-seconds", type=int, default=60)

    progress = subparsers.add_parser("progress")
    progress.add_argument("--run-id", required=True)
    progress.add_argument("--agent-id", required=True)
    progress.add_argument("--epoch", type=int, required=True)
    progress.add_argument("--seq", type=int, required=True)
    progress.add_argument("--phase", required=True)
    progress.add_argument("--message", required=True)

    observe_wait = subparsers.add_parser("observe-wait")
    observe_wait.add_argument("--run-id", required=True)
    observe_wait.add_argument("--target-agent", required=True)
    observe_wait.add_argument("--phase", required=True)
    observe_wait.add_argument("--wait-seconds", type=int, required=True)
    observe_wait.add_argument("--timed-out", action="store_true")

    packet = subparsers.add_parser("packet")
    packet.add_argument("--run-id", required=True)
    packet.add_argument("--agent-id", required=True)
    packet.add_argument("--epoch", type=int, required=True)
    packet.add_argument("--packet-type", required=True)
    packet.add_argument("--validation-status", required=True)
    packet.add_argument("--packet-json", required=True)

    events = subparsers.add_parser("events")
    events.add_argument("--run-id", required=True)
    events.add_argument("--after-event-id", type=int, default=0)
    events.add_argument("--limit", type=int, default=100)

    get = subparsers.add_parser("get")
    get.add_argument("--run-id", required=True)
    get.add_argument("--agent-id")

    close = subparsers.add_parser("close")
    close.add_argument("--run-id", required=True)
    close.add_argument("--status", choices=sorted(TERMINAL_RUN_STATUSES), required=True)
    close.add_argument("--summary-json", default="{}")

    cleanup = subparsers.add_parser("cleanup")
    cleanup.add_argument("--event-days", type=int, default=1)
    cleanup.add_argument("--terminal-days", type=int, default=30)
    return parser


def _output(value: Any) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "state-path":
        _output({"state_db": str(resolve_state_db(args.db))})
        return 0

    store = ReviewRuntimeStore(args.db)
    try:
        if args.command == "create":
            run_id = store.create_run(
                repository=args.repository,
                pr_number=args.pr_number,
                base_sha=args.base_sha,
                head_sha=args.head_sha,
                skill_name=args.skill_name,
                run_id=args.run_id,
            )
            _output({"run_id": run_id, "state_db": str(store.db_path)})
        elif args.command == "register-agent":
            _output(store.register_agent(args.run_id, args.agent_id, args.role, epoch=args.epoch))
        elif args.command == "heartbeat":
            _output(
                store.heartbeat(
                    run_id=args.run_id,
                    agent_id=args.agent_id,
                    epoch=args.epoch,
                    seq=args.seq,
                    phase=args.phase,
                    activity=args.activity,
                    lease_seconds=args.lease_seconds,
                )
            )
        elif args.command == "progress":
            _output(
                store.progress(
                    run_id=args.run_id,
                    agent_id=args.agent_id,
                    epoch=args.epoch,
                    seq=args.seq,
                    phase=args.phase,
                    message=args.message,
                )
            )
        elif args.command == "observe-wait":
            _output(
                store.record_wait_observation(
                    run_id=args.run_id,
                    target_agent_id=args.target_agent,
                    phase=args.phase,
                    wait_seconds=args.wait_seconds,
                    timed_out=args.timed_out,
                )
            )
        elif args.command == "packet":
            packet = json.loads(args.packet_json)
            packet_id = store.record_packet(
                run_id=args.run_id,
                agent_id=args.agent_id,
                epoch=args.epoch,
                packet_type=args.packet_type,
                packet=packet,
                validation_status=args.validation_status,
            )
            _output({"packet_id": packet_id})
        elif args.command == "events":
            items, cursor = store.list_events(
                args.run_id,
                after_event_id=args.after_event_id,
                limit=args.limit,
            )
            _output({"events": items, "cursor": cursor})
        elif args.command == "get":
            if args.agent_id:
                _output(store.get_agent(args.run_id, args.agent_id))
            else:
                _output(store.get_run(args.run_id))
        elif args.command == "close":
            store.close_run(
                args.run_id,
                status=args.status,
                summary=json.loads(args.summary_json),
            )
            _output({"run_id": args.run_id, "status": args.status})
        elif args.command == "cleanup":
            _output(
                store.cleanup(
                    event_retention=timedelta(days=args.event_days),
                    terminal_retention=timedelta(days=args.terminal_days),
                )
            )
        return 0
    except (KeyError, ProtocolError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.error("review runtime command failed: %s", exc)
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.ERROR)
    raise SystemExit(main())
