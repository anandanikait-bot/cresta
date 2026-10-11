from pathlib import Path
from dataclasses import replace
import unittest

from agent.grounding import ClaimDraft, validate_claim
from agent.evidence import EvidenceLedger
from agent.sources import load_sources


ROOT = Path(__file__).resolve().parents[1]


def validate_with_evidence(draft, source_by_id):
    ledger = EvidenceLedger(source_by_id)
    for passage_id in draft.support_ids:
        ledger.fetch_exact(passage_id, reason="grounding unit test")
    return validate_claim(draft, ledger)


class GroundingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_sources(
            ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md",
            ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md",
            ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt",
        )

    def test_tax_unknown_claim_requires_both_itemization_and_plan_scope_sources(self):
        draft = ClaimDraft(
            "tax_recalculation_unknown", "The sources do not say whether taxes are recalculated.",
            ["KB-1003", "KB-1004"],
        )
        result = validate_with_evidence(draft, self.bundle.by_id)
        self.assertEqual(result.status, "partially_grounded")

    def test_outage_amount_claim_recomputes_derived_inputs(self):
        draft = ClaimDraft("outage_amount", "The calculated outage credit is $82.50.", ["RULE-3"], {
            "monthly_recurring_charge": "225.00", "full_outage_days": 11, "amount": "82.50",
            "outage_start": "2026-09-10T06:00:00", "outage_end": "2026-09-21T06:00:00",
        })
        draft = ClaimDraft(draft.claim_type, draft.text, ["RULE-3", "KB-1004", "TRANSCRIPT-14:06"], draft.derived_inputs)
        self.assertEqual(validate_with_evidence(draft, self.bundle.by_id).status, "grounded")
        draft.derived_inputs["amount"] = "82.51"
        self.assertEqual(validate_with_evidence(draft, self.bundle.by_id).status, "unsupported")

    def test_outage_amount_requires_source_formula_to_match_the_calculator(self):
        evidence = dict(self.bundle.by_id)
        for passage_id in ("RULE-3", "KB-1004"):
            passage = evidence[passage_id]
            evidence[passage_id] = replace(passage, text=passage.text.replace("÷ 30", "÷ 31"))
        draft = ClaimDraft("outage_amount", "The outage credit is $82.50.", ["RULE-3", "KB-1004", "TRANSCRIPT-14:06"], {
            "monthly_recurring_charge": "225.00", "full_outage_days": 11, "amount": "82.50",
            "outage_start": "2026-09-10T06:00:00", "outage_end": "2026-09-21T06:00:00",
        })
        self.assertEqual(validate_with_evidence(draft, evidence).status, "unsupported")

    def test_amount_is_unsupported_when_transcript_citation_does_not_support_outage_dates(self):
        draft = ClaimDraft("outage_amount", "The amount is $82.50.", ["RULE-3", "TRANSCRIPT-14:02"], {
            "monthly_recurring_charge": "225.00", "full_outage_days": 11, "amount": "82.50",
            "outage_start": "2026-09-10T06:00:00", "outage_end": "2026-09-21T06:00:00",
        })
        result = validate_with_evidence(draft, self.bundle.by_id)
        self.assertEqual(result.status, "unsupported")

    def test_specific_deadline_recomputes_from_cited_request_and_resolution_dates(self):
        ids = ["RULE-3", "KB-1005", "TRANSCRIPT-14:06", "TRANSCRIPT-METADATA"]
        correct = ClaimDraft("deadline", "October 21 is the deadline.", ids, {
            "request_deadline": "2026-10-21", "resolution_date": "2026-09-21", "request_date": "2026-10-05", "request_day_delta": 14,
        })
        wrong = ClaimDraft("deadline", "October 22 is the deadline.", ids, {
            "request_deadline": "2026-10-22", "resolution_date": "2026-09-21", "request_date": "2026-10-05", "request_day_delta": 14,
        })
        correct = validate_with_evidence(correct, self.bundle.by_id)
        wrong = validate_with_evidence(wrong, self.bundle.by_id)
        self.assertEqual(correct.status, "grounded")
        self.assertEqual(wrong.status, "unsupported")

    def test_wrong_category_or_missing_citations_is_unsupported(self):
        wrong = ClaimDraft("outage_amount", "The outage credit is $82.50.", ["KB-1010"], {
            "monthly_recurring_charge": "225.00", "full_outage_days": 11, "amount": "82.50",
        })
        missing = ClaimDraft("deadline", "The deadline is October 21.", [], {})
        self.assertEqual(validate_with_evidence(wrong, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(missing, self.bundle.by_id).status, "unsupported")

    def test_outage_approval_claim_requires_each_authoritative_threshold_source(self):
        claim_text = (
            "Tier 2 review is required for an outage credit above $300, an outage above 30 consecutive days, "
            "or a ticket that cannot be verified. A second dispute within 90 days of a prior resolved dispute "
            "or a flagged fraud indicator also requires Tier 2 review."
        )
        complete = ClaimDraft(
            "outage_approval_conditions", claim_text,
            ["RULE-4", "RULE-5", "KB-1006", "KB-1007"],
        )
        incomplete = ClaimDraft(
            "outage_approval_conditions", claim_text,
            ["RULE-3", "KB-1004"],
        )
        complete = validate_with_evidence(complete, self.bundle.by_id)
        incomplete = validate_with_evidence(incomplete, self.bundle.by_id)
        self.assertEqual(complete.status, "grounded")
        self.assertEqual(incomplete.status, "unsupported")

    def test_account_check_claim_cannot_call_unknown_checks_clear(self):
        result = validate_with_evidence(ClaimDraft(
            "account_check_pending", "The account's repeat-dispute and fraud checks are clear and the credit is approved.",
            ["RULE-4", "RULE-5", "KB-1006", "KB-1007"],
        ), self.bundle.by_id)
        self.assertEqual(result.status, "unsupported")

    def test_outage_eligibility_claim_preserves_ticket_address_and_date_conditions(self):
        complete = ClaimDraft(
            "outage_eligibility",
            "Rule 3 requires a valid NOC outage ticket covering the service address and dates; the ticketed outage must last longer than 4 consecutive hours and the request must be made within 30 calendar days of resolution.",
            ["RULE-3", "KB-1004", "KB-1005"],
        )
        incomplete = ClaimDraft(
            "outage_eligibility",
            "The outage must be NOC-confirmed, last longer than 4 consecutive hours, and be requested within 30 calendar days of resolution.",
            ["RULE-3", "KB-1004", "KB-1005"],
        )
        self.assertEqual(validate_with_evidence(complete, self.bundle.by_id).status, "grounded")
        self.assertEqual(validate_with_evidence(incomplete, self.bundle.by_id).status, "unsupported")

    def test_outage_policy_selection_rejects_a_misapplied_goodwill_cap_claim(self):
        correct = ClaimDraft(
            "outage_policy_selection", "Rule 3 is separate and not capped by Rule 2's $75 limit.",
            ["RULE-3", "KB-1004"],
        )
        incorrect = ClaimDraft(
            "outage_policy_selection", "Rule 3 is capped by Rule 2's $75 limit.",
            ["RULE-3", "KB-1004"],
        )
        correct = validate_with_evidence(correct, self.bundle.by_id)
        incorrect = validate_with_evidence(incorrect, self.bundle.by_id)
        self.assertEqual(correct.status, "grounded")
        self.assertEqual(incorrect.status, "unsupported")

    def test_kb_claim_amount_must_match_the_cited_passage(self):
        evidence = dict(self.bundle.by_id)
        passage = evidence["KB-1008"]
        evidence["KB-1008"] = replace(passage, text=passage.text.replace("$50.00", "$20.00"))
        draft = ClaimDraft(
            "kb:KB-1008",
            "The discretionary retention offer is up to $50 for eligible customers considering cancellation.",
            ["KB-1008"],
        )
        result = validate_with_evidence(draft, evidence)
        self.assertEqual(result.status, "unsupported")

    def test_related_but_unrelated_passage_cannot_ground_tax_claim(self):
        evidence = dict(self.bundle.by_id)
        evidence["KB-1003"] = replace(evidence["KB-1003"], text="### KB-1003 — Understanding Your Nimbus Wireless Bill\nPlan changes are prorated by days active in the billing cycle.")
        draft = ClaimDraft(
            "tax_recalculation_unknown", "Taxes are itemized separately and recalculation is unspecified.",
            ["KB-1003", "KB-1004"],
        )
        result = validate_with_evidence(draft, evidence)
        self.assertEqual(result.status, "unsupported")


if __name__ == "__main__":
    unittest.main()
