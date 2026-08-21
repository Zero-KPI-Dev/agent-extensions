#!/usr/bin/env python3
"""Validate repository structure and reject common secret/runtime artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CODEX_ROOT = ROOT / "platforms" / "codex"
MARKETPLACE_PATH = CODEX_ROOT / ".agents" / "plugins" / "marketplace.json"

SKIP_PARTS = {".git", "__pycache__", ".pytest_cache", ".venv", "venv"}
DENIED_NAMES = {".env", "config.json", "state.sqlite", "state.sqlite3"}
DENIED_SUFFIXES = {
    ".log",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".py",
    ".sh",
    ".txt",
    ".yaml",
    ".yml",
}

SECRET_PATTERNS = {
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "GitHub token": re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    "OpenAI-style key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "Feishu app id": re.compile(r"\bcli_[A-Za-z0-9]{12,}\b"),
}


def repository_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file():
            files.append(path)
    return files


def validate_marketplace(errors: list[str]) -> None:
    try:
        marketplace = json.loads(MARKETPLACE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid marketplace: {exc}")
        return

    if marketplace.get("name") != "zero-kpi-dev":
        errors.append("marketplace name must be zero-kpi-dev")

    for entry in marketplace.get("plugins", []):
        name = entry.get("name")
        source = entry.get("source", {}).get("path")
        expected = f"./plugins/{name}"
        if source != expected:
            errors.append(f"plugin {name}: source must be {expected}, got {source!r}")
            continue
        plugin_root = CODEX_ROOT / "plugins" / str(name)
        manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"plugin {name}: invalid manifest: {exc}")
            continue
        if manifest.get("name") != name or plugin_root.name != name:
            errors.append(f"plugin {name}: directory and manifest names must match")


def validate_files(errors: list[str]) -> None:
    for path in repository_files():
        relative = path.relative_to(ROOT)
        lower_name = path.name.lower()
        if lower_name in DENIED_NAMES and lower_name != "config.example.json":
            errors.append(f"forbidden runtime/config file: {relative}")
        if path.suffix.lower() in DENIED_SUFFIXES:
            errors.append(f"forbidden file type: {relative}")
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(text):
                value = match.group(0)
                if value in {"cli_replace_me"}:
                    continue
                errors.append(f"possible {label}: {relative}")
                break
        if (
            relative != Path("scripts/validate_repository.py")
            and "tests" not in relative.parts
            and re.search(r"/Users/[^/<>{}\s]+/", text)
        ):
            errors.append(f"personal absolute path outside tests: {relative}")

        if path.suffix.lower() == ".json":
            for field in ("app_secret", "verification_token", "access_token", "client_secret"):
                for match in re.finditer(rf'"{field}"\s*:\s*"([^"]+)"', text):
                    if match.group(1) not in {"replace_me", "example", "placeholder"}:
                        errors.append(f"non-placeholder {field}: {relative}")


def main() -> int:
    errors: list[str] = []
    validate_marketplace(errors)
    validate_files(errors)
    if errors:
        print("Repository validation failed:")
        for error in sorted(set(errors)):
            print(f"- {error}")
        return 1
    print("Repository validation passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
