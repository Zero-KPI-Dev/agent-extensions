from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Protocol


class PrScope(Protocol):
    owner: str
    repository: str
    number: int
    base_ref: str
    base_sha: str
    head_sha: str


class ReviewCheckoutError(RuntimeError):
    """The exact PR commits cannot be made available without touching the user's worktree."""


def _has_commit(repo: Path, sha: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "-e", f"{sha}^{{commit}}"],
            capture_output=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _has_exact_scope(repo: Path, pr: PrScope) -> bool:
    return _has_commit(repo, pr.base_sha) and _has_commit(repo, pr.head_sha)


def _checkout_is_at_head(repo: Path, sha: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip().lower() == sha.lower()


def _run(args: list[str], *, timeout: int = 180) -> None:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            timeout=timeout,
            env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReviewCheckoutError(f"无法取得精确 PR 代码（{type(exc).__name__}）") from exc
    if result.returncode != 0:
        # Git/gh stderr may include credential-bearing remote URLs.  Never
        # expose it in a Feishu error card or gateway log.
        raise ReviewCheckoutError(f"无法取得精确 PR 代码（命令退出码 {result.returncode}）")


def prepare_review_checkout(pr: PrScope, repo_root: Path, cache_root: Path) -> Path:
    """Use the configured repo when possible; otherwise make an isolated PR checkout.

    No fetch, checkout or reset is performed in ``repo_root``.  The fallback
    is keyed by the exact head SHA, so a later push never mutates a checkout
    that an existing Codex task may still be reading.
    """

    if not all(re.fullmatch(r"[0-9a-fA-F]{40}", sha) for sha in (pr.base_sha, pr.head_sha)):
        raise ReviewCheckoutError("GitHub 没有返回有效的 base/head SHA")
    if _has_exact_scope(repo_root, pr):
        return repo_root
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in (pr.owner, pr.repository)):
        raise ReviewCheckoutError("GitHub 仓库名称无效")

    cache_root.mkdir(parents=True, exist_ok=True)
    prefix = f"{pr.owner.lower()}-{pr.repository.lower()}-pr-{pr.number}-{pr.head_sha[:12]}-"
    for candidate in cache_root.glob(prefix + "*"):
        checkout = candidate / "repo"
        if checkout.is_dir() and _has_exact_scope(checkout, pr) and _checkout_is_at_head(checkout, pr.head_sha):
            return checkout

    temporary_root = Path(tempfile.mkdtemp(prefix=prefix, dir=cache_root))
    checkout = temporary_root / "repo"
    try:
        _run(
            [
                "gh", "repo", "clone", f"{pr.owner}/{pr.repository}", str(checkout),
                "--", "--filter=blob:none", "--no-checkout", "--single-branch", "--branch", pr.base_ref,
            ]
        )
        _run(["git", "-C", str(checkout), "fetch", "--no-tags", "origin", f"refs/pull/{pr.number}/head"])
        for sha in (pr.base_sha, pr.head_sha):
            if not _has_commit(checkout, sha):
                _run(["git", "-C", str(checkout), "fetch", "--no-tags", "origin", sha])
        if not _has_exact_scope(checkout, pr):
            raise ReviewCheckoutError("GitHub PR 的精确 base/head 无法取得；未改审本地其他分支")
        _run(["git", "-C", str(checkout), "checkout", "--detach", pr.head_sha])
        if not _checkout_is_at_head(checkout, pr.head_sha):
            raise ReviewCheckoutError("独立检出未停在目标 PR 的精确 head；本轮未启动检视")
        return checkout
    except Exception:
        # Only a newly created cache entry is removed, never a user's repo or
        # a checkout from an earlier review.
        shutil.rmtree(temporary_root)
        raise
