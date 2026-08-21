"""Derive a stable Codex task title from a GitHub pull request URL."""

from __future__ import annotations

import argparse
import sys
from typing import Optional, Sequence
from urllib.parse import urlparse


def session_title_from_pr_url(pr_url: str) -> str:
    """Return ``Repository#number`` for a canonical GitHub PR URL."""

    parsed = urlparse(pr_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
        "github.com",
        "www.github.com",
    }:
        raise ValueError("URL must use the github.com host")

    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4 or parts[2] != "pull" or not parts[3].isdigit():
        raise ValueError("URL must match https://github.com/<owner>/<repo>/pull/<number>")

    repository = parts[1]
    if not repository:
        raise ValueError("repository name is missing")
    return f"{repository}#{int(parts[3])}"


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url")
    args = parser.parse_args(argv)
    try:
        sys.stdout.write(session_title_from_pr_url(args.url) + "\n")
    except ValueError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
