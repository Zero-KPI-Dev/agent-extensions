from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.configure import (
    list_author_mappings,
    list_merge_maintainers,
    remove_author_mapping,
    remove_merge_maintainers,
    set_author_mapping,
    set_merge_maintainers,
)
from server.config import BotConfig
from server.feishu import (
    author_mention_text,
    build_review_card,
    is_merge_ready_conclusion,
    notification_mention_text,
    review_conclusion,
)
from server.gateway import ReviewWorker, resolve_github_pr_author, resolve_github_pr_metadata


def _bot(
    key: str,
    open_id: str,
    *,
    merge_maintainers: dict[str, tuple[str, ...]] | None = None,
) -> BotConfig:
    return BotConfig(
        key=key,
        display_name=key,
        event_path=f"/events/{key}",
        feishu_base_url="https://open.feishu.cn",
        app_id=f"app-{key}",
        app_secret="secret",
        verification_token="",
        bot_open_id=f"bot-{key}",
        author_mappings={"octocat": open_id},
        merge_maintainers=merge_maintainers or {},
    )


class AuthorMappingTests(unittest.TestCase):
    def test_mapping_is_case_insensitive_and_scoped_to_bot(self) -> None:
        first = _bot("first", "ou_first")
        second = _bot("second", "ou_second")

        self.assertEqual(first.author_open_id("@OctoCat"), "ou_first")
        self.assertEqual(second.author_open_id("octocat"), "ou_second")
        self.assertIsNone(first.author_open_id("someone-else"))

    def test_configure_set_list_and_remove_preserve_bot_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "default_bot": "review",
                        "bots": {
                            "review": {
                                "app_id": "app",
                                "app_secret": "secret",
                                "author_mappings": {},
                            }
                        },
                        "repo_roots": {},
                    }
                ),
                encoding="utf-8",
            )

            set_author_mapping(path, "review", "@OctoCat", "ou_author")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bots"]["review"]["author_mappings"], {"octocat": "ou_author"})
            self.assertEqual(saved["bots"]["review"]["app_secret"], "secret")

            with patch("builtins.print") as printer:
                list_author_mappings(path, "review")
            self.assertTrue(any("octocat -> ou_author" in str(call) for call in printer.call_args_list))

            remove_author_mapping(path, "review", "OCTOCAT")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bots"]["review"]["author_mappings"], {})

    def test_configure_merge_maintainers_preserves_bot_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "default_bot": "review",
                        "bots": {"review": {"app_secret": "secret", "merge_maintainers": {}}},
                    }
                ),
                encoding="utf-8",
            )

            set_merge_maintainers(path, "review", "*", ["ou_first", "ou_second", "ou_first"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bots"]["review"]["merge_maintainers"], {"*": ["ou_first", "ou_second"]})
            self.assertEqual(saved["bots"]["review"]["app_secret"], "secret")

            with patch("builtins.print") as printer:
                list_merge_maintainers(path, "review")
            self.assertTrue(any("* -> ou_first, ou_second" in str(call) for call in printer.call_args_list))

            remove_merge_maintainers(path, "review", "*")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["bots"]["review"]["merge_maintainers"], {})


class AuthorMentionCardTests(unittest.TestCase):
    report = """PR 检视完成
模式: INITIAL_REVIEW
结论: NO_ACTIONABLE_FINDINGS
发现数量: Critical 0 / High 0 / Medium 0 / Low 0 / Suggestion 0
"""

    def test_action_required_card_mentions_mapped_author(self) -> None:
        report = self.report.replace("NO_ACTIONABLE_FINDINGS", "ACTION_REQUIRED").replace(
            "Medium 0", "Medium 1"
        )
        card = build_review_card(
            report,
            pr_url="https://github.com/octo/repo/pull/1",
            mention_open_id="ou_author",
            github_author="octocat",
        )

        rendered = str(card)
        self.assertIn("<at id=ou_author></at>", rendered)
        self.assertIn("GitHub: `octocat`", rendered)
        self.assertIn("PR 检视已完成，请查收", rendered)

    def test_no_actionable_card_mentions_all_merge_maintainers(self) -> None:
        card = build_review_card(
            self.report,
            pr_url="https://github.com/octo/repo/pull/1",
            mention_open_ids=("ou_merger_one", "ou_merger_two"),
            mention_kind="merge_maintainers",
            github_author="octocat",
        )

        rendered = str(card)
        self.assertIn("<at id=ou_merger_one></at>", rendered)
        self.assertIn("<at id=ou_merger_two></at>", rendered)
        self.assertIn("PR 检视未发现待处理问题", rendered)
        self.assertIn("结合 required checks 最终状态评估合入", rendered)
        self.assertNotIn("GitHub: `octocat`", rendered)

    def test_invalid_open_id_does_not_inject_markup(self) -> None:
        card = build_review_card(self.report, mention_open_id='ou_bad">oops', github_author="octocat")
        self.assertNotIn("<at id=", str(card))
        self.assertEqual(author_mention_text("done", 'ou_bad">oops', "octocat"), "done")

    def test_plain_text_fallback_mentions_author(self) -> None:
        text = author_mention_text("检视摘要", "ou_author", "octocat")
        self.assertTrue(text.startswith('<at user_id="ou_author">octocat</at>'))

    def test_fix_verified_card_mentions_all_merge_maintainers(self) -> None:
        report = self.report.replace("NO_ACTIONABLE_FINDINGS", "FIX_VERIFIED").replace(
            "INITIAL_REVIEW", "FIX_VERIFICATION"
        )
        card = build_review_card(
            report,
            pr_url="https://github.com/octo/repo/pull/1",
            mention_open_ids=("ou_merger_one", "ou_merger_two"),
            mention_kind="merge_maintainers",
            github_author="octocat",
        )

        rendered = str(card)
        self.assertIn("<at id=ou_merger_one></at>", rendered)
        self.assertIn("<at id=ou_merger_two></at>", rendered)
        self.assertIn("修复已验证，当前无待处理检视意见", rendered)
        self.assertIn("结合 required checks 最终状态评估合入", rendered)
        self.assertNotIn("GitHub: `octocat`", rendered)

    def test_plain_text_fallback_mentions_all_merge_maintainers(self) -> None:
        text = notification_mention_text(
            "检视摘要",
            ["ou_merger_one", "ou_merger_two"],
            mention_kind="merge_maintainers",
        )
        self.assertIn('<at user_id="ou_merger_one">合入者</at>', text)
        self.assertIn('<at user_id="ou_merger_two">合入者</at>', text)
        self.assertIn("修复已验证，当前无待处理检视意见", text)
        self.assertIn("结合 required checks 最终状态评估合入", text)

    def test_no_actionable_plain_text_fallback_prompts_merge_review(self) -> None:
        text = notification_mention_text(
            self.report,
            ["ou_merger"],
            mention_kind="merge_maintainers",
        )

        self.assertIn('<at user_id="ou_merger">合入者</at>', text)
        self.assertIn("PR 检视未发现待处理问题", text)
        self.assertIn("结合 required checks 最终状态评估合入", text)

    def test_review_conclusion_normalizes_markdown_summary(self) -> None:
        self.assertEqual(review_conclusion("结论：`FIX_VERIFIED`。历史问题已修复。"), "FIX_VERIFIED")
        self.assertTrue(is_merge_ready_conclusion("NO_ACTIONABLE_FINDINGS"))
        self.assertFalse(is_merge_ready_conclusion("ACTION_REQUIRED"))


class GithubAuthorResolutionTests(unittest.TestCase):
    @patch("server.gateway.resolve_executable", return_value="/usr/local/bin/gh")
    @patch("server.gateway.subprocess.run")
    def test_gh_author_is_used_when_available(self, run: object, _resolve: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout='{"author":{"login":"octocat"}}',
            stderr="",
        )

        author = resolve_github_pr_author("https://github.com/octo/repo/pull/1", Path("/tmp"))

        self.assertEqual(author, "octocat")

    @patch("server.gateway.resolve_executable", return_value="/usr/local/bin/gh")
    @patch("server.gateway.subprocess.run")
    def test_live_pr_metadata_pins_base_and_head(self, run: object, _resolve: object) -> None:
        run.return_value = subprocess.CompletedProcess(
            args=[],
            returncode=0,
            stdout=json.dumps(
                {
                    "number": 542,
                    "title": "Current pull request",
                    "url": "https://github.com/octo/repo/pull/542",
                    "state": "OPEN",
                    "isDraft": False,
                    "author": {"login": "octocat"},
                    "baseRefName": "develop",
                    "baseRefOid": "a" * 40,
                    "headRefName": "feature/current",
                    "headRefOid": "b" * 40,
                }
            ),
            stderr="",
        )

        metadata = resolve_github_pr_metadata(
            "https://github.com/octo/repo/pull/542",
            Path("/tmp"),
        )

        self.assertIsNotNone(metadata)
        assert metadata is not None
        self.assertEqual(metadata.base_sha, "a" * 40)
        self.assertEqual(metadata.head_sha, "b" * 40)
        self.assertEqual(metadata.head_ref, "feature/current")


class _DeliveryConfig:
    feishu_result_format = "card"
    max_feishu_text_length = 3500

    def __init__(self) -> None:
        self.bots = {
            "first": _bot("first", "ou_first"),
            "second": _bot("second", "ou_second"),
        }

    def bot(self, key: str) -> BotConfig | None:
        return self.bots.get(key)

    @staticmethod
    def repo_key(_pr_url: str) -> str:
        return "octo/repo"


class _DeliveryStore:
    @staticmethod
    def delivery_targets(_job_id: str) -> list[dict[str, str]]:
        return [
            {"bot_key": "first", "chat_id": "chat-first"},
            {"bot_key": "second", "chat_id": "chat-second"},
        ]


class _DeliveryClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send_review_card(
        self,
        chat_id: str,
        _text: str,
        _max_length: int,
        *,
        pr_url: str | None = None,
        mention_open_id: str | None = None,
        mention_open_ids: list[str] | tuple[str, ...] | None = None,
        mention_kind: str = "author",
        github_author: str | None = None,
    ) -> None:
        self.calls.append(
            {
                "chat_id": chat_id,
                "pr_url": pr_url,
                "mention_open_id": mention_open_id,
                "mention_open_ids": mention_open_ids,
                "mention_kind": mention_kind,
                "github_author": github_author,
            }
        )


class MultiBotDeliveryTests(unittest.TestCase):
    def test_each_bot_uses_its_own_application_scoped_open_id(self) -> None:
        config = _DeliveryConfig()
        clients = {"first": _DeliveryClient(), "second": _DeliveryClient()}
        worker = ReviewWorker(
            config_provider=lambda: config,  # type: ignore[arg-type]
            client_provider=lambda key: clients.get(key),  # type: ignore[arg-type]
            store=_DeliveryStore(),  # type: ignore[arg-type]
            stop_event=threading.Event(),
        )

        delivery = worker._send_job(
            {"job_id": "job", "pr_url": "https://github.com/octo/repo/pull/1"},
            "review complete",
            github_author="octocat",
        )

        self.assertEqual(delivery, "sent_multiple")
        self.assertEqual(clients["first"].calls[0]["mention_open_ids"], ("ou_first",))
        self.assertEqual(clients["second"].calls[0]["mention_open_ids"], ("ou_second",))

    def test_fix_verified_uses_merge_maintainers_instead_of_author(self) -> None:
        config = _DeliveryConfig()
        config.bots["first"] = _bot(
            "first",
            "ou_author_first",
            merge_maintainers={"octo/repo": ("ou_merger_one", "ou_merger_two")},
        )
        config.bots["second"] = _bot(
            "second",
            "ou_author_second",
            merge_maintainers={"*": ("ou_other_merger",)},
        )
        clients = {"first": _DeliveryClient(), "second": _DeliveryClient()}
        worker = ReviewWorker(
            config_provider=lambda: config,  # type: ignore[arg-type]
            client_provider=lambda key: clients.get(key),  # type: ignore[arg-type]
            store=_DeliveryStore(),  # type: ignore[arg-type]
            stop_event=threading.Event(),
        )

        delivery = worker._send_job(
            {"job_id": "job", "pr_url": "https://github.com/octo/repo/pull/1"},
            "模式：FIX_VERIFICATION\n结论：FIX_VERIFIED",
            github_author="octocat",
        )

        self.assertEqual(delivery, "sent_multiple")
        self.assertEqual(
            clients["first"].calls[0]["mention_open_ids"],
            ("ou_merger_one", "ou_merger_two"),
        )
        self.assertEqual(clients["first"].calls[0]["mention_kind"], "merge_maintainers")
        self.assertEqual(clients["second"].calls[0]["mention_open_ids"], ("ou_other_merger",))

    def test_no_actionable_findings_uses_merge_maintainers_instead_of_author(self) -> None:
        config = _DeliveryConfig()
        config.bots["first"] = _bot(
            "first",
            "ou_author_first",
            merge_maintainers={"octo/repo": ("ou_merger_one", "ou_merger_two")},
        )
        config.bots["second"] = _bot(
            "second",
            "ou_author_second",
            merge_maintainers={"*": ("ou_other_merger",)},
        )
        clients = {"first": _DeliveryClient(), "second": _DeliveryClient()}
        worker = ReviewWorker(
            config_provider=lambda: config,  # type: ignore[arg-type]
            client_provider=lambda key: clients.get(key),  # type: ignore[arg-type]
            store=_DeliveryStore(),  # type: ignore[arg-type]
            stop_event=threading.Event(),
        )

        delivery = worker._send_job(
            {"job_id": "job", "pr_url": "https://github.com/octo/repo/pull/1"},
            "模式：INITIAL_REVIEW\n结论：NO_ACTIONABLE_FINDINGS",
            github_author="octocat",
        )

        self.assertEqual(delivery, "sent_multiple")
        self.assertEqual(
            clients["first"].calls[0]["mention_open_ids"],
            ("ou_merger_one", "ou_merger_two"),
        )
        self.assertEqual(clients["first"].calls[0]["mention_kind"], "merge_maintainers")
        self.assertEqual(clients["second"].calls[0]["mention_open_ids"], ("ou_other_merger",))

    def test_merge_ready_result_without_maintainers_does_not_fall_back_to_author(self) -> None:
        config = _DeliveryConfig()
        clients = {"first": _DeliveryClient(), "second": _DeliveryClient()}
        worker = ReviewWorker(
            config_provider=lambda: config,  # type: ignore[arg-type]
            client_provider=lambda key: clients.get(key),  # type: ignore[arg-type]
            store=_DeliveryStore(),  # type: ignore[arg-type]
            stop_event=threading.Event(),
        )

        delivery = worker._send_job(
            {"job_id": "job", "pr_url": "https://github.com/octo/repo/pull/1"},
            "模式：INITIAL_REVIEW\n结论：NO_ACTIONABLE_FINDINGS",
            github_author="octocat",
        )

        self.assertEqual(delivery, "sent_multiple")
        self.assertEqual(clients["first"].calls[0]["mention_open_ids"], ())
        self.assertEqual(clients["second"].calls[0]["mention_open_ids"], ())


if __name__ == "__main__":
    unittest.main()
