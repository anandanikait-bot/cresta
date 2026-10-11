"""Threshold and scope regressions tied directly to the supplied policy text."""
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re
import unittest

from agent.models import CaseFacts, CreditEntry, TicketStatus, Truth
from agent.policy_engine import PolicyEngine
from agent.rules import load_rules, validate_rule_bindings
from agent.sources import load_sources


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md"
KB = ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md"
TRANSCRIPT = ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt"


def facts(**changes):
    base = CaseFacts(
        request_date=date(2026, 10, 5),
        invoice_statement_date=date(2026, 10, 1),
        disputed_item="specific invoice charge",
        monthly_recurring_charge=Decimal("225.00"),
        outage_start=datetime(2026, 9, 10, 6),
        outage_end=datetime(2026, 9, 21, 6),
        service_address_match=Truth.TRUE,
        ticket_id="OUT-58823",
        ticket_status=TicketStatus.VERIFIED,
        independent_ticket_verification=Truth.TRUE,
        account_verified=Truth.TRUE,
        prior_resolved_dispute=Truth.FALSE,
        fraud_indicator=Truth.FALSE,
        credit_history=(),
        billing_error_confirmed=Truth.TRUE,
    )
    return replace(base, **changes)


class PolicyComplianceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = load_rules(ROOT / "config" / "nimbus_policy_rules.json")
        cls.engine = PolicyEngine(cls.rules)
        cls.sources = load_sources(POLICY, KB, TRANSCRIPT)

    def test_configured_numbers_and_predicates_match_authoritative_sources(self):
        policy = re.sub(r"[*_`]", "", POLICY.read_text(encoding="utf-8"))
        self.assertRegex(policy, r"60 calendar days")
        self.assertRegex(policy, r"\$75\.00 per account, per rolling 12-month period")
        self.assertRegex(policy, r"longer than 4 consecutive hours")
        self.assertRegex(policy, r"within 30 calendar days of the outage's resolution date")
        self.assertRegex(policy, r"monthly recurring charge ÷ 30.*number of full days")
        self.assertRegex(policy, r"not counted against, and not capped by")
        self.assertRegex(policy, r"exceeds \$300\.00")
        self.assertRegex(policy, r"exceeds 30 consecutive days")
        self.assertRegex(policy, r"within 90 days of a prior resolved dispute")
        self.assertIn("cannot be verified at the time of the request", policy)
        self.assertIn("Suspected fraud indicators are present", policy)
        self.assertEqual(validate_rule_bindings(self.rules, self.sources), ())

    def test_rule_one_boundary_is_60_not_61_calendar_days(self):
        day60 = self.engine.evaluate_invoice_dispute(facts(request_date=date(2026, 11, 30)))
        day61 = self.engine.evaluate_invoice_dispute(facts(request_date=date(2026, 12, 1)))
        self.assertEqual(day60.status, "eligible")
        self.assertEqual(day61.status, "needs_tier2_approval")

    def test_rule_two_boundary_is_cumulative_75_and_excludes_outage_credits(self):
        entries = (
            CreditEntry(date(2026, 4, 1), Decimal("74.00"), "general_goodwill"),
            CreditEntry(date(2026, 5, 1), Decimal("900.00"), "service_outage"),
        )
        exact = self.engine.evaluate_general_goodwill(facts(credit_history=entries), Decimal("1.00"))
        over = self.engine.evaluate_general_goodwill(facts(credit_history=entries), Decimal("1.01"))
        self.assertEqual(exact.status, "eligible_for_frontline_action")
        self.assertEqual(over.status, "needs_tier2_approval")
        self.assertEqual(exact.calculations["prior_general_credits"], Decimal("74.00"))
        self.assertEqual(over.calculations["proposed_total"], Decimal("75.01"))

    def test_rule_two_rolling_year_boundary_is_calendar_based_and_inclusive(self):
        boundary = (CreditEntry(date(2025, 10, 5), Decimal("75.00"), "general_goodwill"),)
        before = (CreditEntry(date(2025, 10, 4), Decimal("75.00"), "general_goodwill"),)
        included = self.engine.evaluate_general_goodwill(facts(credit_history=boundary), Decimal("0.01"))
        excluded = self.engine.evaluate_general_goodwill(facts(credit_history=before), Decimal("0.01"))
        self.assertEqual(included.calculations["prior_general_credits"], Decimal("75.00"))
        self.assertEqual(included.status, "needs_tier2_approval")
        self.assertEqual(excluded.calculations["prior_general_credits"], Decimal("0.00"))

    def test_rule_three_duration_deadline_formula_and_goodwill_exception(self):
        four_hours = self.engine.evaluate_service_outage(facts(outage_end=datetime(2026, 9, 10, 10)))
        thirty_days = self.engine.evaluate_service_outage(facts(request_date=date(2026, 10, 21)))
        thirty_one_days = self.engine.evaluate_service_outage(facts(request_date=date(2026, 10, 22)))
        self.assertNotEqual(four_hours.selected_rule_id, "RULE-3")
        self.assertEqual(thirty_days.deadline, date(2026, 10, 21))
        self.assertEqual(thirty_days.credit_amount, Decimal("82.50"))
        self.assertEqual(thirty_days.status, "eligible_for_frontline_action")
        self.assertNotEqual(thirty_one_days.selected_rule_id, "RULE-3")
        self.assertEqual(thirty_days.calculations["formula"], "quantize_cents((monthly_recurring_charge / Decimal('30')) * full_outage_days)")
        self.assertIn("RULE-2 cumulative $75 limit does not apply", thirty_days.exclusions[0])

    def test_rule_four_outage_limits_are_strictly_above_300_and_30_days(self):
        start = datetime(2026, 9, 1, 6)
        exactly_300_and_30 = self.engine.evaluate_service_outage(facts(
            monthly_recurring_charge=Decimal("300.00"), outage_start=start,
            outage_end=start + timedelta(days=30), request_date=date(2026, 10, 1),
        ))
        over_300 = self.engine.evaluate_service_outage(facts(
            monthly_recurring_charge=Decimal("300.01"), outage_start=start,
            outage_end=start + timedelta(days=30), request_date=date(2026, 10, 31),
        ))
        over_30_days = self.engine.evaluate_service_outage(facts(
            monthly_recurring_charge=Decimal("31.00"), outage_start=start,
            outage_end=start + timedelta(days=31), request_date=date(2026, 11, 1),
        ))
        self.assertEqual(exactly_300_and_30.credit_amount, Decimal("300.00"))
        self.assertTrue(exactly_300_and_30.issuance_authorized)
        self.assertIn("outage_credit_above_300", over_300.approval_reasons)
        self.assertIn("outage_duration_above_30_days", over_30_days.approval_reasons)

    def test_ticket_dispute_history_and_fraud_gates_preserve_unknowns(self):
        unverifiable = self.engine.evaluate_service_outage(facts(
            ticket_status=TicketStatus.UNVERIFIABLE,
            independent_ticket_verification=Truth.FALSE,
        ))
        ticket_unknown = self.engine.evaluate_service_outage(facts(
            ticket_status=TicketStatus.AGENT_REPORTED,
            independent_ticket_verification=Truth.UNKNOWN,
        ))
        day90 = self.engine.evaluate_service_outage(facts(
            prior_resolved_dispute=Truth.TRUE, prior_resolved_dispute_date=date(2026, 7, 7),
        ))
        day91 = self.engine.evaluate_service_outage(facts(
            prior_resolved_dispute=Truth.TRUE, prior_resolved_dispute_date=date(2026, 7, 6),
        ))
        fraud = self.engine.evaluate_service_outage(facts(fraud_indicator=Truth.TRUE))
        fraud_unknown = self.engine.evaluate_service_outage(facts(fraud_indicator=Truth.UNKNOWN))
        self.assertEqual(unverifiable.status, "needs_tier2_approval")
        self.assertFalse(unverifiable.issuance_authorized)
        self.assertEqual(ticket_unknown.status, "calculated_pending_checks")
        self.assertFalse(ticket_unknown.issuance_authorized)
        self.assertIn("prior_resolved_dispute", day90.approval_reasons)
        self.assertNotIn("prior_resolved_dispute", day91.approval_reasons)
        self.assertEqual(fraud.status, "needs_tier2_approval")
        self.assertEqual(fraud_unknown.status, "calculated_pending_checks")


if __name__ == "__main__":
    unittest.main()
