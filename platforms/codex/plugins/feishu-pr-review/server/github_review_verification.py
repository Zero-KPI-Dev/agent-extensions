from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import resolve_executable
from .feishu import _report_card_data


@dataclass(frozen=True)
class PublishedReviewCheck:
    confirmed: bool
    detail: str
    url: str = ""


def verify_reported_publication(report: str, pr_url: str) -> PublishedReviewCheck | None:
    """Verify a claimed new or deduplicated review without retrying a write."""

    data = _report_card_data(report, pr_url=pr_url)
    is_new_publication = data["publish_status"] == "已发布"
    is_no_new_skip = data["publish_status"] == "已跳过" and str(data["mode"]).upper() == "NO_NEW_REVISION"
    if not (is_new_publication or is_no_new_skip):
        return None

    review_id = str(data["review_id"] or "").strip()
    if is_new_publication and not re.fullmatch(r"R-[A-Za-z0-9-]{8,100}", review_id):
        return PublishedReviewCheck(False, "摘要缺少有效的 Skill review_id，无法核对 GitHub 发布")

    parsed = urlparse(pr_url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.hostname not in {"github.com", "www.github.com"} or (
        len(parts) != 4 or parts[2].lower() != "pull" or not parts[3].isdigit()
    ):
        return PublishedReviewCheck(False, "PR URL 无法用于 GitHub 发布核验")
    owner, repository, _, number = parts
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in (owner, repository)):
        return PublishedReviewCheck(False, "PR 仓库名称无效，无法核验发布")

    gh = resolve_executable("gh")
    if not gh:
        return PublishedReviewCheck(False, "本机找不到 gh，无法核验 GitHub review")
    if is_no_new_skip:
        previous_url = str(data["published_review_url"] or "")
        match = re.search(r"#pullrequestreview-(\d+)$", previous_url, re.IGNORECASE)
        if not match:
            return PublishedReviewCheck(False, "摘要未提供同一 PR 的既有 COMMENT review 链接，无法确认去重依据")
        try:
            result = subprocess.run(
                [gh, "api", f"repos/{owner}/{repository}/pulls/{int(number)}/reviews/{match.group(1)}", "--jq", "{html_url,state}"],
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return PublishedReviewCheck(False, "读取既有 GitHub review 超时或失败，去重依据待确认")
        if result.returncode != 0:
            return PublishedReviewCheck(False, f"既有 GitHub review 查询失败（退出码 {result.returncode}），去重依据待确认")
        try:
            existing = json.loads(result.stdout)
        except json.JSONDecodeError:
            return PublishedReviewCheck(False, "既有 GitHub review 响应无法解析，去重依据待确认")
        actual_url = urlparse(str(existing.get("html_url") or "")) if isinstance(existing, dict) else urlparse("")
        expected_url = urlparse(previous_url)
        same_review = (
            actual_url.hostname in {"github.com", "www.github.com"}
            and actual_url.path.rstrip("/").lower() == expected_url.path.rstrip("/").lower()
            and actual_url.fragment.lower() == expected_url.fragment.lower()
        )
        if isinstance(existing, dict) and same_review and str(existing.get("state") or "").upper() == "COMMENTED":
            return PublishedReviewCheck(True, "已确认此前的 COMMENT review 存在；本轮没有新发布", previous_url)
        return PublishedReviewCheck(False, "既有 review 与摘要链接不一致，无法确认去重依据")

    endpoint = f"repos/{owner}/{repository}/pulls/{int(number)}/reviews?per_page=100"
    try:
        result = subprocess.run(
            [gh, "api", endpoint, "--paginate", "--jq", '.[] | {id,html_url,body,state} | @json'],
            capture_output=True,
            text=True,
            check=False,
            timeout=45,
        )
    except (OSError, subprocess.TimeoutExpired):
        return PublishedReviewCheck(False, "读取 GitHub review 超时或失败，发布状态待确认")
    if result.returncode != 0:
        # Do not forward gh stderr: it may contain auth or proxy details.
        return PublishedReviewCheck(False, f"GitHub review 查询失败（退出码 {result.returncode}），发布状态待确认")
    try:
        reviews = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    except json.JSONDecodeError:
        return PublishedReviewCheck(False, "GitHub review 响应无法解析，发布状态待确认")
    id_marker = re.compile(rf"^\s*review_id\s*:\s*`?{re.escape(review_id)}(?:`|\s|$)", re.IGNORECASE | re.MULTILINE)
    for review in reviews:
        if not isinstance(review, dict) or not id_marker.search(str(review.get("body") or "")):
            continue
        url = str(review.get("html_url") or "")
        review_url = urlparse(url)
        if review_url.hostname not in {"github.com", "www.github.com"} or (
            review_url.path.rstrip("/").lower() != parsed.path.rstrip("/").lower()
        ):
            continue
        if str(review.get("state") or "").upper() != "COMMENTED":
            continue
        return PublishedReviewCheck(True, "已在目标 PR 找到带本轮 review_id 的 COMMENT review", url)
    return PublishedReviewCheck(False, "目标 PR 上未找到带本轮 review_id 的 COMMENT review，不能确认已发布")
