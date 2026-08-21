from __future__ import annotations

import sys
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from pr_session_title import session_title_from_pr_url  # noqa: E402


class PrSessionTitleTests(unittest.TestCase):
    def test_uses_repository_name_and_pr_number(self) -> None:
        self.assertEqual(
            session_title_from_pr_url(
                "https://github.com/tech-innovation-group/EchoMem/pull/228"
            ),
            "EchoMem#228",
        )

    def test_accepts_trailing_path_query_and_fragment(self) -> None:
        self.assertEqual(
            session_title_from_pr_url(
                "https://github.com/org/repo/pull/7/files?plain=1#diff"
            ),
            "repo#7",
        )

    def test_rejects_non_pull_urls(self) -> None:
        with self.assertRaises(ValueError):
            session_title_from_pr_url("https://github.com/org/repo/issues/7")

    def test_rejects_non_github_hosts_and_invalid_numbers(self) -> None:
        with self.assertRaises(ValueError):
            session_title_from_pr_url("https://gitlab.com/org/repo/-/merge_requests/7")
        with self.assertRaises(ValueError):
            session_title_from_pr_url("https://github.com/org/repo/pull/not-a-number")


if __name__ == "__main__":
    unittest.main()
