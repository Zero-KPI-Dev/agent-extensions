from __future__ import annotations

import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Any

from .config import BotConfig
from .feishu import FeishuEvent


LOGGER = logging.getLogger("feishu-pr-review.long-connection")

HEALTH_CHECK_INTERVAL_SECONDS = 5.0
CONNECT_TIMEOUT_SECONDS = 120.0
STALE_CONNECTION_SECONDS = 60.0


class LongConnectionManager:
    """Own one Feishu WebSocket connection per configured long-connection bot."""

    def __init__(self, gateway: Any):
        self.gateway = gateway
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._status_lock = threading.Lock()
        self._status: dict[str, Any] = {
            "state": "stopped",
            "available": None,
            "bots": [],
            "error": None,
            "last_event_at": None,
            "last_connected_at": None,
        }

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="feishu-long-connection", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        loop = self._loop
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        self._set_status(state="stopped")

    def public_status(self) -> dict[str, Any]:
        with self._status_lock:
            return dict(self._status)

    def _set_status(self, **updates: Any) -> None:
        with self._status_lock:
            self._status.update(updates)

    def _run(self) -> None:
        try:
            import lark_channel  # type: ignore[import-not-found]
            del lark_channel
        except ImportError as exc:
            requirements = Path(__file__).resolve().parents[1] / "requirements-long-connection.txt"
            message = (
                "缺少飞书长连接依赖，请执行："
                f"python3 -m pip install -r {requirements}"
            )
            LOGGER.error("%s (%s)", message, exc)
            self._set_status(state="unavailable", available=False, error=message)
            return

        self._set_status(available=True, state="starting", error=None)
        try:
            asyncio.run(self._run_async())
        except Exception as exc:  # noqa: BLE001 - launchd must keep the gateway observable
            LOGGER.exception("long connection manager stopped unexpectedly")
            self._set_status(state="error", error=str(exc))
        finally:
            self._loop = None

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        while not self._stop_event.is_set():
            config = self.gateway.current_config()
            configured_bots = [
                bot for bot in config.bots.values() if bot.enabled and bot.transport == "long_connection"
            ]
            bots = [bot for bot in configured_bots if bot.app_id and bot.app_secret]
            if not bots:
                state = "waiting_for_configuration" if configured_bots else "idle"
                error = "请先配置长连接机器人的 App ID 和 App Secret" if configured_bots else None
                self._set_status(state=state, bots=[bot.key for bot in configured_bots], error=error)
                await asyncio.sleep(2)
                continue
            try:
                await self._run_generation(bots)
            except Exception as exc:  # noqa: BLE001 - reconnect after transient SDK errors
                LOGGER.exception("long connection generation failed")
                self._set_status(state="error", bots=[bot.key for bot in bots], error=str(exc))
                await asyncio.sleep(5)

    async def _run_generation(self, bots: list[BotConfig]) -> None:
        from lark_channel import FeishuChannel, PolicyConfig  # type: ignore[import-not-found]

        channels: list[Any] = []
        connection_tasks: list[asyncio.Task[Any]] = []
        watcher: asyncio.Task[Any] | None = None
        initial_signature = self.gateway.config_signature()
        try:
            for bot in bots:
                channel = FeishuChannel(
                    app_id=bot.app_id,
                    app_secret=bot.app_secret,
                    transport="ws",
                    policy=PolicyConfig(require_mention=bot.require_mention),
                )

                async def on_message(message: Any, current_bot: BotConfig = bot) -> None:
                    event = self._event_from_message(current_bot, message)
                    if event is not None:
                        self._set_status(last_event_at=time.time())
                        await asyncio.to_thread(self.gateway.enqueue_event, current_bot, event)

                def on_error(error: Any, current_bot: BotConfig = bot) -> None:
                    LOGGER.error("机器人 %s 长连接错误：%s", current_bot.key, error)

                def on_reconnecting(current_bot: BotConfig = bot) -> None:
                    LOGGER.warning("机器人 %s 长连接正在重连", current_bot.key)
                    self._set_status(state="reconnecting", error=None)

                def on_reconnected(current_bot: BotConfig = bot) -> None:
                    LOGGER.info("机器人 %s 长连接已恢复", current_bot.key)
                    self._set_status(state="running", error=None, last_connected_at=time.time())

                channel.on("message", on_message)
                channel.on("error", on_error)
                channel.on("reconnecting", on_reconnecting)
                channel.on("reconnected", on_reconnected)
                channels.append(channel)
                connection_tasks.append(asyncio.create_task(channel.connect()))

            self._set_status(state="connecting", bots=[bot.key for bot in bots], error=None)
            watcher = asyncio.create_task(self._watch_generation(channels, initial_signature))
            done, _ = await asyncio.wait(
                [*connection_tasks, watcher],
                return_when=asyncio.FIRST_COMPLETED,
            )
            unexpected_exit = False
            for task in done:
                if task.cancelled():
                    continue
                error = task.exception()
                if error:
                    LOGGER.error("长连接任务退出：%s", error)
                if task is not watcher:
                    unexpected_exit = True
            if unexpected_exit and not self._stop_event.is_set():
                await self._restart_gateway_when_idle("飞书长连接任务意外退出")
        finally:
            if watcher and not watcher.done():
                watcher.cancel()
            if watcher:
                await asyncio.gather(watcher, return_exceptions=True)
            for task in connection_tasks:
                if not task.done():
                    task.cancel()
            if connection_tasks:
                await asyncio.gather(*connection_tasks, return_exceptions=True)
            await self._disconnect_channels(channels)
            if not self._stop_event.is_set():
                self._set_status(state="reconnecting", bots=[bot.key for bot in bots])

    async def _watch_generation(
        self,
        channels: list[Any],
        initial_signature: tuple[tuple[Any, ...], ...],
    ) -> None:
        unhealthy_since: float | None = None
        connected_once = False
        generation_started_at = time.monotonic()
        while not self._stop_event.is_set():
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)
            self.gateway.current_config()
            if self.gateway.config_signature() != initial_signature:
                await self._restart_gateway_when_idle("飞书连接配置已变化")
                return

            all_open = bool(channels) and all(self._channel_transport_open(channel) for channel in channels)
            if all_open:
                if not connected_once:
                    LOGGER.info("飞书长连接传输已就绪")
                connected_once = True
                unhealthy_since = None
                self._set_status(state="running", error=None, last_connected_at=time.time())
                continue

            if not connected_once:
                self._set_status(state="connecting")
                if time.monotonic() - generation_started_at >= CONNECT_TIMEOUT_SECONDS:
                    await self._restart_gateway_when_idle("飞书 WebSocket 在启动超时内未连接")
                    return
                continue

            if unhealthy_since is None:
                unhealthy_since = time.monotonic()
                self._set_status(state="reconnecting", error="WebSocket transport is not open")
                continue
            if time.monotonic() - unhealthy_since >= STALE_CONNECTION_SECONDS:
                await self._restart_gateway_when_idle("飞书 WebSocket 持续不可用，准备由 launchd 重启网关")
                return

    async def _restart_gateway_when_idle(self, reason: str) -> None:
        LOGGER.error("%s", reason)
        self._set_status(state="restart_required", error=reason)
        while not self._stop_event.is_set():
            if self.gateway.store.pending_count() == 0 and self.gateway.store.running_count() == 0:
                LOGGER.warning("队列已空，停止网关以触发 launchd 自动恢复长连接")
                self.gateway.stop()
                return
            await asyncio.sleep(HEALTH_CHECK_INTERVAL_SECONDS)

    @staticmethod
    def _channel_transport_open(channel: Any) -> bool:
        """Inspect the SDK transport because its public ready flag is sticky."""

        ws_client = getattr(channel, "ws_client", None)
        connection = getattr(ws_client, "_conn", None) if ws_client is not None else None
        if connection is None:
            return False
        if getattr(connection, "closed", False) is True:
            return False
        state = getattr(connection, "state", None)
        if state is None:
            return True
        state_name = str(getattr(state, "name", state)).upper()
        return state_name == "OPEN" or state_name == "1"

    async def _disconnect_channels(self, channels: list[Any]) -> None:
        for channel in channels:
            try:
                await asyncio.wait_for(channel.disconnect(), timeout=5)
            except Exception as exc:  # noqa: BLE001 - best-effort shutdown
                LOGGER.debug("长连接关闭异常：%s", exc)

    @staticmethod
    def _event_from_message(bot: BotConfig, message: Any) -> FeishuEvent | None:
        event_id = str(getattr(message, "message_id", None) or getattr(message, "id", None) or "")
        conversation = getattr(message, "conversation", None)
        chat_id = str(getattr(message, "chat_id", None) or getattr(conversation, "chat_id", None) or "")
        sender = getattr(message, "sender", None)
        sender_id = str(getattr(message, "sender_id", None) or getattr(sender, "open_id", None) or "")
        text = str(getattr(message, "body_text", None) or getattr(message, "content_text", None) or "").strip()
        if not event_id or not chat_id or not text:
            return None

        mentioned_bot = bool(getattr(message, "mentioned_bot", False))
        for mention in getattr(message, "mentions", None) or []:
            if getattr(mention, "open_id", None) == bot.bot_open_id:
                mentioned_bot = True
                break
        return FeishuEvent(
            event_id=event_id,
            event_type="im.message.receive_v1",
            chat_id=chat_id,
            message_id=event_id,
            sender_id=sender_id,
            text=text,
            mentioned_bot=mentioned_bot,
        )
