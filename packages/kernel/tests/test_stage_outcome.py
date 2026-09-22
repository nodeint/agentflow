from __future__ import annotations

import sys
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

import unittest

from agentflow_kernel.stage_outcome import parse_stage_outcome


class StageOutcomeTests(unittest.TestCase):
    def test_requires_a_declared_decision_on_complete(self) -> None:
        with self.assertRaisesRegex(ValueError, "decision: approved\\|revise"):
            parse_stage_outcome(
                "status: complete\n\nApproved.",
                required=True,
                decision_values=("approved", "revise"),
            )

    def test_accepts_a_declared_decision_header(self) -> None:
        outcome = parse_stage_outcome(
            "status: complete\ndecision: Approved\n\nlooks good",
            required=True,
            decision_values=("approved", "revise"),
        )
        self.assertEqual(outcome.status, "complete")
        self.assertEqual(outcome.decision, "approved")

    def test_rejects_an_undeclared_decision_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "Invalid workflow stage decision"):
            parse_stage_outcome(
                "status: complete\ndecision: ship-it\n\nnope",
                required=True,
                decision_values=("approved", "revise"),
            )

    def test_blocked_responses_do_not_require_a_decision(self) -> None:
        outcome = parse_stage_outcome(
            "status: blocked\nblocker: missing contract\n\nstopped",
            required=True,
            decision_values=("approved", "revise"),
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertIsNone(outcome.decision)

if __name__ == "__main__":
    unittest.main()
