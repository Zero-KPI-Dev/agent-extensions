from __future__ import annotations

import sys
import unittest
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

from validate_review_packet import validate  # noqa: E402


def fix_packet(new_findings: list[dict]) -> dict:
    return {
        "packet_type": "A_FIX_VERIFY",
        "round": 0,
        "run": {"mode": "FIX_VERIFICATION"},
        "model": {},
        "scope": {},
        "prior_findings": [],
        "new_findings": new_findings,
    }


def finding(severity: str, gate: dict | None = None) -> dict:
    value = {
        "finding_id": "F-101",
        "revision": 1,
        "title": "Delivery blocker",
        "locations": [],
        "claim": "Current revision breaks the primary request path.",
        "evidence": [],
        "impact": "Primary functionality cannot be delivered.",
        "severity": severity,
        "severity_rationale": "The main path fails for all callers.",
        "verification": "Run the focused request-path test.",
        "confidence": "high",
    }
    if gate is not None:
        value["recheck_gate"] = gate
    return value


class RecheckFindingGateTests(unittest.TestCase):
    def test_empty_new_findings_is_valid(self) -> None:
        self.assertEqual(validate(fix_packet([])), [])

    def test_rejects_medium_new_finding_during_recheck(self) -> None:
        errors = validate(
            fix_packet(
                [
                    finding(
                        "Medium",
                        {
                            "introduced_or_worsened_by_current_revision": True,
                            "direct_evidence": True,
                            "delivery_or_stability_impact": True,
                        },
                    )
                ]
            )
        )
        self.assertTrue(any("must be High or Critical" in error for error in errors))

    def test_accepts_high_finding_with_complete_gate(self) -> None:
        errors = validate(
            fix_packet(
                [
                    finding(
                        "High",
                        {
                            "introduced_or_worsened_by_current_revision": True,
                            "direct_evidence": True,
                            "delivery_or_stability_impact": True,
                        },
                    )
                ]
            )
        )
        self.assertEqual(errors, [])

    def test_rejects_missing_gate(self) -> None:
        errors = validate(fix_packet([finding("High")]))
        self.assertTrue(any("recheck_gate must be an object" in error for error in errors))

    def test_rejects_a_second_fix_recheck_round(self) -> None:
        packet = {
            "packet_type": "A_FIX_RECHECK",
            "round": 2,
            "run": {"mode": "FIX_VERIFICATION"},
            "model": {},
            "responses": [],
        }
        errors = validate(packet)
        self.assertTrue(any("round must be 1" in error for error in errors))

    def test_fix_recheck_cannot_use_final_by_a_to_close_an_old_finding(self) -> None:
        packet = {
            "packet_type": "A_FIX_RECHECK",
            "round": 1,
            "run": {"mode": "FIX_VERIFICATION"},
            "model": {},
            "responses": [
                {
                    "finding_id": "F-001",
                    "response": "ACCEPT_CLOSURE",
                    "current_status": "FINAL_BY_A",
                    "consensus": False,
                    "disagreement_reason": "B still reaches the original trigger path.",
                }
            ],
        }

        errors = validate(packet)

        self.assertTrue(any("current_status is invalid" in error for error in errors))


class AgentAFinalPositionTests(unittest.TestCase):
    @staticmethod
    def packet(*, response: str, final_position: str | None) -> dict:
        return {
            "packet_type": "A_RECHECK",
            "round": 1,
            "run": {"mode": "INITIAL_REVIEW"},
            "model": {},
            "responses": [
                {
                    "finding_id": "F-001",
                    "response": response,
                    "current_validity": "CONFIRMED",
                    "current_severity": "High",
                    "consensus": False,
                    "final_technical_position": final_position,
                }
            ],
        }

    def test_definitive_round_one_response_requires_final_position(self) -> None:
        errors = validate(
            self.packet(response="REJECT_WITH_EVIDENCE", final_position=None)
        )

        self.assertTrue(
            any("final_technical_position is required" in error for error in errors)
        )

    def test_definitive_round_one_response_accepts_final_position(self) -> None:
        errors = validate(
            self.packet(
                response="REJECT_WITH_EVIDENCE",
                final_position="The counterexample is unreachable because the caller validates X.",
            )
        )

        self.assertEqual(errors, [])

    def test_need_more_evidence_does_not_pretend_to_be_final(self) -> None:
        errors = validate(
            self.packet(response="NEED_MORE_EVIDENCE", final_position=None)
        )

        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
