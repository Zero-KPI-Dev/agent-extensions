from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.review_checkout import ReviewCheckoutError, prepare_review_checkout


class ReviewCheckoutTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pr = SimpleNamespace(
            owner="tech-innovation-group",
            repository="EchoMem",
            number=514,
            base_ref="develop",
            base_sha="a" * 40,
            head_sha="b" * 40,
        )

    def test_existing_exact_commits_keep_the_configured_repo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("server.review_checkout._has_exact_scope", return_value=True), patch(
                "server.review_checkout._run"
            ) as run:
                chosen = prepare_review_checkout(self.pr, root / "configured", root / "cache")
            self.assertEqual(chosen, root / "configured")
            run.assert_not_called()

    def test_missing_commit_uses_isolated_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch("server.review_checkout._has_exact_scope", side_effect=[False, True]), patch(
                "server.review_checkout._has_commit", return_value=True
            ), patch("server.review_checkout._checkout_is_at_head", return_value=True), patch(
                "server.review_checkout._run"
            ) as run:
                chosen = prepare_review_checkout(self.pr, root / "configured", root / "cache")
            self.assertEqual(chosen.name, "repo")
            self.assertTrue(str(chosen).startswith(str(root / "cache")))
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args_list[0].args[0][:3], ["gh", "repo", "clone"])
            self.assertEqual(run.call_args_list[1].args[0][-1], "refs/pull/514/head")
            self.assertEqual(run.call_args_list[2].args[0][-2:], ["--detach", "b" * 40])

    def test_stale_cached_checkout_is_not_reused(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stale_checkout = root / "cache" / f"tech-innovation-group-echomem-pr-514-{'b' * 12}-old" / "repo"
            stale_checkout.mkdir(parents=True)
            with patch("server.review_checkout._has_exact_scope", side_effect=[False, True, True]), patch(
                "server.review_checkout._has_commit", return_value=True
            ), patch("server.review_checkout._checkout_is_at_head", side_effect=[False, True]), patch(
                "server.review_checkout._run"
            ):
                chosen = prepare_review_checkout(self.pr, root / "configured", root / "cache")
            self.assertNotEqual(chosen, stale_checkout)

    def test_invalid_sha_fails_without_running_git(self) -> None:
        self.pr.head_sha = "not-a-sha"
        with tempfile.TemporaryDirectory() as directory, patch("server.review_checkout._run") as run:
            with self.assertRaises(ReviewCheckoutError):
                prepare_review_checkout(self.pr, Path(directory), Path(directory) / "cache")
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
