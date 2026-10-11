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

    def test_outage_amount_claim_must_match_the_amount_rendered_to_the_customer(self):
        ids = ["RULE-3", "KB-1004", "TRANSCRIPT-14:06"]
        inputs = {
            "monthly_recurring_charge": "225.00", "full_outage_days": 11, "amount": "82.50",
            "outage_start": "2026-09-10T06:00:00", "outage_end": "2026-09-21T06:00:00",
        }
        incorrect = ClaimDraft("outage_amount", "The customer will receive $999.00.", ids, inputs)
        promised = ClaimDraft("outage_amount", "The customer will receive $82.50.", ids, inputs)
        possible_issuance = ClaimDraft("outage_amount", "The calculated amount is $82.50 and the customer may receive it.", ids, inputs)
        conflicting_amounts = ClaimDraft("outage_amount", "The calculated service-outage amount is $225.00, not $82.50.", ids, inputs)
        already_issued = ClaimDraft("outage_amount", "The calculated service-outage amount is $82.50; billing already posted it to the account.", ids, inputs)
        processed = ClaimDraft("outage_amount", "The calculated service-outage amount is $82.50, and the refund was processed.", ids, inputs)
        malformed_amount = ClaimDraft("outage_amount", "The calculated outage amount is $82.500.", ids, inputs)
        missing_amount = ClaimDraft("outage_amount", "The outage credit was calculated.", ids, inputs)
        correct = ClaimDraft("outage_amount", "The calculated service-outage amount is $82.50.", ids, inputs)
        self.assertEqual(validate_with_evidence(incorrect, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(promised, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(possible_issuance, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(conflicting_amounts, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(already_issued, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(processed, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(malformed_amount, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(missing_amount, self.bundle.by_id).status, "unsupported")
        self.assertEqual(validate_with_evidence(correct, self.bundle.by_id).status, "grounded")

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
        correct = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is 14 days later and timely; October 21, 2026 is the deadline.", ids, {
            "request_deadline": "2026-10-21", "resolution_date": "2026-09-21", "request_date": "2026-10-05", "request_day_delta": 14,
        })
        wrong = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is 14 days later and timely; October 22, 2026 is the deadline.", ids, {
            "request_deadline": "2026-10-22", "resolution_date": "2026-09-21", "request_date": "2026-10-05", "request_day_delta": 14,
        })
        untimely = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is not timely; October 21, 2026 is the deadline.", ids, correct.derived_inputs)
        wrong_elapsed = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is 15 days later and timely; October 21, 2026 is the deadline.", ids, correct.derived_inputs)
        contradictory_elapsed = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is 40 days later and timely; October 21, 2026 is the deadline.", ids, correct.derived_inputs)
        uncertain_timing = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, it is unknown whether the October 5, 2026 request is timely; October 21, 2026 is the deadline.", ids, correct.derived_inputs)
        wrong_year = ClaimDraft("deadline", "Within 30 calendar days of the September 21, 2026 resolution, the October 5, 2026 request is 14 days later and timely; October 21, 2025 is the deadline.", ids, correct.derived_inputs)
        correct = validate_with_evidence(correct, self.bundle.by_id)
        wrong = validate_with_evidence(wrong, self.bundle.by_id)
        untimely = validate_with_evidence(untimely, self.bundle.by_id)
        wrong_elapsed = validate_with_evidence(wrong_elapsed, self.bundle.by_id)
        contradictory_elapsed = validate_with_evidence(contradictory_elapsed, self.bundle.by_id)
        uncertain_timing = validate_with_evidence(uncertain_timing, self.bundle.by_id)
        wrong_year = validate_with_evidence(wrong_year, self.bundle.by_id)
        self.assertEqual(correct.status, "grounded")
        self.assertEqual(wrong.status, "unsupported")
        self.assertEqual(untimely.status, "unsupported")
        self.assertEqual(wrong_elapsed.status, "unsupported")
        self.assertEqual(contradictory_elapsed.status, "unsupported")
        self.assertEqual(uncertain_timing.status, "unsupported")
        self.assertEqual(wrong_year.status, "unsupported")

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

    def test_adversarial_policy_claims_are_rejected_even_with_real_citations(self):
        cases = (
            ClaimDraft("general_goodwill", "The $75 goodwill cap applies per incident.", ["RULE-2", "KB-1002"]),
            ClaimDraft("outage_approval_conditions", "Outage credits over $75 automatically require Tier 2 approval.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("tax_recalculation_unknown", "Taxes are always refunded when an outage credit is issued.", ["KB-1003", "KB-1004"]),
            ClaimDraft("tax_recalculation_unknown", "The source does not specify whether taxes are recalculated; taxes are guaranteed to be refunded.", ["KB-1003", "KB-1004"]),
            ClaimDraft("ticket_approval_unknown", "The reported ticket does not establish credit approval; no credit has been issued, but the credit is approved.", ["RULE-4", "KB-1006", "TRANSCRIPT-14:07"]),
            ClaimDraft("deadline", "Rule 3 allows 60 days after an outage.", ["RULE-3", "KB-1005"], {"request_deadline": "60 calendar days"}),
            ClaimDraft("kb:KB-1010", "The KB-1010 plan-change proration formula authorizes an outage refund.", ["KB-1010"]),
            ClaimDraft("account_check_pending", "Fraud status is unknown, and no fraud was found.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Checks remain unknown, but the customer has received their credit.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Fraud and repeat-dispute checks are unknown, but the customer already received the outage credit.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Repeat-dispute checks are pending; billing already posted $82.50 to the account.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Repeat-dispute checks are pending; the $82.50 credit was processed and is reflected in the account.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Fraud checks are unknown, but the customer is credited $82.50 for the outage.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("ticket_approval_unknown", "Authorization is not established by the ticket, but the refund was processed.", ["RULE-4", "KB-1006"]),
            ClaimDraft("credit_issuance_status", "The customer has received their credit.", ["RULE-3", "KB-1004", "TRANSCRIPT-14:07"]),
        )
        for draft in cases:
            with self.subTest(claim_type=draft.claim_type, text=draft.text):
                self.assertEqual(validate_with_evidence(draft, self.bundle.by_id).status, "unsupported")

    def test_valid_conditional_policy_claims_remain_grounded(self):
        cases = (
            ClaimDraft("general_goodwill", "A confirmed general billing error may qualify for a cumulative $75 limit per account in a rolling 12-month period.", ["RULE-2", "KB-1002"]),
            ClaimDraft("general_goodwill", "A confirmed billing error has a cumulative $75 account total over a rolling 12-month period; it is not a per-incident allowance.", ["RULE-2", "KB-1002"]),
            ClaimDraft("outage_approval_conditions", "Tier 2 review is required for an outage credit above $300, an outage above 30 consecutive days, or a ticket that cannot be verified. A second dispute within 90 days of a prior resolved dispute or a flagged fraud indicator also requires Tier 2 review.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("tax_recalculation_unknown", "Taxes and surcharges are itemized separately; the supplied sources do not say whether they are recalculated after an outage credit.", ["KB-1003", "KB-1004"]),
            ClaimDraft("account_check_pending", "Fraud and repeat-dispute checks are unknown; no credit has been issued.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
            ClaimDraft("account_check_pending", "Fraud and repeat-dispute checks remain unknown; no outage credit has been issued.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]),
        )
        for draft in cases:
            with self.subTest(claim_type=draft.claim_type):
                self.assertNotEqual(validate_with_evidence(draft, self.bundle.by_id).status, "unsupported")


if __name__ == "__main__":
    unittest.main()
