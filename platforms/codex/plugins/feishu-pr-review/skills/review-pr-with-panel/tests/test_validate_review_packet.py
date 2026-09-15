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
        "scope": {
            "deployment_context": {
                "topology": "distributed",
                "evidence": ["deployment manifest declares multiple replicas"],
                "affected_components": ["router", "core"],
                "risk_areas_checked": ["ownership-routing"],
                "distributed_impact": "SAFE_WITH_EVIDENCE",
                "limitations": [],
            }
        },
        "prior_findings": [],
        "new_findings": new_findings,
    }


def finding(severity: str, gate: dict | None = None) -> dict:
    value = {
        "finding_id": "F-101",
        "revision": 1,
        "title": "Delivery blocker",
        "locations": [],
        "change_attribution": {
            "classification": "INTRODUCED",
            "base_behavior": "The base revision preserves the primary request path.",
            "head_behavior": "The current revision breaks the primary request path.",
            "causal_hunks": [
                {"path": "src/service.py", "line_start": 42, "line_end": 44}
            ],
            "causal_link": "The changed branch now rejects every request.",
            "scope_obligation": None,
        },
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
    def test_requires_deployment_context_for_agent_a_packet(self) -> None:
        packet = fix_packet([])
        packet["scope"] = {}

        errors = validate(packet)

        self.assertTrue(any("deployment_context must be an object" in error for error in errors))

    def test_rejects_invalid_distributed_impact(self) -> None:
        packet = fix_packet([])
        packet["scope"]["deployment_context"]["distributed_impact"] = "LOOKS_FINE"

        errors = validate(packet)

        self.assertTrue(any("distributed_impact is invalid" in error for error in errors))

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


class ChangeAttributionTests(unittest.TestCase):
    @staticmethod
    def b_packet(attribution_decision: str, validity: str) -> dict:
        return {
            "packet_type": "B_VERIFICATION",
            "round": 1,
            "run": {"mode": "INITIAL_REVIEW"},
            "model": {},
            "reviews": [
                {
                    "finding_id": "F-001",
                    "validity": validity,
                    "attribution_decision": attribution_decision,
                    "attribution_evidence": "Base and head were compared independently.",
                    "severity_decision": "NOT_APPLICABLE",
                }
            ],
            "supplementary_findings": [],
        }

    def test_rejects_finding_without_change_attribution(self) -> None:
        candidate = finding("High")
        candidate.pop("change_attribution")

        errors = validate(fix_packet([candidate]))

        self.assertTrue(any("change_attribution is required" in error for error in errors))

    def test_rejects_pre_existing_as_actionable_attribution(self) -> None:
        candidate = finding("High")
        candidate["change_attribution"]["classification"] = "PRE_EXISTING"

        errors = validate(fix_packet([candidate]))

        self.assertTrue(any("classification is invalid" in error for error in errors))

    def test_contract_incomplete_requires_specific_scope_obligation(self) -> None:
        candidate = finding("High")
        candidate["change_attribution"]["classification"] = "CONTRACT_INCOMPLETE"
        candidate["change_attribution"]["scope_obligation"] = None

        errors = validate(fix_packet([candidate]))

        self.assertTrue(any("scope_obligation is required" in error for error in errors))

    def test_b_rejecting_pre_existing_finding_must_reject_validity(self) -> None:
        errors = validate(self.b_packet("REJECT_PRE_EXISTING", "CONFIRMED"))

        self.assertTrue(any("validity must be REJECTED" in error for error in errors))

    def test_b_can_reject_pre_existing_finding_consistently(self) -> None:
        self.assertEqual(
            validate(self.b_packet("REJECT_PRE_EXISTING", "REJECTED")), []
        )


class AgentAFinalPositionTests(unittest.TestCase):
    @staticmethod
    def packet(
        *,
        response: str,
        final_position: str | None,
        consensus: bool = False,
        current_validity: str = "CONFIRMED",
    ) -> dict:
        return {
            "packet_type": "A_RECHECK",
            "round": 1,
            "run": {"mode": "INITIAL_REVIEW"},
            "model": {},
            "responses": [
                {
                    "finding_id": "F-001",
                    "response": response,
                    "current_validity": current_validity,
                    "current_severity": "High",
                    "consensus": consensus,
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
            self.packet(
                response="NEED_MORE_EVIDENCE",
                final_position=None,
                current_validity="INSUFFICIENT_EVIDENCE",
            )
        )

        self.assertEqual(errors, [])

    def test_consensus_withdrawal_still_requires_final_position(self) -> None:
        errors = validate(
            self.packet(
                response="WITHDRAW",
                final_position=None,
                consensus=True,
            )
        )

        self.assertTrue(
            any("final_technical_position is required" in error for error in errors)
        )

    def test_consensus_rejection_still_requires_final_position(self) -> None:
        errors = validate(
            self.packet(
                response="REJECT_WITH_EVIDENCE",
                final_position=None,
                consensus=True,
            )
        )

        self.assertTrue(
            any("final_technical_position is required" in error for error in errors)
        )

    def test_response_consensus_transition_table(self) -> None:
        cases = {
            "ACCEPT": (True, None, "CONFIRMED"),
            "PARTIAL_ACCEPT": (
                False,
                "A accepts only the severity adjustment.",
                "PARTIALLY_CONFIRMED",
            ),
            "REJECT_WITH_EVIDENCE": (
                False,
                "The rebuttal path is unreachable.",
                "CONFIRMED",
            ),
            "NEED_MORE_EVIDENCE": (False, None, "INSUFFICIENT_EVIDENCE"),
            "WITHDRAW": (
                True,
                "B's rebuttal invalidates the original finding.",
                "REJECTED",
            ),
        }

        for response, (expected_consensus, final_position, current_validity) in cases.items():
            with self.subTest(response=response, expected="valid"):
                self.assertEqual(
                    validate(
                        self.packet(
                            response=response,
                            final_position=final_position,
                            consensus=expected_consensus,
                            current_validity=current_validity,
                        )
                    ),
                    [],
                )
            with self.subTest(response=response, expected="invalid_consensus"):
                errors = validate(
                    self.packet(
                        response=response,
                        final_position=final_position or "Not a valid final position here.",
                        consensus=not expected_consensus,
                        current_validity=current_validity,
                    )
                )
                self.assertTrue(any("consensus must be" in error for error in errors))

    def test_accept_cannot_carry_a_non_consensus_final_position(self) -> None:
        errors = validate(
            self.packet(
                response="ACCEPT",
                final_position="This field would contradict an agreed acceptance.",
                consensus=True,
            )
        )

        self.assertTrue(
            any("final_technical_position must be empty" in error for error in errors)
        )

    def test_response_validity_transition_table(self) -> None:
        cases = {
            "ACCEPT": ({"CONFIRMED", "PARTIALLY_CONFIRMED"}, True, None),
            "PARTIAL_ACCEPT": (
                {"CONFIRMED", "PARTIALLY_CONFIRMED"},
                False,
                "A accepts only part of B's adjustment.",
            ),
            "REJECT_WITH_EVIDENCE": (
                {"CONFIRMED", "PARTIALLY_CONFIRMED"},
                False,
                "The rebuttal path is unreachable.",
            ),
            "NEED_MORE_EVIDENCE": ({"INSUFFICIENT_EVIDENCE"}, False, None),
            "WITHDRAW": ({"REJECTED"}, True, "B's evidence invalidates the finding."),
        }

        for response, (allowed_validities, consensus, final_position) in cases.items():
            for current_validity in {
                "CONFIRMED",
                "PARTIALLY_CONFIRMED",
                "REJECTED",
                "INSUFFICIENT_EVIDENCE",
            }:
                with self.subTest(response=response, current_validity=current_validity):
                    errors = validate(
                        self.packet(
                            response=response,
                            final_position=final_position,
                            consensus=consensus,
                            current_validity=current_validity,
                        )
                    )
                    if current_validity in allowed_validities:
                        self.assertEqual(errors, [])
                    else:
                        self.assertTrue(
                            any("current_validity must be" in error for error in errors)
                        )


if __name__ == "__main__":
    unittest.main()
