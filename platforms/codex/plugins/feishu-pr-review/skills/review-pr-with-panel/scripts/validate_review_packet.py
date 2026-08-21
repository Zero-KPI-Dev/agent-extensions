#!/usr/bin/env python3
"""Validate the mechanical envelope of review-pr-with-panel JSON packets."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


PACKET_ITEMS = {
    "A_INITIAL": "findings",
    "B_VERIFICATION": "reviews",
    "A_RECHECK": "responses",
    "A_FIX_VERIFY": "prior_findings",
    "B_FIX_VERIFICATION": "reviews",
    "A_FIX_RECHECK": "responses",
}
SEVERITIES = {"Critical", "High", "Medium", "Low", "Suggestion"}
CONFIDENCES = {"high", "medium", "low"}
VALIDITIES = {
    "CONFIRMED",
    "PARTIALLY_CONFIRMED",
    "REJECTED",
    "INSUFFICIENT_EVIDENCE",
}
SEVERITY_DECISIONS = {
    "MAINTAIN",
    "UPGRADE",
    "DOWNGRADE",
    "NOT_APPLICABLE",
    "UNDETERMINED",
}
RESPONSES = {
    "ACCEPT",
    "PARTIAL_ACCEPT",
    "REJECT_WITH_EVIDENCE",
    "NEED_MORE_EVIDENCE",
    "WITHDRAW",
}
FIX_STATUSES = {
    "FIXED_VERIFIED",
    "PARTIALLY_FIXED",
    "NOT_FIXED",
    "UNVERIFIABLE",
    "OBSOLETE",
    "REGRESSION_INTRODUCED",
    "DISPUTED_OPEN",
}
FIX_STATUS_DECISIONS = {"CLOSE", "KEEP_OPEN", "PARTIAL", "UNVERIFIABLE", "REGRESSION"}
FIX_RESPONSES = {
    "ACCEPT_CLOSURE",
    "PARTIAL_ACCEPT",
    "KEEP_OPEN_WITH_EVIDENCE",
    "NEED_MORE_EVIDENCE",
    "REOPEN",
}


def load_packet(argument: str | None) -> dict:
    raw = Path(argument).read_text(encoding="utf-8") if argument else sys.stdin.read()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("packet must be a JSON object")
    return value


def require(condition: bool, message: str, errors: list[str]) -> None:
    if not condition:
        errors.append(message)


def validate_finding(item: object, prefix: str, errors: list[str]) -> None:
    require(isinstance(item, dict), f"{prefix} must be an object", errors)
    if not isinstance(item, dict):
        return

    finding_id = item.get("finding_id")
    require(
        isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
        f"{prefix}.finding_id must match F-001",
        errors,
    )
    for field in (
        "revision",
        "title",
        "locations",
        "claim",
        "evidence",
        "impact",
        "severity",
        "severity_rationale",
        "verification",
    ):
        require(field in item, f"{prefix}.{field} is required", errors)
    require(item.get("severity") in SEVERITIES, f"{prefix}.severity is invalid", errors)
    confidence = item.get("confidence")
    if confidence is not None:
        require(confidence in CONFIDENCES, f"{prefix}.confidence is invalid", errors)


def validate_prior_finding(item: object, prefix: str, errors: list[str]) -> None:
    require(isinstance(item, dict), f"{prefix} must be an object", errors)
    if not isinstance(item, dict):
        return
    finding_id = item.get("finding_id")
    require(
        isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
        f"{prefix}.finding_id must match F-001",
        errors,
    )
    require(isinstance(item.get("original_revision"), int), f"{prefix}.original_revision must be an integer", errors)
    require(item.get("current_status") in FIX_STATUSES, f"{prefix}.current_status is invalid", errors)
    require(isinstance(item.get("evidence"), list), f"{prefix}.evidence must be an array", errors)
    require(item.get("confidence") in CONFIDENCES, f"{prefix}.confidence is invalid", errors)


def validate_recheck_new_finding(item: object, prefix: str, errors: list[str]) -> None:
    """Enforce the narrow exception for new findings during a recheck."""
    validate_finding(item, prefix, errors)
    if not isinstance(item, dict):
        return

    require(
        item.get("severity") in {"Critical", "High"},
        f"{prefix}.severity must be High or Critical during a recheck",
        errors,
    )
    gate = item.get("recheck_gate")
    require(isinstance(gate, dict), f"{prefix}.recheck_gate must be an object", errors)
    if not isinstance(gate, dict):
        return
    for field in (
        "introduced_or_worsened_by_current_revision",
        "direct_evidence",
        "delivery_or_stability_impact",
    ):
        require(gate.get(field) is True, f"{prefix}.recheck_gate.{field} must be true", errors)


def validate(packet: dict) -> list[str]:
    errors: list[str] = []
    packet_type = packet.get("packet_type")
    require(packet_type in PACKET_ITEMS, f"invalid packet_type: {packet_type!r}", errors)

    round_number = packet.get("round")
    require(isinstance(round_number, int) and 0 <= round_number <= 3, "round must be an integer from 0 to 3", errors)
    if packet_type in {"A_INITIAL", "A_FIX_VERIFY"}:
        require(round_number == 0, f"{packet_type} round must be 0", errors)
    elif packet_type in {"B_VERIFICATION", "A_RECHECK"}:
        require(isinstance(round_number, int) and 1 <= round_number <= 3, f"{packet_type} round must be 1 to 3", errors)
    elif packet_type in {"B_FIX_VERIFICATION", "A_FIX_RECHECK"}:
        require(round_number == 1, f"{packet_type} round must be 1", errors)

    require(isinstance(packet.get("run"), dict), f"{packet_type} requires run object", errors)
    require(isinstance(packet.get("model"), dict), "model must be an object", errors)

    item_key = PACKET_ITEMS.get(packet_type)
    items = packet.get(item_key) if item_key else None
    require(isinstance(items, list), f"{item_key or 'items'} must be an array", errors)
    if not isinstance(items, list):
        return errors

    for index, item in enumerate(items):
        prefix = f"{item_key}[{index}]"
        if packet_type == "A_INITIAL":
            validate_finding(item, prefix, errors)
        elif packet_type == "A_FIX_VERIFY":
            validate_prior_finding(item, prefix, errors)
        elif packet_type == "B_VERIFICATION":
            require(isinstance(item, dict), f"{prefix} must be an object", errors)
            if not isinstance(item, dict):
                continue
            finding_id = item.get("finding_id")
            require(
                isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
                f"{prefix}.finding_id must match F-001",
                errors,
            )
            require(item.get("validity") in VALIDITIES, f"{prefix}.validity is invalid", errors)
            require(item.get("severity_decision") in SEVERITY_DECISIONS, f"{prefix}.severity_decision is invalid", errors)
            if item.get("suggested_severity") is not None:
                require(item.get("suggested_severity") in SEVERITIES, f"{prefix}.suggested_severity is invalid", errors)
        elif packet_type == "A_RECHECK":
            require(isinstance(item, dict), f"{prefix} must be an object", errors)
            if not isinstance(item, dict):
                continue
            finding_id = item.get("finding_id")
            require(
                isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
                f"{prefix}.finding_id must match F-001",
                errors,
            )
            require(item.get("response") in RESPONSES, f"{prefix}.response is invalid", errors)
            require(item.get("current_validity") in VALIDITIES, f"{prefix}.current_validity is invalid", errors)
            require(item.get("current_severity") in SEVERITIES, f"{prefix}.current_severity is invalid", errors)
            if round_number == 3 and item.get("consensus") is False:
                require(bool(item.get("final_technical_position")), f"{prefix}.final_technical_position is required for unresolved round 3", errors)
        elif packet_type == "B_FIX_VERIFICATION":
            require(isinstance(item, dict), f"{prefix} must be an object", errors)
            if not isinstance(item, dict):
                continue
            finding_id = item.get("finding_id")
            require(
                isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
                f"{prefix}.finding_id must match F-001",
                errors,
            )
            require(item.get("status_decision") in FIX_STATUS_DECISIONS, f"{prefix}.status_decision is invalid", errors)
            require(item.get("proposed_status") in FIX_STATUSES, f"{prefix}.proposed_status is invalid", errors)
            require(item.get("confidence") in CONFIDENCES, f"{prefix}.confidence is invalid", errors)
        elif packet_type == "A_FIX_RECHECK":
            require(isinstance(item, dict), f"{prefix} must be an object", errors)
            if not isinstance(item, dict):
                continue
            finding_id = item.get("finding_id")
            require(
                isinstance(finding_id, str) and re.fullmatch(r"F-\d{3,}", finding_id) is not None,
                f"{prefix}.finding_id must match F-001",
                errors,
            )
            require(item.get("response") in FIX_RESPONSES, f"{prefix}.response is invalid", errors)
            require(item.get("current_status") in FIX_STATUSES, f"{prefix}.current_status is invalid", errors)
            require(isinstance(item.get("consensus"), bool), f"{prefix}.consensus must be boolean", errors)
            if item.get("consensus") is False:
                require(bool(item.get("disagreement_reason")), f"{prefix}.disagreement_reason is required when the focused recheck remains unresolved", errors)

    if packet_type == "A_FIX_VERIFY":
        new_findings = packet.get("new_findings")
        require(isinstance(new_findings, list), "A_FIX_VERIFY.new_findings must be an array", errors)
        if isinstance(new_findings, list):
            for index, item in enumerate(new_findings):
                validate_recheck_new_finding(item, f"new_findings[{index}]", errors)
    elif packet_type in {"B_VERIFICATION", "B_FIX_VERIFICATION"}:
        supplementary = packet.get("supplementary_findings", [])
        require(isinstance(supplementary, list), f"{packet_type}.supplementary_findings must be an array", errors)
        if isinstance(supplementary, list):
            for index, item in enumerate(supplementary):
                if packet_type == "B_FIX_VERIFICATION":
                    validate_recheck_new_finding(item, f"supplementary_findings[{index}]", errors)
                else:
                    validate_finding(item, f"supplementary_findings[{index}]", errors)

    if packet_type in {"A_FIX_VERIFY", "B_FIX_VERIFICATION", "A_FIX_RECHECK"}:
        mode = packet.get("run", {}).get("mode") if isinstance(packet.get("run"), dict) else None
        require(mode in {"FIX_VERIFICATION", "INCREMENTAL_REREVIEW"}, f"{packet_type} run.mode must be a fix or incremental mode", errors)

    return errors


def main() -> int:
    try:
        packet = load_packet(sys.argv[1] if len(sys.argv) > 1 else None)
        errors = validate(packet)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        print(f"INVALID: {exc}")
        return 1

    if errors:
        for error in errors:
            print(f"INVALID: {error}")
        return 1

    print("VALID")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
