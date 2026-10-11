from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import unittest

from agent.models import CaseFacts, CreditEntry, TicketStatus, Truth
from agent.policy_engine import PolicyEngine
from agent.rules import load_rules


ROOT = Path(__file__).resolve().parents[1]


def case(**changes):
    base = CaseFacts(
        request_date=date(2026, 10, 5),
        invoice_statement_date=date(2026, 10, 1),
        disputed_item="October 1 monthly recurring charge",
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
    )
    return replace(base, **changes)


class PolicyEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = PolicyEngine(load_rules(ROOT / "config" / "nimbus_policy_rules.json"))

    def test_specific_outage_track_calculates_82_50_without_goodwill_cap(self):
        result = self.engine.evaluate_service_outage(case())
        self.assertEqual(result.selected_rule_id, "RULE-3")
        self.assertEqual(result.credit_amount, Decimal("82.50"))
        self.assertNotIn("goodwill_limit", " ".join(result.approval_reasons).lower())
        self.assertEqual(result.status, "eligible_for_frontline_action")

    def test_exactly_four_hours_does_not_qualify(self):
        result = self.engine.evaluate_service_outage(case(outage_end=datetime(2026, 9, 10, 10)))
        self.assertNotEqual(result.selected_rule_id, "RULE-3")

    def test_more_than_four_hours_with_no_full_day_has_zero_credit(self):
        result = self.engine.evaluate_service_outage(case(outage_end=datetime(2026, 9, 10, 10, 1)))
        self.assertEqual(result.selected_rule_id, "RULE-3")
        self.assertEqual(result.credit_amount, Decimal("0.00"))

    def test_exact_300_and_exact_30_days_do_not_trigger_those_tier2_thresholds(self):
        start = datetime(2026, 9, 1, 6)
        result = self.engine.evaluate_service_outage(case(
            monthly_recurring_charge=Decimal("300.00"), outage_start=start,
            outage_end=start + timedelta(days=30), request_date=date(2026, 10, 1),
        ))
        self.assertEqual(result.credit_amount, Decimal("300.00"))
        self.assertEqual(result.status, "eligible_for_frontline_action")

    def test_amount_or_duration_above_outage_threshold_requires_tier2(self):
        start = datetime(2026, 9, 1, 6)
        amount = self.engine.evaluate_service_outage(case(
            monthly_recurring_charge=Decimal("300.01"), outage_start=start,
            outage_end=start + timedelta(days=30), request_date=date(2026, 10, 31),
        ))
        duration = self.engine.evaluate_service_outage(case(
            monthly_recurring_charge=Decimal("31.00"), outage_start=start,
            outage_end=start + timedelta(days=31), request_date=date(2026, 11, 1),
        ))
        self.assertEqual(amount.status, "needs_tier2_approval")
        self.assertEqual(duration.status, "needs_tier2_approval")

    def test_request_on_day_30_is_timely_and_day_31_falls_back(self):
        day30 = self.engine.evaluate_service_outage(case(request_date=date(2026, 10, 21)))
        day31 = self.engine.evaluate_service_outage(case(request_date=date(2026, 10, 22)))
        self.assertEqual(day30.deadline, date(2026, 10, 21))
        self.assertEqual(day30.selected_rule_id, "RULE-3")
        self.assertNotEqual(day31.selected_rule_id, "RULE-3")

    def test_statement_day_60_is_in_window_and_day_61_needs_tier2(self):
        day60 = self.engine.evaluate_dispute_window(case(request_date=date(2026, 11, 30)))
        day61 = self.engine.evaluate_dispute_window(case(request_date=date(2026, 12, 1)))
        self.assertEqual(day60.status, "eligible")
        self.assertEqual(day61.status, "needs_tier2_approval")

    def test_missing_ticket_and_unverifiable_ticket_are_distinct(self):
        missing = self.engine.evaluate_service_outage(case(ticket_status=TicketStatus.MISSING, ticket_id=None))
        unverifiable = self.engine.evaluate_service_outage(case(ticket_status=TicketStatus.UNVERIFIABLE))
        unknown = self.engine.evaluate_service_outage(case(ticket_status=TicketStatus.UNKNOWN))
        self.assertEqual(missing.status, "pending_missing_facts")
        self.assertEqual(unverifiable.status, "needs_tier2_approval")
        self.assertEqual(unknown.status, "pending_missing_facts")

    def test_ticket_presence_and_verification_state_must_be_consistent(self):
        claimed_missing = self.engine.evaluate_service_outage(case(ticket_status=TicketStatus.MISSING))
        verified_without_id = self.engine.evaluate_service_outage(case(ticket_id=None))
        self.assertEqual(claimed_missing.status, "pending_missing_facts")
        self.assertEqual(verified_without_id.status, "pending_missing_facts")

    def test_known_unverified_account_cannot_be_treated_as_approved(self):
        result = self.engine.evaluate_service_outage(case(account_verified=Truth.FALSE))
        self.assertEqual(result.status, "calculated_pending_checks")
        self.assertIn("account_verification", result.unresolved_checks)

    def test_unknown_repeat_and_fraud_are_pending_not_cleared(self):
        result = self.engine.evaluate_service_outage(case(
            prior_resolved_dispute=Truth.UNKNOWN, fraud_indicator=Truth.UNKNOWN,
        ))
        self.assertEqual(result.status, "calculated_pending_checks")
        self.assertIn("prior_resolved_dispute", result.unresolved_checks)
        self.assertIn("fraud_indicator", result.unresolved_checks)

    def test_known_repeat_within_90_days_or_fraud_requires_tier2(self):
        repeat = self.engine.evaluate_service_outage(case(prior_resolved_dispute=Truth.TRUE, prior_resolved_dispute_date=date(2026, 7, 7)))
        fraud = self.engine.evaluate_service_outage(case(fraud_indicator=Truth.TRUE))
        self.assertEqual(repeat.status, "needs_tier2_approval")
        self.assertEqual(fraud.status, "needs_tier2_approval")

    def test_repeat_dispute_90_day_window_is_inclusive(self):
        day90 = self.engine.evaluate_service_outage(case(prior_resolved_dispute=Truth.TRUE, prior_resolved_dispute_date=date(2026, 7, 7)))
        day91 = self.engine.evaluate_service_outage(case(prior_resolved_dispute=Truth.TRUE, prior_resolved_dispute_date=date(2026, 7, 6)))
        self.assertIn("prior_resolved_dispute", day90.approval_reasons)
        self.assertNotIn("prior_resolved_dispute", day91.approval_reasons)
        self.assertEqual(day91.status, "eligible_for_frontline_action")

    def test_known_prior_dispute_without_resolution_date_remains_pending(self):
        result = self.engine.evaluate_service_outage(case(prior_resolved_dispute=Truth.TRUE))
        self.assertEqual(result.status, "calculated_pending_checks")
        self.assertIn("prior_resolved_dispute", result.unresolved_checks)

    def test_goodwill_is_cumulative_and_excludes_prior_outage_credits(self):
        entries = (
            CreditEntry(date(2026, 4, 1), Decimal("74.00"), "general_goodwill"),
            CreditEntry(date(2026, 5, 1), Decimal("900.00"), "service_outage"),
        )
        exactly75 = self.engine.evaluate_general_goodwill(case(credit_history=entries, billing_error_confirmed=Truth.TRUE), Decimal("1.00"))
        over75 = self.engine.evaluate_general_goodwill(case(credit_history=entries, billing_error_confirmed=Truth.TRUE), Decimal("1.01"))
        self.assertEqual(exactly75.status, "eligible_for_frontline_action")
        self.assertEqual(over75.status, "needs_tier2_approval")

    def test_rolling_window_excludes_general_credits_older_than_twelve_calendar_months(self):
        stale = (CreditEntry(date(2025, 10, 4), Decimal("75.00"), "general_goodwill"),)
        result = self.engine.evaluate_general_goodwill(case(credit_history=stale, billing_error_confirmed=Truth.TRUE), Decimal("0.01"))
        self.assertEqual(result.status, "eligible_for_frontline_action")
        self.assertEqual(result.calculations["prior_general_credits"], Decimal("0.00"))

    def test_unknown_credit_history_never_promises_remaining_goodwill(self):
        result = self.engine.evaluate_general_goodwill(case(credit_history=None, billing_error_confirmed=Truth.TRUE), Decimal("20.00"))
        self.assertEqual(result.status, "pending_missing_facts")
        self.assertIn("credit_history", result.unresolved_checks)

    def test_late_confirmed_billing_error_cannot_bypass_dispute_rule(self):
        result = self.engine.evaluate_general_goodwill(case(
            invoice_statement_date=date(2026, 7, 1), disputed_item="incorrect monthly charge",
            billing_error_confirmed=Truth.TRUE,
        ), Decimal("25.00"))
        self.assertEqual(result.status, "needs_tier2_approval")
        self.assertIn("late_dispute", result.approval_reasons)
        self.assertEqual(result.calculations["days_since_statement"], 96)


if __name__ == "__main__":
    unittest.main()
