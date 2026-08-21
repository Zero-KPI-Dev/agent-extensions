from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.config import Config, bundled_review_skill_path


class ReviewSkillPathTests(unittest.TestCase):
    def load_config(self, value: dict[str, object], *, override: str = "") -> Config:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(json.dumps(value), encoding="utf-8")
            environment = {"FEISHU_PR_REVIEW_CONFIG": str(config_path)}
            if override:
                environment["REVIEW_SKILL_PATH"] = override
            with patch.dict(os.environ, environment, clear=True):
                return Config.load()

    def test_default_uses_bundled_review_skill(self) -> None:
        config = self.load_config({})

        self.assertEqual(config.review_skill_path, bundled_review_skill_path())
        self.assertTrue(config.review_skill_path.is_file())
        self.assertTrue(config.public_summary()["review_skill_bundled"])

    def test_legacy_user_skill_path_migrates_to_bundled_copy(self) -> None:
        config = self.load_config(
            {"review_skill_path": "/Users/example/.codex/skills/review-pr-with-panel/SKILL.md"}
        )

        self.assertEqual(config.review_skill_path, bundled_review_skill_path())

    def test_custom_config_path_is_preserved(self) -> None:
        custom = "/opt/review-skills/custom/SKILL.md"
        config = self.load_config({"review_skill_path": custom})

        self.assertEqual(config.review_skill_path, Path(custom))

    def test_environment_override_takes_priority(self) -> None:
        override = "/tmp/review-pr-with-panel/SKILL.md"
        config = self.load_config({}, override=override)

        self.assertEqual(config.review_skill_path, Path(override))


if __name__ == "__main__":
    unittest.main()
