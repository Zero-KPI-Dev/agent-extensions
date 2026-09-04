from __future__ import annotations

import asyncio
import threading
import unittest
from types import SimpleNamespace

from server.config import BotConfig
from server.gateway import Gateway
from server.long_connection import LongConnectionManager


def _bot(*, app_secret: str = "secret", author_open_id: str = "ou_author") -> BotConfig:
    return BotConfig(
        key="review",
        display_name="review",
        event_path="/events/review",
        feishu_base_url="https://open.feishu.cn",
        app_id="app",
        app_secret=app_secret,
        verification_token="",
        bot_open_id="bot",
        author_mappings={"octocat": author_open_id},
        merge_maintainers={"*": ("ou_merger",)},
    )


class _CountStore:
    @staticmethod
    def pending_count() -> int:
        return 0

    @staticmethod
    def running_count() -> int:
        return 0


class _Gateway:
    def __init__(self) -> None:
        self.store = _CountStore()
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class LongConnectionHealthTests(unittest.TestCase):
    def test_transport_open_rejects_sticky_ready_closed_socket(self) -> None:
        open_channel = SimpleNamespace(
            ws_client=SimpleNamespace(_conn=SimpleNamespace(closed=False, state=SimpleNamespace(name="OPEN")))
        )
        closed_channel = SimpleNamespace(
            ws_client=SimpleNamespace(_conn=SimpleNamespace(closed=False, state=SimpleNamespace(name="CLOSED")))
        )

        self.assertTrue(LongConnectionManager._channel_transport_open(open_channel))
        self.assertFalse(LongConnectionManager._channel_transport_open(closed_channel))
        self.assertFalse(LongConnectionManager._channel_transport_open(SimpleNamespace(ws_client=None)))

    def test_idle_restart_requests_gateway_shutdown(self) -> None:
        gateway = _Gateway()
        manager = LongConnectionManager(gateway)

        asyncio.run(manager._restart_gateway_when_idle("stale"))

        self.assertTrue(gateway.stopped)
        self.assertEqual(manager.public_status()["state"], "restart_required")


class ConnectionConfigSignatureTests(unittest.TestCase):
    def test_delivery_mapping_changes_do_not_rebuild_websocket(self) -> None:
        gateway = object.__new__(Gateway)
        config = SimpleNamespace(bots={"review": _bot(author_open_id="ou_first")})
        gateway.current_config = lambda: config  # type: ignore[method-assign]
        first = Gateway.config_signature(gateway)

        config.bots["review"] = _bot(author_open_id="ou_second")
        second = Gateway.config_signature(gateway)

        self.assertEqual(first, second)

    def test_connection_credential_change_rebuilds_websocket(self) -> None:
        gateway = object.__new__(Gateway)
        config = SimpleNamespace(bots={"review": _bot(app_secret="first")})
        gateway.current_config = lambda: config  # type: ignore[method-assign]
        first = Gateway.config_signature(gateway)

        config.bots["review"] = _bot(app_secret="second")
        second = Gateway.config_signature(gateway)

        self.assertNotEqual(first, second)


if __name__ == "__main__":
    unittest.main()
