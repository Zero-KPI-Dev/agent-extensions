from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


def _now() -> float:
    return time.time()


class StateStore:
    """Small durable queue shared by the HTTP gateway, worker, and MCP server."""

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self._init_lock = threading.Lock()
        self._initialized = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        """Open one short-lived connection and always release its file handles.

        ``sqlite3.Connection.__exit__`` only commits or rolls back; it doesn't
        close the connection.  Returning a raw connection here and using it as
        ``with self._connect()`` therefore left database and WAL descriptors
        alive until Python's cyclic garbage collector happened to run.  The
        review workers poll cancellation frequently, so those descriptors
        could reach launchd's soft limit before collection.
        """

        connection = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout = 30000")
            connection.execute("PRAGMA journal_mode = WAL")
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        event_id TEXT PRIMARY KEY,
                        received_at REAL NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS jobs (
                        job_id TEXT PRIMARY KEY,
                        event_id TEXT UNIQUE,
                        bot_key TEXT NOT NULL DEFAULT 'default',
                        chat_id TEXT,
                        sender_id TEXT,
                        message_id TEXT,
                        codex_thread_id TEXT,
                        request_text TEXT NOT NULL,
                        pr_url TEXT NOT NULL,
                        repo_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        attempt INTEGER NOT NULL DEFAULT 0,
                        pid INTEGER,
                        cancel_requested INTEGER NOT NULL DEFAULT 0,
                        ack_text TEXT,
                        result_text TEXT,
                        error_text TEXT,
                        delivery_status TEXT,
                        created_at REAL NOT NULL,
                        started_at REAL,
                        finished_at REAL,
                        updated_at REAL NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS jobs_status_created_idx
                        ON jobs(status, created_at);
                    CREATE INDEX IF NOT EXISTS jobs_updated_idx
                        ON jobs(updated_at);

                    CREATE TABLE IF NOT EXISTS job_subscribers (
                        subscriber_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        job_id TEXT NOT NULL,
                        event_id TEXT UNIQUE,
                        bot_key TEXT NOT NULL,
                        chat_id TEXT,
                        sender_id TEXT,
                        message_id TEXT,
                        created_at REAL NOT NULL,
                        FOREIGN KEY(job_id) REFERENCES jobs(job_id)
                    );

                    CREATE INDEX IF NOT EXISTS job_subscribers_job_idx
                        ON job_subscribers(job_id, created_at);
                    """
                )
                columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)").fetchall()}
                if "bot_key" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN bot_key TEXT NOT NULL DEFAULT 'default'")
                if "codex_thread_id" not in columns:
                    connection.execute("ALTER TABLE jobs ADD COLUMN codex_thread_id TEXT")
            self._initialized = True

    def record_event(self, event_id: str) -> bool:
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO events(event_id, received_at) VALUES(?, ?)",
                (event_id, _now()),
            )
            return cursor.rowcount == 1

    def create_job(
        self,
        *,
        event_id: str | None,
        bot_key: str = "default",
        chat_id: str | None,
        sender_id: str | None,
        message_id: str | None,
        request_text: str,
        pr_url: str,
        repo_key: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        self.initialize()
        now = _now()
        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, event_id, bot_key, chat_id, sender_id, message_id,
                    request_text, pr_url, repo_key, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    event_id,
                    bot_key,
                    chat_id,
                    sender_id,
                    message_id,
                    request_text,
                    pr_url,
                    repo_key,
                    status,
                    now,
                    now,
                ),
            )
        return self.get_job(job_id) or {}

    def create_or_get_active_job(
        self,
        *,
        event_id: str | None,
        bot_key: str = "default",
        chat_id: str | None,
        sender_id: str | None,
        message_id: str | None,
        request_text: str,
        pr_url: str,
        repo_key: str,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically coalesce duplicate active requests for the same PR.

        Completed jobs are intentionally ignored so an explicit later re-review
        can create a fresh job. Duplicate callers are retained as subscribers so
        a request from another chat still receives the shared final result.
        """
        self.initialize()
        now = _now()
        job_id = uuid.uuid4().hex
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT *
                FROM jobs
                WHERE status IN ('pending', 'running')
                  AND cancel_requested = 0
                  AND lower(rtrim(pr_url, '/')) = lower(rtrim(?, '/'))
                ORDER BY CASE status WHEN 'running' THEN 0 ELSE 1 END,
                         created_at ASC
                LIMIT 1
                """,
                (pr_url,),
            ).fetchone()
            if row:
                self._add_subscriber(
                    connection,
                    job_id=row["job_id"],
                    event_id=event_id,
                    bot_key=bot_key,
                    chat_id=chat_id,
                    sender_id=sender_id,
                    message_id=message_id,
                    created_at=now,
                )
                connection.execute("COMMIT")
                return dict(row), False

            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, event_id, bot_key, chat_id, sender_id, message_id,
                    request_text, pr_url, repo_key, status, created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    job_id,
                    event_id,
                    bot_key,
                    chat_id,
                    sender_id,
                    message_id,
                    request_text,
                    pr_url,
                    repo_key,
                    now,
                    now,
                ),
            )
            self._add_subscriber(
                connection,
                job_id=job_id,
                event_id=event_id,
                bot_key=bot_key,
                chat_id=chat_id,
                sender_id=sender_id,
                message_id=message_id,
                created_at=now,
            )
            connection.execute("COMMIT")
        return self.get_job(job_id) or {}, True

    @staticmethod
    def _add_subscriber(
        connection: sqlite3.Connection,
        *,
        job_id: str,
        event_id: str | None,
        bot_key: str,
        chat_id: str | None,
        sender_id: str | None,
        message_id: str | None,
        created_at: float,
    ) -> None:
        connection.execute(
            """
            INSERT OR IGNORE INTO job_subscribers(
                job_id, event_id, bot_key, chat_id, sender_id, message_id, created_at
            ) VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, event_id, bot_key, chat_id, sender_id, message_id, created_at),
        )

    def delivery_targets(self, job_id: str) -> list[dict[str, str]]:
        """Return distinct bot/chat destinations subscribed to a job."""
        self.initialize()
        with self._connect() as connection:
            job = connection.execute(
                "SELECT bot_key, chat_id FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            subscribers = connection.execute(
                """
                SELECT bot_key, chat_id
                FROM job_subscribers
                WHERE job_id = ? AND chat_id IS NOT NULL AND chat_id != ''
                ORDER BY created_at ASC
                """,
                (job_id,),
            ).fetchall()

        targets: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        candidates = ([job] if job else []) + list(subscribers)
        for row in candidates:
            bot_key = str(row["bot_key"] or "")
            chat_id = str(row["chat_id"] or "")
            key = (bot_key, chat_id)
            if not chat_id or key in seen:
                continue
            seen.add(key)
            targets.append({"bot_key": bot_key, "chat_id": chat_id})
        return targets

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return dict(row) if row else None

    def claim_next_job(self) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT pending.*
                FROM jobs AS pending
                WHERE pending.status = 'pending'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM jobs AS running
                      WHERE running.status = 'running'
                        AND lower(running.pr_url) = lower(pending.pr_url)
                  )
                ORDER BY pending.created_at ASC
                LIMIT 1
                """
            ).fetchone()
            if not row:
                connection.execute("COMMIT")
                return None
            now = _now()
            connection.execute(
                """
                UPDATE jobs
                SET status = 'running', attempt = attempt + 1,
                    started_at = ?, updated_at = ?, cancel_requested = 0
                WHERE job_id = ? AND status = 'pending'
                """,
                (now, now, row["job_id"]),
            )
            connection.execute("COMMIT")
        return self.get_job(row["job_id"])

    def set_pid(self, job_id: str, pid: int | None) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET pid = ?, updated_at = ? WHERE job_id = ?",
                (pid, _now(), job_id),
            )

    def set_codex_thread_id(self, job_id: str, thread_id: str) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET codex_thread_id = ?, updated_at = ? WHERE job_id = ?",
                (thread_id, _now(), job_id),
            )

    def request_cancel(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET cancel_requested = 1,
                    status = CASE WHEN status = 'pending' THEN 'cancelled' ELSE status END,
                    updated_at = ?, finished_at = CASE WHEN status = 'pending' THEN ? ELSE finished_at END
                WHERE job_id = ? AND status IN ('pending', 'running')
                """,
                (_now(), _now(), job_id),
            )
        return self.get_job(job_id)

    def is_cancel_requested(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return bool(job and job.get("cancel_requested"))

    def finish(
        self,
        job_id: str,
        *,
        status: str,
        result_text: str | None = None,
        error_text: str | None = None,
        delivery_status: str | None = None,
    ) -> None:
        self.initialize()
        now = _now()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, result_text = ?, error_text = ?,
                    delivery_status = ?, pid = NULL, finished_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, result_text, error_text, delivery_status, now, now, job_id),
            )

    def retry(self, job_id: str) -> dict[str, Any] | None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', cancel_requested = 0, pid = NULL,
                    error_text = NULL, result_text = NULL, delivery_status = NULL,
                    started_at = NULL, finished_at = NULL, updated_at = ?
                WHERE job_id = ? AND status IN ('failed', 'cancelled', 'succeeded')
                """,
                (_now(), job_id),
            )
        return self.get_job(job_id)

    def set_ack(self, job_id: str, ack_text: str) -> None:
        self.initialize()
        with self._connect() as connection:
            connection.execute(
                "UPDATE jobs SET ack_text = ?, updated_at = ? WHERE job_id = ?",
                (ack_text, _now(), job_id),
            )

    def list_jobs(self, *, limit: int = 20, status: str | None = None) -> list[dict[str, Any]]:
        self.initialize()
        limit = max(1, min(int(limit), 100))
        with self._connect() as connection:
            if status:
                rows = connection.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(row) for row in rows]

    def pending_count(self) -> int:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'pending'").fetchone()
        return int(row["count"] if row else 0)

    def running_count(self) -> int:
        self.initialize()
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM jobs WHERE status = 'running'").fetchone()
        return int(row["count"] if row else 0)

    def mark_stale_running_jobs_pending(self) -> int:
        """Recover jobs left running when launchd restarted the process."""
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', pid = NULL, updated_at = ?
                WHERE status = 'running' AND (pid IS NULL OR pid <= 0)
                """,
                (_now(),),
            )
            return cursor.rowcount

    def requeue_running_jobs(self) -> int:
        """Put interrupted jobs back in the durable queue after a daemon restart."""
        self.initialize()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs
                SET status = 'pending', pid = NULL, updated_at = ?
                WHERE status = 'running'
                """,
                (_now(),),
            )
            return cursor.rowcount

    @staticmethod
    def public_job(job: dict[str, Any] | None) -> dict[str, Any] | None:
        if not job:
            return None
        safe = dict(job)
        safe.pop("pid", None)
        safe.pop("cancel_requested", None)
        return safe

    @staticmethod
    def encode_payload(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
