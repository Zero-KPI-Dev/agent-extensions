from __future__ import annotations

import unittest

from server.config import BotConfig
from server.feishu import FeishuEvent, build_help_card, build_help_text, is_help_request
from server.gateway import Gateway


class _HelpStore:
    def record_event(self, _event_id: str) -> bool:
        return True

    def create_or_get_active_job(self, **_kwargs: object) -> object:
        raise AssertionError("help must not create a review job")


class _HelpConfig:
    repo_roots = {"tech-innovation-group/echomem": "/tmp/EchoMem"}

    @staticmethod
    def default_repo_key() -> str:
        return "tech-innovation-group/echomem"


class HelpIntentTests(unittest.TestCase):
    def test_bare_mention_and_cli_aliases_request_help(self) -> None:
        for text in ("@_user_1", "@_user_1 help", "@_user_1 --help", "@_user_1 帮助", "@_user_1 怎么用"):
            with self.subTest(text=text):
                self.assertTrue(is_help_request(text))

    def test_natural_language_usage_questions_request_help(self) -> None:
        for text in (
            "这个机器人如何使用？",
            "这个机器人怎么用？",
            "你能做什么",
            "有哪些功能",
            "有哪些可用指令",
            "我要怎么触发检视",
        ):
            with self.subTest(text=text):
                self.assertTrue(is_help_request(text))

    def test_pr_target_takes_priority_over_help_wording(self) -> None:
        self.assertFalse(is_help_request("@_user_1 帮助我检视 #340"))
        self.assertFalse(
            is_help_request("怎么用这个链接检视 https://github.com/tech-innovation-group/EchoMem/pull/340")
        )

    def test_unrelated_message_is_not_explicit_help(self) -> None:
        self.assertFalse(is_help_request("@_user_1 今天天气怎么样"))

    def test_gateway_help_route_does_not_create_review_job(self) -> None:
        gateway = Gateway.__new__(Gateway)
        gateway.store = _HelpStore()
        gateway.current_config = lambda: _HelpConfig()
        gateway._send_help = lambda _bot, _chat_id, notice=None: "sent_card"
        bot = BotConfig(
            key="pr-review",
            display_name="PR Review",
            event_path="/events",
            feishu_base_url="https://open.feishu.cn",
            app_id="app",
            app_secret="secret",
            verification_token="",
            bot_open_id="bot",
        )
        event = FeishuEvent(
            event_id="event-help",
            event_type="im.message.receive_v1",
            chat_id="chat",
            message_id="message",
            sender_id="sender",
            text="@_user_1 怎么用",
            mentioned_bot=True,
        )

        status, result = gateway.enqueue_event(bot, event)

        self.assertEqual(status, 200)
        self.assertTrue(result["help"])
        self.assertEqual(result["delivery"], "sent_card")


class HelpCardTests(unittest.TestCase):
    def test_default_repo_card_shows_shortcuts_and_behavior(self) -> None:
        card = build_help_card(
            default_repo="tech-innovation-group/echomem",
            configured_repos=["tech-innovation-group/echomem"],
        )

        self.assertEqual(card["header"]["template"], "blue")
        self.assertEqual(card["header"]["title"]["content"], "PR 检视机器人帮助")
        content = str(card)
        self.assertIn("tech-innovation-group/echomem", content)
        self.assertIn("检视 #314", content)
        self.assertIn("Leader + A/B 独立复核", content)
        self.assertIn("help", content)

    def test_multiple_repos_without_default_require_full_url(self) -> None:
        card = build_help_card(default_repo=None, configured_repos=["org/a", "org/b"])
        self.assertIn("多个仓库但没有默认仓库", str(card))
        self.assertIn("完整 GitHub PR 链接", str(card))

    def test_notice_is_in_card_and_text_fallback(self) -> None:
        notice = "没有识别到 PR 链接或编号。"
        card = build_help_card(default_repo="org/repo", configured_repos=["org/repo"], notice=notice)
        text = build_help_text(default_repo="org/repo", configured_repos=["org/repo"], notice=notice)
        self.assertIn(notice, str(card))
        self.assertIn(notice, text)


if __name__ == "__main__":
    unittest.main()
