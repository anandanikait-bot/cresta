from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from .calculators import calendar_months_ago, outage_credit
from .models import CaseFacts, Decision, TicketStatus, Truth
from .rules import RuleSet


class PolicyEngine:
    def __init__(self, rules: RuleSet):
        self.rules = rules

    def evaluate_dispute_window(self, facts: CaseFacts) -> Decision:
        if facts.request_date is None or facts.invoice_statement_date is None or not facts.disputed_item:
            return Decision("RULE-1", "pending_missing_facts", reason="Need the invoice statement date and specific disputed charge.", unresolved_checks=("invoice_statement_date", "disputed_item"))
        elapsed = (facts.request_date - facts.invoice_statement_date).days
        if elapsed < 0:
            return Decision("RULE-1", "pending_missing_facts", reason="The request date precedes the invoice statement date.", unresolved_checks=("request_date",))
        limit = int(self.rules["RULE-1"]["dispute_window_calendar_days"])
        if elapsed <= limit:
            return Decision("RULE-1", "eligible", reason=f"Specific disputed item is within {limit} calendar days of the statement date.", calculations={"days_since_statement": elapsed, "window_days": limit})
        return Decision("RULE-1", "needs_tier2_approval", reason=f"The request is {elapsed} days after the statement date, beyond the {limit}-day window; Tier 2 may consider it.", approval_reasons=("late_dispute",), calculations={"days_since_statement": elapsed, "window_days": limit})

    def evaluate_invoice_dispute(self, facts: CaseFacts) -> Decision:
        """Evaluate Rule 1 only when the customer is disputing an invoice charge."""
        return self.evaluate_dispute_window(facts)

    def evaluate_service_outage(self, facts: CaseFacts, invoice_dispute: Decision | None = None) -> Decision:
        if facts.ticket_status == TicketStatus.UNVERIFIABLE:
            return Decision("RULE-3", "needs_tier2_approval", reason="The outage ticket cannot be verified at request time.", approval_reasons=("unverifiable_ticket",))
        if facts.ticket_status in (TicketStatus.UNKNOWN, TicketStatus.MISSING):
            if facts.ticket_status == TicketStatus.MISSING and facts.ticket_id is None:
                return self._fallback_to_goodwill(facts, "No NOC ticket exists; Rule 3 does not apply.", invoice_dispute=invoice_dispute)
            return Decision("RULE-3", "pending_missing_facts", reason="The NOC ticket status or ticket number is not documented consistently.", unresolved_checks=("ticket_status",))
        if not facts.ticket_id:
            return Decision("RULE-3", "pending_missing_facts", reason="The NOC ticket number is required.", unresolved_checks=("ticket_id",))
        if facts.independent_ticket_verification == Truth.FALSE:
            return Decision(
                "RULE-3", "needs_tier2_approval",
                reason="The reported NOC ticket could not be independently verified.",
                approval_reasons=("unverifiable_ticket",),
            )
        if facts.service_address_match == Truth.FALSE:
            return self._fallback_to_goodwill(facts, "The ticket does not cover the customer's service address.", invoice_dispute=invoice_dispute)
        if facts.service_address_match == Truth.UNKNOWN:
            return Decision("RULE-3", "pending_missing_facts", reason="The ticket's service-address coverage is unknown.", unresolved_checks=("service_address_match",))
        duration = facts.outage_duration_hours
        request_delta = facts.request_days_after_resolution
        if duration is None or facts.outage_end is None:
            return Decision("RULE-3", "pending_missing_facts", reason="The outage dates and duration are required.", unresolved_checks=("outage_start", "outage_end"))
        min_hours = Decimal(str(self.rules["RULE-3"]["minimum_duration_hours_exclusive"]))
        if duration <= min_hours:
            return self._fallback_to_goodwill(
                facts,
                f"The outage lasted {duration} hours, which does not exceed the Rule 3 threshold of {min_hours} hours.",
                invoice_dispute=invoice_dispute,
            )
        if request_delta is None:
            return Decision("RULE-3", "pending_missing_facts", reason="The outage resolution date and request date are required.", unresolved_checks=("request_date", "outage_resolution_date"))
        if request_delta < 0:
            return Decision("RULE-3", "pending_missing_facts", reason="The request date precedes the reported outage resolution date.", unresolved_checks=("request_date",))
        request_window = int(self.rules["RULE-3"]["request_window_calendar_days_inclusive"])
        deadline = facts.outage_end.date() + timedelta(days=request_window)
        if request_delta > request_window:
            return self._fallback_to_goodwill(
                facts,
                f"The request is {request_delta} days after resolution, outside Rule 3's {request_window}-day window.",
                deadline=deadline,
                invoice_dispute=invoice_dispute,
            )
        if facts.monthly_recurring_charge is None:
            return Decision("RULE-3", "pending_missing_facts", reason="The current monthly recurring charge is required for the calculation.", deadline=deadline, unresolved_checks=("monthly_recurring_charge",))
        full_days = facts.full_outage_days or 0
        amount = outage_credit(facts.monthly_recurring_charge, full_days)
        calculations = {
            "monthly_recurring_charge": facts.monthly_recurring_charge,
            "full_outage_days": full_days,
            "outage_duration_hours": duration,
            "formula": "quantize_cents((monthly_recurring_charge / Decimal('30')) * full_outage_days)",
            "result": amount,
            "request_days_after_resolution": request_delta,
            "request_deadline": deadline,
        }
        approval: list[str] = []
        unresolved: list[str] = []
        max_credit = Decimal(str(self.rules["RULE-4"]["outage_credit_tier2_exclusive"]))
        max_hours = Decimal(int(self.rules["RULE-4"]["outage_duration_days_tier2_exclusive"]) * 24)
        calculations["outage_amount_tier2_threshold"] = max_credit
        calculations["outage_duration_days_tier2_threshold"] = self.rules["RULE-4"]["outage_duration_days_tier2_exclusive"]
        calculations["outage_amount_tier2_triggered"] = amount > max_credit
        calculations["outage_duration_tier2_triggered"] = duration > max_hours
        if amount > max_credit:
            approval.append("outage_credit_above_300")
        if duration > max_hours:
            approval.append("outage_duration_above_30_days")
        self._review_repeat_dispute(facts, approval, unresolved)
        self._review_status(facts.fraud_indicator, "fraud_indicator", approval, unresolved)
        if facts.ticket_status != TicketStatus.VERIFIED or facts.independent_ticket_verification != Truth.TRUE:
            unresolved.append("independent_ticket_verification")
        if facts.account_verified != Truth.TRUE:
            unresolved.append("account_verification")
        if approval:
            status = "needs_tier2_approval"
        elif unresolved:
            status = "calculated_pending_checks"
        else:
            status = "eligible_for_frontline_action"
        return Decision(
            "RULE-3", status, amount, deadline,
            "The specific service-outage track governs; Rule 2's $75 goodwill cap is excluded.",
            exclusions=("RULE-2 cumulative $75 limit does not apply to a RULE-3 outage credit",),
            approval_reasons=tuple(dict.fromkeys(approval)),
            unresolved_checks=tuple(dict.fromkeys(unresolved)), calculations=calculations,
        )

    def evaluate_general_goodwill(
        self, facts: CaseFacts, requested_amount: Decimal | None = None,
        invoice_dispute: Decision | None = None,
    ) -> Decision:
        """Evaluate Rule 2; pass a Rule 1 result only for an actual invoice dispute."""
        if facts.billing_error_confirmed == Truth.FALSE:
            return Decision("RULE-2", "ineligible", reason="Rule 2 requires a confirmed general billing error.")
        if facts.billing_error_confirmed == Truth.UNKNOWN:
            return Decision("RULE-2", "pending_missing_facts", reason="A confirmed billing error and remaining credit history are required before quoting a goodwill amount.", unresolved_checks=("billing_error_confirmed", "credit_history"))
        if invoice_dispute is not None and invoice_dispute.status == "pending_missing_facts":
            return Decision(
                "RULE-2", "pending_missing_facts",
                reason="Rule 1 timing cannot be assessed before considering general goodwill.",
                unresolved_checks=tuple(dict.fromkeys(invoice_dispute.unresolved_checks)),
                calculations={"rule1_status": invoice_dispute.status},
            )
        if facts.request_date is None or facts.credit_history is None:
            return Decision("RULE-2", "pending_missing_facts", reason="The request date and account credit history are required to determine the rolling total.", unresolved_checks=("credit_history",))
        amount = requested_amount if requested_amount is not None else facts.requested_goodwill_amount
        if amount is None:
            return Decision("RULE-2", "pending_missing_facts", reason="The requested goodwill amount is not known.", unresolved_checks=("requested_goodwill_amount",))
        if amount < 0:
            return Decision("RULE-2", "ineligible", reason="A requested credit cannot be negative.")
        months = int(self.rules["RULE-2"]["rolling_period_months"])
        lower_bound = calendar_months_ago(facts.request_date, months)
        prior = sum((entry.amount for entry in facts.credit_history if entry.category == "general_goodwill" and lower_bound <= entry.issued_date <= facts.request_date), Decimal("0.00"))
        proposed_total = prior + amount
        ceiling = Decimal(self.rules["RULE-2"]["cumulative_limit"])
        approval: list[str] = []
        unresolved: list[str] = []
        if invoice_dispute is not None and invoice_dispute.status == "needs_tier2_approval":
            approval.extend(invoice_dispute.approval_reasons)
        rule1_calculations = (
            {"rule1_status": invoice_dispute.status, **invoice_dispute.calculations}
            if invoice_dispute is not None else {}
        )
        if proposed_total > ceiling:
            approval.append("general_goodwill_above_75")
        self._review_repeat_dispute(facts, approval, unresolved)
        self._review_status(facts.fraud_indicator, "fraud_indicator", approval, unresolved)
        if facts.account_verified != Truth.TRUE:
            unresolved.append("account_verification")
        status = "needs_tier2_approval" if approval else "calculated_pending_checks" if unresolved else "eligible_for_frontline_action"
        reason = "A request beyond Rule 1's 60-day dispute window may only be considered with Tier 2 sign-off." if "late_dispute" in approval else "The general goodwill limit is cumulative and applies only when no more-specific policy governs."
        return Decision("RULE-2", status, amount, reason=reason, approval_reasons=tuple(dict.fromkeys(approval)), unresolved_checks=tuple(dict.fromkeys(unresolved)), calculations={"prior_general_credits": prior, "requested_amount": amount, "proposed_total": proposed_total, "limit": ceiling, **rule1_calculations})

    def _fallback_to_goodwill(
        self, facts: CaseFacts, reason: str, deadline: date | None = None,
        invoice_dispute: Decision | None = None,
    ) -> Decision:
        goodwill = self.evaluate_general_goodwill(facts, invoice_dispute=invoice_dispute)
        return replace(goodwill, deadline=deadline, reason=reason + " " + goodwill.reason, exclusions=("Rule 3 conditions are not met; a separate confirmed Rule 2 billing error and credit-history review are still required.",))

    def _review_status(self, value: Truth, name: str, approval: list[str], unresolved: list[str]) -> None:
        if value == Truth.TRUE:
            approval.append(name)
        elif value == Truth.UNKNOWN:
            unresolved.append(name)

    def _review_repeat_dispute(self, facts: CaseFacts, approval: list[str], unresolved: list[str]) -> None:
        if facts.prior_resolved_dispute == Truth.FALSE:
            return
        if facts.prior_resolved_dispute == Truth.UNKNOWN or facts.prior_resolved_dispute_date is None or facts.request_date is None:
            unresolved.append("prior_resolved_dispute")
            return
        days = (facts.request_date - facts.prior_resolved_dispute_date).days
        window = int(self.rules["RULE-4"]["repeat_dispute_window_days_inclusive"])
        if 0 <= days <= window:
            approval.append("prior_resolved_dispute")
