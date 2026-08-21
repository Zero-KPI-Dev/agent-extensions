from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.db import StateStore


class _TrackedConnection:
    def __init__(self, raw: sqlite3.Connection, closed: list[sqlite3.Connection]) -> None:
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_closed", closed)

    def __getattr__(self, name: str) -> object:
        return getattr(self._raw, name)

    def __setattr__(self, name: str, value: object) -> None:
        setattr(self._raw, name, value)

    def close(self) -> None:
        self._closed.append(self._raw)
        self._raw.close()


class StateStoreConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.db_path = Path(self.temp_dir.name) / "state.sqlite3"

    def test_frequent_cancel_polling_closes_every_connection(self) -> None:
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []
        closed: list[sqlite3.Connection] = []

        def tracked_connect(*args: object, **kwargs: object) -> _TrackedConnection:
            raw = real_connect(*args, **kwargs)
            opened.append(raw)
            return _TrackedConnection(raw, closed)

        with patch("server.db.sqlite3.connect", side_effect=tracked_connect):
            store = StateStore(self.db_path)
            store.initialize()
            for _ in range(500):
                self.assertFalse(store.is_cancel_requested("missing-job"))

        self.assertEqual(len(opened), 501)
        self.assertEqual(len(closed), len(opened))

    def test_connection_closes_when_database_operation_raises(self) -> None:
        real_connect = sqlite3.connect
        opened: list[sqlite3.Connection] = []
        closed: list[sqlite3.Connection] = []

        def tracked_connect(*args: object, **kwargs: object) -> _TrackedConnection:
            raw = real_connect(*args, **kwargs)
            opened.append(raw)
            return _TrackedConnection(raw, closed)

        with patch("server.db.sqlite3.connect", side_effect=tracked_connect):
            store = StateStore(self.db_path)
            with self.assertRaisesRegex(RuntimeError, "boom"):
                with store._connect():
                    raise RuntimeError("boom")

        self.assertEqual(len(opened), 1)
        self.assertEqual(len(closed), 1)


if __name__ == "__main__":
    unittest.main()
