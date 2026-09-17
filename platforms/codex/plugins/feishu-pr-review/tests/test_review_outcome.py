from __future__ import annotations

import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from server.feishu import review_incomplete_reason
from server.gateway import ReviewWorker
from server.github_review_verification import PublishedReviewCheck


class ReviewOutcomeTests(unittest.TestCase):
    def test_target_resolution_failure_is_not_a_successful_review(self) -> None:
        report = """- **结论**：目标解析失败。本地缺少精确 head 对象 `b27ca90dae6280c487d7bb0827fffd71bba3dd1b`。
- **当前待处理发现数量**：Critical：未核验；High：未核验；Medium：未核验。
- **主要发现摘要**：本轮未进入 diff 检视及 A/B 验证。
- **GitHub 发布状态**：未发布。
"""
        self.assertIsNotNone(review_incomplete_reason(report))

        store = Mock()
        worker = ReviewWorker(lambda: Mock(), lambda _key: None, store, threading.Event())
        with patch.object(worker, "_send_job", return_value="sent_card") as send, patch.object(
            worker, "_github_author_for_delivery"
        ) as author:
            delivery = worker._finish_report({"job_id": "example"}, report, Path("/tmp/repo"))
        self.assertEqual(delivery, "sent_card")
        self.assertEqual(store.finish.call_args.kwargs["status"], "failed")
        self.assertIn("没有产生新的检视结论", store.finish.call_args.kwargs["error_text"])
        send.assert_called_once()
        author.assert_not_called()

    def test_no_new_revision_is_a_successful_deduplicated_request(self) -> None:
        report = """- **mode**：NO_NEW_REVISION
- **结论**：NO_NEW_REVISION。当前 head 与上轮相同。
- **当前待处理发现数量**：Critical 0 / High 0 / Medium 1 / Low 0
- **GitHub 发布状态**：SKIPPED。同一 head 的 finding 已发布于上一轮 COMMENT review。
"""
        self.assertIsNone(review_incomplete_reason(report))

    def test_explicit_github_publish_failure_is_reported(self) -> None:
        report = """- **结论**：ACTION_REQUIRED
- **当前待处理发现数量**：Critical 0 / High 1 / Medium 0 / Low 0
- **GitHub 发布状态**：FAILED。权限不足，未创建 COMMENT review。
"""
        self.assertEqual(review_incomplete_reason(report), "代码检视已完成，但 GitHub 评论发布失败")

    def test_actionable_review_without_publication_confirmation_is_failed(self) -> None:
        report = """- **mode**：INITIAL_REVIEW
- **结论**：ACTION_REQUIRED
- **当前待处理发现数量**：Critical 0 / High 1 / Medium 0 / Low 0
- **GitHub 发布状态**：未说明
"""
        self.assertEqual(
            review_incomplete_reason(report),
            "检视发现待处理问题，但没有可确认的 GitHub 评论发布结果",
        )

    def test_claimed_publication_is_marked_failed_when_github_cannot_confirm_it(self) -> None:
        report = """- **review_id**：R-20260915-074340-2ea8368c1a47
- **mode**：INITIAL_REVIEW
- **结论**：ACTION_REQUIRED
- **当前待处理发现数量**：Critical 0 / High 1 / Medium 0 / Low 0
- **GitHub 发布状态**：PUBLISHED
"""
        store = Mock()
        worker = ReviewWorker(lambda: Mock(), lambda _key: None, store, threading.Event())
        with patch("server.gateway.verify_reported_publication", return_value=PublishedReviewCheck(False, "未找到本轮 review")), patch.object(
            worker, "_send_job", return_value="sent_card"
        ) as send, patch.object(worker, "_github_author_for_delivery") as author:
            worker._finish_report({"job_id": "example", "pr_url": "https://github.com/a/b/pull/1"}, report, Path("/tmp/repo"))
        self.assertEqual(store.finish.call_args.kwargs["status"], "failed")
        self.assertIn("GitHub 上无法确认", store.finish.call_args.kwargs["error_text"])
        self.assertIn("GitHub 发布核验", send.call_args.args[1])
        author.assert_not_called()


if __name__ == "__main__":
    unittest.main()
