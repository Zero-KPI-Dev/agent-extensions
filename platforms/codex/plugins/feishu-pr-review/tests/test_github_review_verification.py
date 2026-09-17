from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from server.feishu import build_review_card, review_incomplete_reason
from server.github_review_verification import verify_reported_publication


PR_URL = "https://github.com/tech-innovation-group/EchoMem/pull/542"
REVIEW_ID = "R-20260915-074340-2ea8368c1a47"
REPORT = f"""- **PR**：{PR_URL}
- **review_id**：`{REVIEW_ID}`
- **mode**：INITIAL_REVIEW
- **结论**：ACTION_REQUIRED
- **当前待处理发现数量**：Critical 0 / High 1 / Medium 0 / Low 0
- **GitHub 发布状态**：PUBLISHED，COMMENT review。
"""


class GitHubReviewVerificationTests(unittest.TestCase):
    def test_claimed_publication_requires_matching_comment_review_on_same_pr(self) -> None:
        url = PR_URL + "#pullrequestreview-5208746484"
        payload = json.dumps(
            {"id": 5208746484, "html_url": url, "body": f"review_id: {REVIEW_ID}", "state": "COMMENTED"}
        )
        with patch("server.github_review_verification.resolve_executable", return_value="/usr/bin/gh"), patch(
            "server.github_review_verification.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=payload + "\n", stderr=""),
        ):
            check = verify_reported_publication(REPORT, PR_URL)
        self.assertIsNotNone(check)
        self.assertTrue(check.confirmed)
        self.assertEqual(check.url, url)

    def test_other_pr_review_does_not_confirm_publication(self) -> None:
        payload = json.dumps(
            {
                "id": 5208746484,
                "html_url": "https://github.com/tech-innovation-group/EchoMem/pull/541#pullrequestreview-5208746484",
                "body": f"review_id: {REVIEW_ID}",
                "state": "COMMENTED",
            }
        )
        with patch("server.github_review_verification.resolve_executable", return_value="/usr/bin/gh"), patch(
            "server.github_review_verification.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=payload + "\n", stderr=""),
        ):
            check = verify_reported_publication(REPORT, PR_URL)
        self.assertIsNotNone(check)
        self.assertFalse(check.confirmed)

    def test_prior_review_id_is_not_mistaken_for_this_runs_publication(self) -> None:
        payload = json.dumps(
            {
                "id": 5208746484,
                "html_url": PR_URL + "#pullrequestreview-5208746484",
                "body": f"review_id: R-another-run\nprior_review_id: {REVIEW_ID}",
                "state": "COMMENTED",
            }
        )
        with patch("server.github_review_verification.resolve_executable", return_value="/usr/bin/gh"), patch(
            "server.github_review_verification.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=payload + "\n", stderr=""),
        ):
            check = verify_reported_publication(REPORT, PR_URL)
        self.assertIsNotNone(check)
        self.assertFalse(check.confirmed)

    def test_unverified_claim_is_not_presented_as_published(self) -> None:
        amended = REPORT + "\n- **GitHub 发布核验**：待确认。目标 PR 未找到本轮 review_id。"
        self.assertIsNotNone(review_incomplete_reason(amended))
        card = build_review_card(amended, pr_url=PR_URL)
        self.assertIn("发布未确认", card["header"]["title"]["content"])
        self.assertTrue(
            any("**状态**　发布未确认" in element.get("text", {}).get("content", "") for element in card["elements"])
        )

    def test_skip_without_existing_review_link_is_unconfirmed(self) -> None:
        skipped = REPORT.replace("PUBLISHED", "SKIPPED").replace("INITIAL_REVIEW", "NO_NEW_REVISION")
        with patch("server.github_review_verification.subprocess.run") as run:
            check = verify_reported_publication(skipped, PR_URL)
        self.assertIsNotNone(check)
        self.assertFalse(check.confirmed)
        run.assert_not_called()

    def test_existing_comment_review_confirms_no_new_revision_skip(self) -> None:
        url = PR_URL + "#pullrequestreview-5208746484"
        skipped = REPORT.replace("PUBLISHED", "SKIPPED").replace("INITIAL_REVIEW", "NO_NEW_REVISION") + f"\n- 既有 review：{url}"
        payload = json.dumps({"html_url": url, "state": "COMMENTED"})
        with patch("server.github_review_verification.resolve_executable", return_value="/usr/bin/gh"), patch(
            "server.github_review_verification.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=payload, stderr=""),
        ):
            check = verify_reported_publication(skipped, PR_URL)
        self.assertIsNotNone(check)
        self.assertTrue(check.confirmed)
        self.assertIn("本轮没有新发布", check.detail)


if __name__ == "__main__":
    unittest.main()
