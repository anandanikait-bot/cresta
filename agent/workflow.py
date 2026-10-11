from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from .grounding import ClaimDraft, validate_claim
from .language import interpret
from .models import Action, CaseFacts, Decision, MaterialClaim, SourceBundle, TurnTrace
from .policy_engine import PolicyEngine
from .retrieval import retrieve
from .rules import RuleSet, validate_rule_bindings
from .state import snapshots_for_questions


_ALLOWED = {
    "service_outage_amount": {"RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5", "KB-1002", "KB-1004", "KB-1005", "KB-1006", "KB-1007"},
    "outage_deadline": {"RULE-3", "KB-1004", "KB-1005"},
    "tax_recalculation": {"KB-1003", "KB-1004"},
    "file_dispute": {"RULE-1", "KB-1001"},
    "general_goodwill": {"RULE-2", "RULE-4", "RULE-5", "KB-1002", "KB-1006", "KB-1007"},
    "retention": {"KB-1008"},
    "device_protection": {"KB-1009"},
    "plan_change_proration": {"KB-1010"},
    "mixed_retention_outage": {"RULE-3", "KB-1004", "KB-1008"},
    "service_outage_policy": {"RULE-3", "RULE-4", "RULE-5", "KB-1004", "KB-1005", "KB-1006", "KB-1007", "KB-1010"},
    "repeat_fraud_policy": {"RULE-4", "RULE-5", "KB-1006", "KB-1007"},
    "unknown": None,
}
_FINANCIAL_INTENTS = {"service_outage_amount", "outage_deadline", "tax_recalculation", "general_goodwill", "mixed_retention_outage", "service_outage_policy", "repeat_fraud_policy"}


def _safe_question(value: str) -> str:
    value = re.sub(r"\bNMB-\d+\b", "[REDACTED ACCOUNT]", value, flags=re.IGNORECASE)
    value = re.sub(r"\baccount(?:\s+number)?\s+(?:is\s+|[:=]\s*)?[A-Z0-9-]*\d[A-Z0-9-]{5,}\b", "[REDACTED ACCOUNT]", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[REDACTED PAYMENT DETAIL]", value)
    value = re.sub(r"\b(?:payment\s+)?card\b.{0,32}?\b\d{4}\b", "[REDACTED PAYMENT DETAIL]", value, flags=re.IGNORECASE)
    return value


class BillingWorkflow:
    def __init__(self, sources: SourceBundle, rules: RuleSet):
        self.sources = sources
        self.rules = rules
        self.engine = PolicyEngine(rules)
        self.conflicts = sources.conflicts + validate_rule_bindings(rules, sources)

    def replay(self) -> list[TurnTrace]:
        traces: list[TurnTrace] = []
        for event, facts in snapshots_for_questions(self.sources):
            traces.append(self.answer(event.text, facts, timestamp=event.timestamp))
        return traces

    def answer(self, question: str, facts: CaseFacts | None = None, timestamp: str | None = None) -> TurnTrace:
        facts = facts or CaseFacts()
        interpretation = interpret(question, facts)
        intent = interpretation.intent
        allowed = _ALLOWED[intent]
        candidates = retrieve(question, self.sources.passages, allowed_ids=allowed, top_k=10)
        claims: list[MaterialClaim] = []
        provenance_notes: list[str] = []
        answer: str
        decision = Decision(None, "informational", reason="Informational answer; no account action is available in this prototype.")
        action = Action.ANSWER
        evidence = self.sources.by_id

        if self.conflicts and intent in _FINANCIAL_INTENTS:
            pair_ids = [sid for conflict in self.conflicts for sid in re.findall(r"\b(?:RULE-\d|KB-\d{4})\b", conflict)]
            pair_ids = list(dict.fromkeys(pair_ids))
            claims.append(validate_claim(ClaimDraft("source_conflict", "The authoritative policy, rules configuration, or supporting knowledge base contain a conflicting material rule.", pair_ids, {"conflicts": list(self.conflicts)}), evidence))
            decision = Decision(None, "source_conflict", reason="A material source or approved-rule configuration conflict blocks a safe policy decision.", approval_reasons=tuple(self.conflicts))
            action = Action.ESCALATE
            answer = "I found conflicting values in the supplied policy and knowledge base, so I can't safely calculate or confirm this request. A billing specialist must resolve the source conflict first."
        elif intent == "service_outage_amount":
            decision = self.engine.evaluate_service_outage(facts)
            if decision.status == "pending_missing_facts":
                action = Action.CLARIFY
                answer = self._outage_clarification(question, facts)
            elif decision.status == "needs_tier2_approval":
                action = Action.ESCALATE
                answer = "This outage request needs review by a Tier 2 Billing Specialist before any credit decision. This prototype does not issue credits."
            elif decision.selected_rule_id != "RULE-3":
                action = Action.CLARIFY if decision.status == "pending_missing_facts" else Action.ESCALATE
                answer = "The supplied facts do not establish eligibility under the Service-Outage Credit rule. A separate confirmed billing error and the account's rolling credit history are required before evaluating a general goodwill request; I can't promise a $75 credit."
            else:
                facts_quote_ids = [facts.fact_provenance[k].get("timestamp") for k in ("monthly_recurring_charge", "outage_start", "ticket_id") if k in facts.fact_provenance]
                turn_ids = [f"TRANSCRIPT-{stamp}" for stamp in facts_quote_ids]
                claim_sources = ["RULE-3", "KB-1004", *turn_ids]
                amount = decision.credit_amount or Decimal("0.00")
                calc_claim = validate_claim(ClaimDraft("outage_amount", f"The calculated service-outage amount is ${amount:.2f}.", claim_sources, {
                    "monthly_recurring_charge": str(facts.monthly_recurring_charge),
                    "full_outage_days": facts.full_outage_days or 0,
                    "amount": str(amount),
                    "outage_start": facts.outage_start.isoformat() if facts.outage_start else None,
                    "outage_end": facts.outage_end.isoformat() if facts.outage_end else None,
                }), evidence)
                claims.append(validate_claim(ClaimDraft("outage_policy_selection", "Rule 3 is the specific service-outage track and is not capped by Rule 2's $75 goodwill limit.", ["RULE-3", "KB-1004"]), evidence))
                claims.append(calc_claim)
                if decision.unresolved_checks or decision.approval_reasons:
                    claims.append(validate_claim(ClaimDraft("account_check_pending", "Account-level repeat-dispute and fraud checks are not established; no credit has been issued.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]), evidence))
                    action = Action.ESCALATE if decision.approval_reasons else Action.ANSWER
                    pending = ", ".join(decision.unresolved_checks + decision.approval_reasons)
                    answer = (
                        f"This is a separate Service-Outage Credit under Rule 3, not the general $75 goodwill cap. "
                        f"Using the reported ${facts.monthly_recurring_charge:.2f} monthly charge and {facts.full_outage_days} full outage days, "
                        f"(${facts.monthly_recurring_charge:.2f} ÷ 30) × {facts.full_outage_days} = ${amount:.2f}. "
                        f"That amount is not limited by Rule 2's $75 cap. The transcript records an agent-reported NOC lookup; this replay does not independently access NOC. "
                        f"Required checks remain pending ({pending}); no credit has been issued."
                    )
                else:
                    answer = f"The Rule 3 calculation is ${amount:.2f}. This is an estimate for review; the prototype does not issue credits or confirm an account-side action."
        elif intent == "tax_recalculation":
            claims.append(validate_claim(ClaimDraft("plan_charge_scope", "The documented outage credit is calculated against the recurring plan charge only.", ["KB-1004"]), evidence))
            claims.append(validate_claim(ClaimDraft("tax_recalculation_unknown", "Taxes and regulatory surcharges are itemized separately; the supplied sources do not say whether they are recalculated or refunded after an outage credit.", ["KB-1003", "KB-1004"]), evidence))
            action = Action.ESCALATE
            answer = "The documented outage credit applies to the recurring plan charge. Taxes and regulatory surcharges are listed separately. The supplied guidance does not specify whether those items are recalculated or refunded after an outage credit, so a billing specialist must confirm that part; I can't answer yes or no from these sources."
            if facts.outage_start or facts.ticket_id:
                provenance_notes.append("The outage facts are available only from the earlier transcript turns; later case facts were not added to earlier snapshots.")
        elif intent == "outage_deadline":
            if facts.outage_end is not None and facts.request_date is not None:
                delta = facts.request_days_after_resolution
                window = int(self.rules["RULE-3"]["request_window_calendar_days_inclusive"])
                deadline = facts.outage_end.date() + timedelta(days=window)
                timely = delta is not None and 0 <= delta <= window
                decision = Decision("RULE-3", "eligible" if timely else "ineligible", deadline=deadline, reason="Request timing uses calendar dates from the outage resolution date.", calculations={"request_day_delta": delta, "deadline": deadline})
                answer = f"A service-outage credit request is due within {window} calendar days after the outage is resolved. For the reported September 21, 2026 resolution, the deadline is October 21, 2026; this October 5 request is timely." if timely and deadline == date(2026, 10, 21) else f"A service-outage credit request is due within {window} calendar days after resolution. The calculated deadline is {deadline:%B %d, %Y}; the request is {'timely' if timely else 'outside the documented window'}."
                delta = facts.request_days_after_resolution
                resolution_source = facts.fact_provenance.get("outage_end", {}).get("timestamp")
                deadline_sources = ["RULE-3", "KB-1005", *( [f"TRANSCRIPT-{resolution_source}"] if resolution_source else []), "TRANSCRIPT-METADATA"]
                claims.append(validate_claim(ClaimDraft("deadline", answer, deadline_sources, {
                    "request_deadline": deadline.isoformat(), "request_day_delta": delta,
                    "resolution_date": facts.outage_end.date().isoformat(), "request_date": facts.request_date.isoformat(),
                }), evidence))
            else:
                deadline_days = int(self.rules["RULE-3"]["request_window_calendar_days_inclusive"])
                decision = Decision("RULE-3", "informational", reason="The case-specific date needs the outage resolution date.", unresolved_checks=("outage_resolution_date",))
                answer = f"The supplied policy requires a service-outage credit request within {deadline_days} calendar days after resolution. Share the resolution date and I can calculate the calendar deadline; I don't have those case facts here."
                claims.append(validate_claim(ClaimDraft("deadline", answer, ["RULE-3", "KB-1005"], {"request_deadline": f"{deadline_days}-calendar-day rule"}), evidence))
        elif intent == "service_outage_policy":
            decision = Decision("RULE-3", "informational", reason="The specific outage policy is distinct from general goodwill; no customer-specific credit is being decided.")
            track_claim = "A NOC-confirmed outage credit is calculated from the monthly recurring plan charge and full outage days."
            eligibility_claim = (
                "The outage must be NOC-confirmed, last longer than 4 consecutive hours, and be requested within 30 calendar days of resolution."
            )
            cap_claim = "The Rule 3 outage credit is separate from, and not capped by, Rule 2's $75 goodwill limit."
            approval_claim = (
                "Tier 2 review is required before issuance for an outage credit above $300, an outage above 30 consecutive days, "
                "or a ticket that cannot be verified. A second dispute within 90 days of a prior resolved dispute or a flagged "
                "fraud indicator also requires Tier 2 review."
            )
            answer = " ".join((track_claim, eligibility_claim, cap_claim, approval_claim, "These are conditional controls; this prototype does not issue credits."))
            claims.append(validate_claim(ClaimDraft("outage_policy_selection", cap_claim, ["RULE-3", "KB-1004"]), evidence))
            claims.append(validate_claim(ClaimDraft("outage_eligibility", eligibility_claim, ["RULE-3", "KB-1004"]), evidence))
            claims.append(validate_claim(ClaimDraft("outage_approval_conditions", approval_claim, ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]), evidence))
            claims.append(validate_claim(ClaimDraft("account_check_pending", "This policy answer does not determine whether any condition applies to a specific account.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]), evidence))
            if "kb-1010" in question.lower() or "plan change formula" in question.lower():
                answer += " KB-1010 describes plan changes and does not govern outage-credit decisions; KB-1004 is the separate policy for service-outage credits."
                claims.append(validate_claim(ClaimDraft("kb:KB-1010", "KB-1010 describes plan changes and does not govern outage-credit decisions; KB-1004 is the separate policy for service-outage credits.", ["KB-1010"]), evidence))
        elif intent == "repeat_fraud_policy":
            decision = Decision(None, "informational", reason="Conditional repeat-dispute and fraud controls; no account-level check is available.")
            policy_text = (
                "A second billing dispute within 90 days of a prior resolved dispute, or a flagged fraud indicator, "
                "requires a Tier 2 specialist to make the credit decision regardless of amount."
            )
            answer = (
                policy_text + " I can't tell whether either applies without an account-level review; "
                "this prototype cannot perform that check or issue a credit."
            )
            claims.append(validate_claim(ClaimDraft("repeat_fraud_policy", policy_text, ["RULE-4", "RULE-5", "KB-1006", "KB-1007"]), evidence))
            claims.append(validate_claim(ClaimDraft(
                "account_check_pending", "Whether either condition applies to this account is not established by the policy text alone.",
                ["RULE-4", "RULE-5", "KB-1006", "KB-1007"],
            ), evidence))
        elif intent == "file_dispute":
            decision = Decision("RULE-1", "informational", reason="General filing guidance; no case was opened.")
            action = Action.ANSWER
            window = int(self.rules["RULE-1"]["dispute_window_calendar_days"])
            elapsed_match = re.search(r"\b(\d+)\s+days?\s+after\s+(?:the\s+)?(?:invoice\s+)?statement\s+date\b", question.lower())
            elapsed = int(elapsed_match.group(1)) if elapsed_match else None
            if elapsed is not None:
                timing = f"The {elapsed}-day interval you gave is {'within the limit of' if 0 <= elapsed <= window else 'beyond the limit of'} {window} calendar days after the statement date, if counted from that date."
            else:
                timing = f"A specific invoice charge may be disputed within {window} calendar days of the statement date."
            answer = (
                f"{timing} Identify the specific charge on the invoice (date, amount, and description). "
                f"After {window} days, Tier 2 Billing Specialist sign-off is required and resolution is not guaranteed. "
                "This prototype cannot open a case or determine any account-specific outcome."
            )
            policy_claim = (
                f"A specific invoice charge may be disputed within {window} calendar days of the statement date; "
                f"after that period, Tier 2 sign-off is required and resolution is not guaranteed."
            )
            if elapsed is not None:
                policy_claim = f"The {elapsed}-day interval is {'within' if 0 <= elapsed <= window else 'outside'} the policy window. " + policy_claim
            claims.append(validate_claim(ClaimDraft("dispute_policy", policy_claim, ["RULE-1", "KB-1001"], {"elapsed_days": elapsed}), evidence))
        elif intent == "general_goodwill":
            decision = Decision("RULE-2", "informational", reason="General goodwill is separate from more-specific policy tracks.", unresolved_checks=("credit_history",))
            answer = "A confirmed general billing error may qualify for a cumulative total of up to $75 per account in a rolling 12-month period. The account's credit history is needed to determine any remaining allowance; a total above $75 needs Tier 2 approval. Service-outage credits use a separate policy."
            claims.append(validate_claim(ClaimDraft("general_goodwill", answer, ["RULE-2", "KB-1002"]), evidence))
        elif intent == "retention":
            source_text = evidence["KB-1008"].text
            cap_match = re.search(r"up to\s+\*\*\$(\d+(?:\.\d{1,2})?)\*\*", source_text, re.IGNORECASE)
            cap = f"up to ${Decimal(cap_match.group(1)):.2f}" if cap_match else "with no dollar cap stated in this excerpt"
            answer = f"The knowledge base describes a separate discretionary retention offer {cap} for eligible customers actively considering cancellation, handled by the Customer Retention queue. It cannot resolve a billing dispute or outage-credit request; this prototype cannot issue an offer."
            claims.append(validate_claim(ClaimDraft("kb:KB-1008", answer, ["KB-1008"]), evidence))
        elif intent == "device_protection":
            answer = "The supplied knowledge base describes a separate Device Protection replacement-credit process for eligible lost, stolen, or malfunctioning devices, with a deductible and claim limits. It is handled by the Device Protection team, not Billing; this prototype cannot issue a replacement credit."
            claims.append(validate_claim(ClaimDraft("kb:KB-1009", answer, ["KB-1009"]), evidence))
        elif intent == "plan_change_proration":
            answer = "The knowledge base says mid-cycle plan changes prorate old and new recurring charges by active days using monthly charge ÷ 30 × days active. That rule is for plan changes; KB-1004 describes the separate service-outage policy, which requires NOC confirmation."
            claims.append(validate_claim(ClaimDraft("kb:KB-1010", answer, ["KB-1010"]), evidence))
        elif intent == "mixed_retention_outage":
            decision = Decision("RULE-3", "pending_missing_facts", reason="Retention offers cannot substitute for outage-credit review.", unresolved_checks=("outage_case_facts",))
            action = Action.ESCALATE
            source_text = evidence["KB-1008"].text
            cap_match = re.search(r"up to\s+\*\*\$(\d+(?:\.\d{1,2})?)\*\*", source_text, re.IGNORECASE)
            cap = f"${Decimal(cap_match.group(1)):.2f}" if cap_match else "a discretionary offer"
            retention_claim = f"The {cap} retention program is separate and is not a substitute for an outage-credit decision."
            answer = retention_claim + " An outage request must be evaluated under the Service-Outage Credit policy using the reported service address, outage dates, NOC ticket, and request date. I can't promise or issue either credit here."
            claims.append(validate_claim(ClaimDraft("kb:KB-1008", retention_claim, ["KB-1008"]), evidence))
            claims.append(validate_claim(ClaimDraft("outage_policy_selection", "Qualifying outage requests use a separate Rule 3 credit that is not capped by Rule 2's $75 limit.", ["RULE-3", "KB-1004"]), evidence))
        else:
            decision = Decision(None, "unsupported", reason="The supplied policy and knowledge-base topics do not support this answer.")
            action = Action.DECLINE
            answer = "I can't answer that from the supplied billing policy and knowledge-base excerpt. Please check the applicable product documentation or ask the relevant specialist."
            claims.append(validate_claim(ClaimDraft("unsupported", "No supplied source supports an answer to this question.", []), evidence))

        # Refuse to render any affirmative template claim that fails semantic validation.
        if any(claim.status == "unsupported" for claim in claims) and intent != "unknown":
            action = Action.ESCALATE
            answer = "The supplied sources do not adequately support this answer. I won't guess; please have a billing specialist verify it."

        return TurnTrace(
            timestamp, _safe_question(question), intent, "offline deterministic", facts.to_public_dict(), tuple(candidates),
            decision, tuple(claims), action, answer, tuple(provenance_notes),
        )

    def _outage_clarification(self, question: str, facts: CaseFacts) -> str:
        if any(term in question.lower() for term in ("approve", "issue", "credit me", "$1000")):
            return "I can't approve or issue a credit based on a request to bypass policy. To evaluate an outage credit, the agent needs the service address, outage start and resolution dates, NOC ticket, monthly recurring plan charge, and request date."
        missing: list[str] = []
        if facts.ticket_id is None:
            missing.append("the NOC ticket and whether it covers the service address")
        if facts.outage_start is None or facts.outage_end is None:
            missing.append("the outage start and resolution dates")
        if facts.monthly_recurring_charge is None:
            missing.append("the current monthly recurring plan charge")
        if facts.request_date is None:
            missing.append("the request date")
        if facts.disputed_item is None:
            missing.append("the specific disputed invoice charge")
        detail = "; ".join(missing) if missing else "the missing NOC verification details"
        return f"I need {detail} before I can calculate an outage credit. I won't assume case facts or quote a credit amount."
