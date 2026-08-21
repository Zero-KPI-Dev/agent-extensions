from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


PR_URL_RE = re.compile(
    r"https?://(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/pull/(\d+)(?:[^\s]*)?",
    re.IGNORECASE,
)
PR_NUMBER_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:pr|pull(?:\s*request)?|#)\s*#?\s*(\d+)(?!\d)",
    re.IGNORECASE,
)
MENTION_TOKEN_RE = re.compile(r"(?:@_user_\d+|<at\b[^>]*>.*?</at>|@[A-Za-z0-9_.-]+)", re.IGNORECASE)


class FeishuError(RuntimeError):
    pass


@dataclass(frozen=True)
class FeishuEvent:
    event_id: str
    event_type: str
    chat_id: str
    message_id: str
    sender_id: str
    text: str
    mentioned_bot: bool


def _json_content(content: Any) -> dict[str, Any]:
    if isinstance(content, dict):
        return content
    if isinstance(content, str):
        try:
            value = json.loads(content)
        except json.JSONDecodeError:
            return {"text": content}
        return value if isinstance(value, dict) else {"text": str(value)}
    return {"text": str(content or "")}


def _mentioned_bot(mentions: Any, bot_open_id: str) -> bool:
    if not bot_open_id:
        return False
    for mention in mentions or []:
        if not isinstance(mention, dict):
            continue
        candidate = mention.get("id", {})
        if isinstance(candidate, dict):
            for key in ("open_id", "user_id", "union_id"):
                if candidate.get(key) == bot_open_id:
                    return True
        if mention.get("open_id") == bot_open_id:
            return True
    return False


def parse_event(payload: dict[str, Any], bot_open_id: str) -> FeishuEvent | None:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id_data = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    content = _json_content(message.get("content", ""))
    event_type = str(header.get("event_type") or event.get("type") or "im.message.receive_v1")
    event_id = str(header.get("event_id") or payload.get("event_id") or payload.get("uuid") or message.get("message_id") or "")
    chat_id = str(message.get("chat_id") or event.get("chat_id") or "")
    message_id = str(message.get("message_id") or payload.get("message_id") or "")
    sender_id = str(sender_id_data.get("open_id") or sender_id_data.get("user_id") or "")
    text = str(content.get("text") or "").strip()
    if not event_id or not chat_id or not message_id or not text:
        return None
    return FeishuEvent(
        event_id=event_id,
        event_type=event_type,
        chat_id=chat_id,
        message_id=message_id,
        sender_id=sender_id,
        text=text,
        mentioned_bot=_mentioned_bot(message.get("mentions", []), bot_open_id),
    )


def extract_pr_number(text: str) -> str | None:
    match = PR_NUMBER_RE.search(text)
    return match.group(1) if match else None


def extract_pr_url(text: str, default_repo: str | None = None) -> str | None:
    match = PR_URL_RE.search(text)
    if match:
        return f"https://github.com/{match.group(1)}/{match.group(2)}/pull/{match.group(3)}"

    if not default_repo:
        return None
    repo_parts = [part.strip() for part in default_repo.split("/", 1)]
    if len(repo_parts) != 2 or not all(repo_parts):
        return None
    number = extract_pr_number(text)
    if not number:
        return None
    return f"https://github.com/{repo_parts[0]}/{repo_parts[1]}/pull/{number}"


def is_help_request(text: str) -> bool:
    """Return whether a Feishu message is asking how to use the bot."""

    # A PR target always wins over help-like wording, such as "帮助我检视 #340".
    if PR_URL_RE.search(text) or extract_pr_number(text):
        return False

    cleaned = MENTION_TOKEN_RE.sub(" ", text).strip()
    if not cleaned:
        return True
    if re.fullmatch(r"(?:/|--?)?(?:help|h)", cleaned, re.IGNORECASE):
        return True

    normalized = re.sub(r"[\s?？!！,，。:：/\-_]+", "", cleaned).lower()
    exact = {
        "帮助",
        "使用帮助",
        "使用说明",
        "使用方法",
        "怎么用",
        "如何使用",
        "如何用",
        "你怎么用",
        "功能",
        "指令",
        "命令",
        "commands",
    }
    if normalized in exact:
        return True
    return any(
        phrase in normalized
        for phrase in (
            "帮助",
            "使用说明",
            "使用方法",
            "怎么用",
            "怎么使用",
            "如何用",
            "如何使用",
            "能做什么",
            "你会什么",
            "有哪些功能",
            "怎么触发",
            "指令",
            "命令",
        )
    )


def split_text(text: str, max_length: int) -> list[str]:
    max_length = max(200, max_length)
    if len(text) <= max_length:
        return [text]
    chunks: list[str] = []
    remaining = text.strip()
    while len(remaining) > max_length:
        cut = remaining.rfind("\n", 0, max_length)
        if cut < max_length // 2:
            cut = remaining.rfind(" ", 0, max_length)
        if cut < max_length // 2:
            cut = max_length
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def _clip_card_text(value: str, max_length: int = 1600) -> str:
    value = value.strip()
    if len(value) <= max_length:
        return value
    return value[: max_length - 1].rstrip() + "…"


def _card_lines(report: str) -> list[str]:
    return [line.strip() for line in report.replace("\r\n", "\n").splitlines() if line.strip()]


def _clean_card_line(line: str) -> str:
    line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line.strip())
    # A few summary formats contain a placeholder Markdown link such as
    # `[owner/repo#123]()`. It is not useful in a card; the PR button below
    # carries the real URL.
    line = re.sub(r"\[([^\]]+)\]\(\s*\)", r"\1", line)
    return line.strip()


def _strip_card_markup(value: str) -> str:
    """Make a value suitable for a compact card label or status line."""
    value = re.sub(r"`([^`]*)`", r"\1", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", value)
    return re.sub(r"\s+", " ", value).strip()


def _first_labeled_value(lines: list[str], pattern: str) -> str | None:
    expression = re.compile(pattern, re.IGNORECASE)
    for raw_line in lines:
        line = _clean_card_line(raw_line)
        match = expression.search(line)
        if match:
            value = match.group(1).strip()
            code_match = re.search(r"`([^`]+)`", value)
            if code_match:
                value = code_match.group(1)
            value = re.split(r"\s*[（(。；;]", value, maxsplit=1)[0]
            value = _strip_card_markup(value).strip("`，,。；; ")
            if value:
                return value
    return None


def _counts_in_line(line: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for severity in ("Critical", "High", "Medium", "Low", "Suggestion"):
        expression = re.compile(
            rf"\b{severity}\b\s*(?:[：:]\s*)?`?(\d+)\b",
            re.IGNORECASE,
        )
        match = expression.search(line)
        if match:
            counts[severity] = int(match.group(1))
    return counts


def _extract_counts(lines: list[str]) -> dict[str, int]:
    severities = ("Critical", "High", "Medium", "Low", "Suggestion")
    # Prefer the overview line so that a number in a finding description is
    # not mistaken for the severity count.
    overview = [
        line
        for line in lines
        if "发现" in line or "finding" in line.lower() or sum(name.lower() in line.lower() for name in severities) >= 2
    ]
    search_lines = overview + [line for line in lines if line not in overview]
    counts: dict[str, int] = {}
    for line in search_lines:
        for severity, count in _counts_in_line(line).items():
            counts.setdefault(severity, count)
    return counts


def _first_line_containing(lines: list[str], *markers: str) -> str:
    lowered_markers = tuple(marker.lower() for marker in markers)
    for line in lines:
        lowered = line.lower()
        if any(marker in lowered for marker in lowered_markers):
            return _clean_card_line(line)
    return ""


def _normalize_publish_status(value: str) -> str:
    normalized = value.strip().upper().replace(" ", "_")
    return {
        "PUBLISHED": "已发布",
        "COMMENT": "已发布",
        "FAILED": "发布失败",
        "NOT_ATTEMPTED": "未发布",
        "NOT_ATTEMPTED_YET": "未发布",
    }.get(normalized, value.strip())


def _parse_publish_line(lines: list[str]) -> tuple[str, str]:
    line = _first_line_containing(lines, "github 发布", "github publish")
    if not line:
        return "", ""
    detail = re.sub(
        r"^(?:github\s+发布|github\s+publish)\s*[：:]\s*",
        "",
        line,
        flags=re.IGNORECASE,
    )
    status_match = re.search(
        r"(已发布|未发布|发布失败|published|failed|not[_ ]attempted)",
        detail,
        flags=re.IGNORECASE,
    )
    if not status_match:
        return "", _strip_card_markup(detail).strip("。.;； ")
    status = _normalize_publish_status(status_match.group(1))
    reason = detail[status_match.end() :].lstrip("。.;；:：,，- ")
    return status, _strip_card_markup(reason).strip("。.;； ")


def _extract_report_status(lines: list[str]) -> str:
    status = _first_labeled_value(
        lines,
        r"(?<![\w\u4e00-\u9fff])(?:结论|状态|status)\s*(?:为|is)?\s*[：:]?\s*(.+)",
    )
    if status:
        return status
    joined = "\n".join(lines)
    for candidate in (
        "FIXED_VERIFIED",
        "FIX_VERIFIED",
        "PARTIALLY_FIXED",
        "DISPUTED_OPEN",
        "UNVERIFIABLE",
        "NO_ACTIONABLE_FINDINGS",
        "ACTION_REQUIRED",
        "NO_NEW_REVISION",
        "SKIPPED_TOOL_UNAVAILABLE",
        "FAILED",
        "CANCELLED",
        "PASSED",
    ):
        if candidate in joined.upper():
            return candidate
    if "已修复" in joined and "复检" in joined:
        return "FIX_VERIFIED"
    return ""


def _infer_report_mode(
    report: str,
    *,
    conclusion: str = "",
    history_summary: str = "",
    new_summary: str = "",
) -> str:
    """Infer a mode only when an older summary omitted the explicit field."""

    upper = report.upper()
    mode_match = re.search(
        r"\b(INITIAL_REVIEW|FIX_VERIFICATION|INCREMENTAL_REREVIEW|NO_NEW_REVISION)\b",
        upper,
    )
    if mode_match:
        return mode_match.group(1)
    if "NO_NEW_REVISION" in conclusion.upper():
        return "NO_NEW_REVISION"
    if history_summary and new_summary:
        return "INCREMENTAL_REREVIEW"
    if history_summary or "复检" in report or conclusion.upper() in {
        "FIXED_VERIFIED",
        "FIX_VERIFIED",
        "PARTIALLY_FIXED",
        "DISPUTED_OPEN",
        "UNVERIFIABLE",
    }:
        return "FIX_VERIFICATION"
    return "INITIAL_REVIEW"


def _display_mode(mode: str) -> str:
    normalized = mode.strip().upper()
    label = {
        "INITIAL_REVIEW": "初次检视",
        "FIX_VERIFICATION": "修复验证",
        "INCREMENTAL_REREVIEW": "增量复检",
        "NO_NEW_REVISION": "无新版本",
    }.get(normalized)
    return f"{normalized}（{label}）" if label else (mode.strip() or "未提供")


def _unique_nonempty(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _parse_json_report(report: str) -> dict[str, Any] | None:
    candidate = report.strip()
    if not candidate.startswith("{"):
        return None
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _report_card_data(report: str, pr_url: str | None = None) -> dict[str, Any]:
    lines = _card_lines(report)
    parsed = _parse_json_report(report)
    raw_run = parsed.get("run") if isinstance(parsed, dict) and isinstance(parsed.get("run"), dict) else parsed or {}
    raw_findings = raw_run.get("findings", []) if isinstance(raw_run, dict) else []
    if not raw_findings and isinstance(parsed, dict):
        raw_findings = parsed.get("findings", [])
    if not isinstance(raw_findings, list):
        raw_findings = []

    counts: dict[str, int] = {}
    historical_counts: dict[str, int] = {}
    new_counts: dict[str, int] = {}
    findings: list[str] = []
    history_summary = ""
    new_summary = ""
    conclusion = ""
    publish_status = ""
    publish_detail = ""
    other: list[str] = []
    if parsed is not None:
        for finding in raw_findings:
            if not isinstance(finding, dict):
                continue
            severity = str(finding.get("severity") or "").strip()
            if severity:
                counts[severity.title()] = counts.get(severity.title(), 0) + 1
            detail = str(finding.get("title") or finding.get("claim") or finding.get("detail") or "").strip()
            if detail:
                findings.append(f"{severity}: {detail}" if severity else detail)
        candidate_url = raw_run.get("github_target") if isinstance(raw_run, dict) else None
        pr_url = pr_url or (str(candidate_url).strip() if candidate_url else None)
        review_id = str(raw_run.get("review_id") or raw_run.get("run_id") or "").strip() if isinstance(raw_run, dict) else ""
        mode = str(raw_run.get("mode") or "").strip() if isinstance(raw_run, dict) else ""
        publish_status = _normalize_publish_status(str(raw_run.get("publish_status") or "").strip()) if isinstance(raw_run, dict) else ""
        publish_policy = str(raw_run.get("publish_policy") or "").strip() if isinstance(raw_run, dict) else ""
        publish_detail = f"策略：{publish_policy}" if publish_policy else ""
        conclusion = str(raw_run.get("status") or raw_run.get("decision") or "").strip()
        raw_limitations = raw_run.get("limitations", []) if isinstance(raw_run, dict) else []
        if not raw_limitations and isinstance(parsed, dict):
            raw_scope = parsed.get("scope") if isinstance(parsed.get("scope"), dict) else {}
            raw_limitations = raw_scope.get("limitations", [])
        if not isinstance(raw_limitations, list):
            raw_limitations = []
        other = [
            _clean_card_line(str(item))
            for item in raw_limitations
            if str(item).strip()
        ]
    else:
        counts = _extract_counts(lines)
        review_id = _first_labeled_value(
            lines,
            r"(?:`?review[_ ]?id`?)\s*[：:]\s*(.+)",
        ) or ""
        mode = _first_labeled_value(lines, r"(?:模式|mode)\s*(?:为|is)?\s*[：:]\s*(.+)") or ""
        conclusion = _extract_report_status(lines)
        publish_status, publish_detail = _parse_publish_line(lines)

        history_summary = _first_line_containing(lines, "历史 findings", "历史发现")
        new_summary = _first_line_containing(lines, "本轮新增 findings", "本轮新增发现", "new findings")
        if history_summary:
            historical_counts = _counts_in_line(history_summary)
        if new_summary:
            new_counts = _counts_in_line(new_summary)
            counts = new_counts

        heading_index = next(
            (
                index
                for index, line in enumerate(lines)
                if any(marker in line.lower() for marker in ("主要问题", "主要发现", "key findings"))
            ),
            None,
        )
        if heading_index is not None:
            for line in lines[heading_index + 1 :]:
                if any(marker in line.lower() for marker in ("未发布或阻塞", "阻塞原因", "其他限制", "补充信息")):
                    break
                cleaned = _clean_card_line(line)
                if cleaned and not any(marker in cleaned.lower() for marker in ("github 发布", "github publish")):
                    findings.append(cleaned)
        if not findings:
            findings = [
                _clean_card_line(line)
                for line in lines
                if (
                    re.search(r"\b(?:critical|high|medium|low|suggestion)\b", line, re.IGNORECASE)
                    or re.search(r"\bF-\d+\b", line, re.IGNORECASE)
                )
                and "发现" not in line
                and "github 发布" not in line.lower()
            ]

        for line in lines:
            cleaned = _clean_card_line(line)
            lower = cleaned.lower()
            if (
                cleaned not in {publish_detail, history_summary, new_summary}
                and any(marker in lower for marker in ("a/b", "阻塞", "限制", "sqlite", "只读", "未运行", "未修改", "其他"))
            ):
                other.append(cleaned)

    if not mode:
        mode = _infer_report_mode(
            report,
            conclusion=conclusion,
            history_summary=history_summary,
            new_summary=new_summary,
        )

    other = _unique_nonempty(other)
    findings = _unique_nonempty(findings)

    if pr_url is None:
        match = PR_URL_RE.search(report)
        pr_url = match.group(0).rstrip(".,);】") if match else None

    normalized_counts = {
        severity: counts.get(severity, 0)
        for severity in ("Critical", "High", "Medium", "Low", "Suggestion")
    }
    return {
        "pr_url": pr_url,
        "review_id": review_id,
        "mode": mode,
        "conclusion": conclusion,
        "counts": normalized_counts,
        "historical_counts": {
            severity: historical_counts.get(severity, 0)
            for severity in ("Critical", "High", "Medium", "Low", "Suggestion")
        },
        "new_counts": {
            severity: new_counts.get(severity, 0)
            for severity in ("Critical", "High", "Medium", "Low", "Suggestion")
        },
        "history_summary": _clip_card_text(history_summary, 1000),
        "new_summary": _clip_card_text(new_summary, 1000),
        "publish_status": publish_status,
        "publish_detail": _clip_card_text(publish_detail, 1200),
        "findings": [_clip_card_text(item, 1000) for item in findings[:5]],
        "other": [_clip_card_text(item, 1000) for item in other[:4]],
    }


def _card_div(content: str) -> dict[str, Any]:
    return {"tag": "div", "text": {"tag": "lark_md", "content": content}}


def _pr_card_label(pr_url: str) -> str:
    match = PR_URL_RE.search(pr_url)
    if not match:
        return pr_url
    return f"{match.group(1)}/{match.group(2)}#{match.group(3)}"


def build_help_text(
    *,
    default_repo: str | None,
    configured_repos: list[str] | None = None,
    notice: str | None = None,
) -> str:
    """Build the plain-text fallback for the bot help response."""

    repos = configured_repos or []
    lines = ["PR 检视机器人使用帮助"]
    if notice:
        lines.extend(["", notice])
    lines.extend(
        [
            "",
            "在群里 @机器人并发送 GitHub PR 链接即可开始检视。",
            "示例：https://github.com/org/repo/pull/123",
        ]
    )
    if default_repo:
        lines.extend(
            [
                f"默认仓库：{default_repo}",
                "简写：检视 #314、复检 PR 314 或 pr314",
            ]
        )
    elif repos:
        lines.append("当前配置了多个仓库但没有默认仓库；使用 PR 号时请改发完整 GitHub PR 链接。")
    else:
        lines.append("本机尚未配置仓库映射，请先完成仓库配置。")
    lines.extend(
        [
            "可以在同一条消息中补充关注点。",
            "机器人会在后台运行 Leader + A/B 独立复核，完成后回传结果，并按 Skill 规则发布 GitHub 检视意见。",
            "再次查看本帮助：@机器人 help、帮助或怎么用。",
        ]
    )
    return "\n".join(lines)


def build_help_card(
    *,
    default_repo: str | None,
    configured_repos: list[str] | None = None,
    notice: str | None = None,
) -> dict[str, Any]:
    """Build a compact CLI-style help card for Feishu users."""

    repos = configured_repos or []
    elements: list[dict[str, Any]] = []
    if notice:
        elements.extend([_card_div(f"**提示**\n{notice}"), {"tag": "hr"}])

    elements.append(
        _card_div(
            "**开始检视**\n"
            "在群里 **@本机器人**，发送 GitHub PR 链接：\n"
            "`https://github.com/org/repo/pull/123`"
        )
    )
    if default_repo:
        elements.append(
            _card_div(
                f"**默认仓库** `{default_repo}`\n"
                "可简写为 `检视 #314`、`复检 PR 314` 或 `pr314`。"
            )
        )
    elif repos:
        elements.append(
            _card_div(
                "**仓库选择**\n当前配置了多个仓库但没有默认仓库。"
                "使用 PR 号时，请发送完整 GitHub PR 链接以避免歧义。"
            )
        )
    else:
        elements.append(_card_div("**需要配置**\n本机尚未配置仓库映射，请先完成仓库配置。"))

    elements.extend(
        [
            {"tag": "hr"},
            _card_div(
                "**可以附加要求**\n"
                "在同一条消息补充关注点，例如：`重点检查权限、并发和数据库迁移`。"
            ),
            _card_div(
                "**执行方式**\n"
                "后台运行 **Leader + A/B 独立复核**；完成后回传结果，"
                "并按 Skill 规则将共识后的可行动意见发布到 GitHub PR。"
            ),
            {"tag": "hr"},
            _card_div("再次查看本帮助：只需 **@本机器人**，或发送 `help`、`帮助`、`怎么用`。"),
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": "PR 检视机器人帮助"}},
        "elements": elements,
    }


def build_ack_card(
    *,
    pr_url: str,
    job_id: str,
    status: str,
    pr_label: str | None = None,
    deduplicated: bool = False,
    repo_mapping_missing: bool = False,
    repo_key: str | None = None,
) -> dict[str, Any]:
    """Build the compact acknowledgement card sent before a review starts."""

    status_label = {
        "pending": "等待执行",
        "running": "正在检视",
        "succeeded": "已完成",
        "failed": "执行失败",
        "cancelled": "已取消",
    }.get(status, status or "已受理")
    display_pr = pr_label or _pr_card_label(pr_url)

    if repo_mapping_missing:
        title, template = "PR 检视未启动", "orange"
        status_label = "需要配置"
        detail = (
            f"本机尚未配置 **{repo_key or '该仓库'}** 的仓库映射，任务不会启动。"
            "请完成配置后重新触发。"
        )
    elif deduplicated:
        title, template = "PR 检视已在进行", "blue"
        detail = (
            "本次请求已合并到现有任务，不会重复创建 Codex 会话。"
            "任务完成后，结果仍会回传到本群。"
        )
    else:
        title, template = "PR 检视已受理", "blue"
        detail = (
            "后台将执行 **Leader + A/B 独立复核**。完成后自动回传结果；"
            "共识后的可行动意见会发布到 GitHub PR。"
        )

    metadata = "\n".join(
        [
            f"**PR** [{display_pr}]({pr_url})",
            f"**任务 ID** {job_id[:8]}",
            f"**状态** {status_label}",
        ]
    )
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": [
            _card_div(metadata),
            {"tag": "hr"},
            _card_div(detail),
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 GitHub PR"},
                        "type": "primary",
                        "url": pr_url,
                    }
                ],
            },
        ],
    }


def build_review_card(report: str, pr_url: str | None = None, max_length: int = 3500) -> dict[str, Any]:
    """Build a compact legacy interactive card accepted by the IM v1 API."""
    data = _report_card_data(report, pr_url=pr_url)
    first_line = _card_lines(report)[0].lower() if _card_lines(report) else ""
    conclusion = str(data.get("conclusion") or "")
    normalized_conclusion = conclusion.upper()
    is_clean_conclusion = normalized_conclusion in {
        "FIXED_VERIFIED",
        "FIX_VERIFIED",
        "NO_ACTIONABLE_FINDINGS",
        "NO_NEW_REVISION",
        "PASSED",
    }
    is_failure = "失败" in first_line or "failed" in first_line or conclusion.upper() in {"FAILED", "FAILURE"}
    is_cancelled = "取消" in first_line or "cancel" in first_line
    mode = str(data.get("mode") or "").upper()
    is_follow_up = mode in {"FIX_VERIFICATION", "INCREMENTAL_REREVIEW", "NO_NEW_REVISION"}
    counts = data["counts"]
    total_findings = sum(counts.values())
    highest_severity = next(
        (severity for severity in ("Critical", "High", "Medium", "Low", "Suggestion") if counts[severity]),
        "",
    )
    if is_failure:
        title, template = "❌ PR 检视失败", "red"
    elif is_cancelled:
        title, template = "⏹️ PR 检视已取消", "orange"
    elif highest_severity == "Critical":
        title, template = f"🔴 发现 {total_findings} 个问题（含 Critical）", "red"
    elif highest_severity == "High":
        title, template = f"🟠 发现 {total_findings} 个问题（含 High）", "orange"
    elif highest_severity == "Medium":
        title, template = f"🟡 发现 {total_findings} 个问题（最高 Medium）", "yellow"
    elif highest_severity in {"Low", "Suggestion"}:
        title, template = f"🔵 发现 {total_findings} 个建议", "blue"
    elif is_clean_conclusion and is_follow_up:
        title, template = "✅ PR 复检通过", "green"
    elif is_clean_conclusion:
        title, template = "✅ PR 检视未发现问题", "green"
    elif data["findings"]:
        title, template = "⚠️ 检视发现待处理问题", "orange"
    elif conclusion.upper() in {"PARTIALLY_FIXED", "DISPUTED_OPEN", "UNVERIFIABLE"}:
        title, template = "🟡 PR 复检仍有未决问题", "yellow"
    elif is_follow_up:
        title, template = "✅ PR 复检未发现新问题", "green"
    else:
        title, template = "✅ PR 检视未发现问题", "green"

    url = data["pr_url"]
    if url:
        pr_label = _pr_card_label(url)
        pr_line = f"**PR** [{pr_label}]({url})"
    else:
        pr_line = "**PR** —"
    metadata_lines = [
        pr_line,
        f"**Review ID** {data['review_id'] or '—'}",
        f"**模式** {_display_mode(str(data['mode']))}",
    ]
    if conclusion:
        metadata_lines.append(f"**结论** {conclusion}")
    metadata = "\n".join(metadata_lines)

    historical_counts = data["historical_counts"]
    new_counts = data["new_counts"]

    def format_counts(values: dict[str, int]) -> str:
        parts = [
            f"🔴 **Critical** {values['Critical']}",
            f"🟠 **High** {values['High']}",
            f"🟡 **Medium** {values['Medium']}",
            f"🔵 **Low** {values['Low']}",
        ]
        if values["Suggestion"]:
            parts.append(f"⚪ **Suggestion** {values['Suggestion']}")
        return "　·　".join(parts)

    if is_failure:
        outcome = "❌ **检视未完成，请查看下方失败原因**"
    elif is_cancelled:
        outcome = "⏹️ **检视任务已取消**"
    elif total_findings:
        outcome = f"⚠️ **共发现 {total_findings} 个待处理问题，最高级别 {highest_severity}**"
    elif is_clean_conclusion and is_follow_up:
        outcome = "✅ **历史问题已验证修复，本轮无待处理意见**"
    elif is_clean_conclusion:
        outcome = "✅ **未发现待处理问题**"
    elif data["findings"]:
        outcome = "⚠️ **发现待处理问题，但摘要未提供严重级别统计**"
    elif is_follow_up:
        outcome = "✅ **本轮未发现新的待处理问题**"
    else:
        outcome = "✅ **未发现待处理问题**"

    if data["history_summary"] or data["new_summary"]:
        overview_lines = [outcome, "", "**严重级别**"]
        if data["new_summary"] or any(new_counts.values()):
            overview_lines.append(f"**本轮新增**　{format_counts(new_counts)}")
        if data["history_summary"] or any(historical_counts.values()):
            historical_total = sum(historical_counts.values())
            overview_lines.append(
                f"**历史问题**　{historical_total} 条（{format_counts(historical_counts)}）"
            )
        overview = "\n".join(overview_lines)
    else:
        overview = f"{outcome}\n\n**严重级别**\n{format_counts(counts)}"

    elements: list[dict[str, Any]] = [_card_div(_clip_card_text(metadata, max_length)), {"tag": "hr"}, _card_div(overview)]

    publish_status = data["publish_status"]
    publish_detail = data["publish_detail"]
    if publish_status or publish_detail:
        publish_line = f"**状态**　{publish_status or '未说明'}"
        if publish_detail:
            publish_line += f"\n{publish_detail}"
        elements.extend([{"tag": "hr"}, _card_div(f"**GitHub 发布**\n{publish_line}")])

    summary_lines = [item for item in (data["history_summary"], data["new_summary"]) if item]
    if summary_lines:
        summary_text = "**复检摘要**\n" + "\n".join(f"- {_strip_card_markup(item)}" for item in summary_lines)
        elements.extend([{"tag": "hr"}, _card_div(_clip_card_text(summary_text, max_length))])
    elif data["findings"]:
        findings_heading = "**已验证修复**" if is_clean_conclusion and is_follow_up else "**主要发现**"
        finding_text = findings_heading + "\n" + "\n".join(
            f"{index}. {_strip_card_markup(item)}" for index, item in enumerate(data["findings"], start=1)
        )
        elements.extend([{"tag": "hr"}, _card_div(_clip_card_text(finding_text, max_length))])
    if data["other"]:
        other_text = "**验证限制 / 备注**\n" + "\n".join(
            f"- {_strip_card_markup(item)}" for item in data["other"]
        )
        elements.extend([{"tag": "hr"}, _card_div(_clip_card_text(other_text, max_length))])
    if is_failure and not publish_detail and not data["findings"] and not data["other"]:
        fallback = "\n".join(_clean_card_line(line) for line in _card_lines(report)[1:])
        if fallback:
            elements.extend([{"tag": "hr"}, _card_div(_clip_card_text(fallback, max_length))])
    if url:
        elements.append(
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看 GitHub PR"},
                        "type": "primary",
                        "url": url,
                    }
                ],
            }
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": template, "title": {"tag": "plain_text", "content": title}},
        "elements": elements,
    }


class FeishuClient:
    def __init__(self, base_url: str, app_id: str, app_secret: str):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: str | None = None
        self._token_expires_at = 0.0
        self._token_lock = threading.Lock()

    def _request(self, method: str, path: str, *, body: dict[str, Any] | None = None, token: str | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise FeishuError(f"飞书 API HTTP {exc.code}: {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise FeishuError(f"飞书 API 网络错误: {exc.reason}") from exc
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise FeishuError(f"飞书 API 返回了非 JSON 内容: {raw[:300]}") from exc
        if not isinstance(value, dict):
            raise FeishuError("飞书 API 返回格式异常")
        if value.get("code", 0) not in (0, None):
            raise FeishuError(f"飞书 API code={value.get('code')}: {value.get('msg', '')}")
        return value

    def tenant_access_token(self) -> str:
        if not self.app_id or not self.app_secret:
            raise FeishuError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
        now = time.time()
        if self._token and now < self._token_expires_at - 60:
            return self._token
        with self._token_lock:
            now = time.time()
            if self._token and now < self._token_expires_at - 60:
                return self._token
            result = self._request(
                "POST",
                "/open-apis/auth/v3/tenant_access_token/internal",
                body={"app_id": self.app_id, "app_secret": self.app_secret},
            )
            token = str(result.get("tenant_access_token") or "")
            if not token:
                raise FeishuError("飞书未返回 tenant_access_token")
            self._token = token
            self._token_expires_at = time.time() + int(result.get("expire", 7200))
            return token

    def send_text(self, chat_id: str, text: str, max_length: int = 3500) -> list[dict[str, Any]]:
        token = self.tenant_access_token()
        results = []
        for chunk in split_text(text, max_length):
            query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
            results.append(
                self._request(
                    "POST",
                    f"/open-apis/im/v1/messages?{query}",
                    token=token,
                    body={
                        "receive_id": chat_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": chunk}, ensure_ascii=False),
                    },
                )
            )
        return results

    def send_review_card(
        self,
        chat_id: str,
        report: str,
        max_length: int = 3500,
        *,
        pr_url: str | None = None,
    ) -> list[dict[str, Any]]:
        token = self.tenant_access_token()
        query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
        card = build_review_card(report, pr_url=pr_url, max_length=max_length)
        return [
            self._request(
                "POST",
                f"/open-apis/im/v1/messages?{query}",
                token=token,
                body={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                },
            )
        ]

    def send_ack_card(
        self,
        chat_id: str,
        *,
        pr_url: str,
        job_id: str,
        status: str,
        pr_label: str | None = None,
        deduplicated: bool = False,
        repo_mapping_missing: bool = False,
        repo_key: str | None = None,
    ) -> list[dict[str, Any]]:
        token = self.tenant_access_token()
        query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
        card = build_ack_card(
            pr_url=pr_url,
            job_id=job_id,
            status=status,
            pr_label=pr_label,
            deduplicated=deduplicated,
            repo_mapping_missing=repo_mapping_missing,
            repo_key=repo_key,
        )
        return [
            self._request(
                "POST",
                f"/open-apis/im/v1/messages?{query}",
                token=token,
                body={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                },
            )
        ]

    def send_help_card(
        self,
        chat_id: str,
        *,
        default_repo: str | None,
        configured_repos: list[str] | None = None,
        notice: str | None = None,
    ) -> list[dict[str, Any]]:
        token = self.tenant_access_token()
        query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
        card = build_help_card(
            default_repo=default_repo,
            configured_repos=configured_repos,
            notice=notice,
        )
        return [
            self._request(
                "POST",
                f"/open-apis/im/v1/messages?{query}",
                token=token,
                body={
                    "receive_id": chat_id,
                    "msg_type": "interactive",
                    "content": json.dumps(card, ensure_ascii=False, separators=(",", ":")),
                },
            )
        ]
