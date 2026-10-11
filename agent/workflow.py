from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal

from .evidence import EvidenceLedger
from .grounding import ClaimDraft, validate_claim as _validate_claim
from .language import has_outage_subject, interpret
from .models import Action, CaseFacts, Decision, MaterialClaim, SourceBundle, TicketStatus, TurnTrace
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
    "device_protection": {"KB-1003", "KB-1009"},
    "plan_change_proration": {"KB-1010"},
    "mixed_retention_outage": {"RULE-3", "KB-1004", "KB-1008"},
    "service_outage_policy": {"RULE-3", "RULE-4", "RULE-5", "KB-1004", "KB-1005", "KB-1006", "KB-1007", "KB-1010"},
    "combined_dispute_and_outage": {"RULE-1", "RULE-3", "RULE-4", "RULE-5", "KB-1001", "KB-1002", "KB-1004", "KB-1005", "KB-1006", "KB-1007"},
    "repeat_fraud_policy": {"RULE-4", "RULE-5", "KB-1006", "KB-1007"},
    "ticket_status": {"RULE-4", "RULE-5", "KB-1006", "KB-1007"},
    "clarification": set(),
    "unknown": None,
}
_FINANCIAL_INTENTS = {"service_outage_amount", "outage_deadline", "tax_recalculation", "general_goodwill", "mixed_retention_outage", "service_outage_policy", "repeat_fraud_policy", "ticket_status", "combined_dispute_and_outage"}


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
        if intent == "file_dispute" and has_outage_subject(question):
            allowed = allowed | {"RULE-3", "KB-1004"}
        explicit_general_refund = any(term in question.lower() for term in ("general refund", "after a refund", "after refund", "billing refund"))
        tax_outage_context = has_outage_subject(question) or (
            intent == "tax_recalculation" and not explicit_general_refund
            and (facts.outage_start is not None or facts.ticket_id is not None)
        )
        if intent == "tax_recalculation":
            allowed = {"KB-1003", "KB-1004"} if tax_outage_context else {"KB-1003"}
        elif intent == "device_protection" and any(term in question.lower() for term in ("tax", "surcharge", "regulatory fee")):
            allowed = {"KB-1003", "KB-1009"}
        candidates = retrieve(question, self.sources.passages, allowed_ids=allowed, top_k=10)
        ledger = EvidenceLedger(self.sources.by_id)
        ledger.record_ranked(candidates)

        def validate_claim(draft: ClaimDraft) -> MaterialClaim:
            for source_id in draft.support_ids:
                ledger.fetch_exact(source_id, reason=f"validate {draft.claim_type} claim")
            return _validate_claim(draft, ledger)

        claims: list[MaterialClaim] = []
        provenance_notes: list[str] = []
        additional_decisions: tuple[Decision, ...] = ()
        answer: str
        decision = Decision(None, "informational", reason="Informational answer; no account action is available in this prototype.")
        action = Action.ANSWER

        if intent == "clarification":
            decision = Decision(None, "ambiguous", reason="The question uses a policy term with multiple meanings.")
            action = Action.CLARIFY
            answer = interpretation.clarification_prompt or "Which policy category do you mean: an internet service outage or Device Protection?"
        elif self.conflicts and intent in _FINANCIAL_INTENTS:
            pair_ids = [sid for conflict in self.conflicts for sid in re.findall(r"\b(?:RULE-\d|KB-\d{4})\b", conflict)]
            pair_ids = list(dict.fromkeys(pair_ids))
            claims.append(validate_claim(ClaimDraft("source_conflict", "The authoritative policy, rules configuration, or supporting knowledge base contain a conflicting material rule.", pair_ids, {"conflicts": list(self.conflicts)})))
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
                }))
                claims.append(validate_claim(ClaimDraft("outage_policy_selection", "Rule 3 is the specific service-outage track and is not capped by Rule 2's $75 goodwill limit.", ["RULE-3", "KB-1004"])))
                claims.append(calc_claim)
                if decision.unresolved_checks or decision.approval_reasons:
                    claims.append(validate_claim(ClaimDraft("account_check_pending", "Account-level repeat-dispute and fraud checks are not established; no credit has been issued.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"])))
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
            if tax_outage_context:
                claims.append(validate_claim(ClaimDraft("plan_charge_scope", "The documented outage credit is calculated against the recurring plan charge only.", ["KB-1004"])))
                claims.append(validate_claim(ClaimDraft("tax_recalculation_unknown", "Taxes and regulatory surcharges are itemized separately; the supplied sources do not say whether they are recalculated or refunded after an outage credit.", ["KB-1003", "KB-1004"])))
                answer = "The documented outage credit applies to the recurring plan charge. Taxes and regulatory surcharges are listed separately. The supplied guidance does not specify whether those items are recalculated or refunded after an outage credit, so a billing specialist must confirm that part; I can't answer yes or no from these sources."
                if facts.outage_start or facts.ticket_id:
                    provenance_notes.append("The outage facts are available only from the earlier transcript turns; later case facts were not added to earlier snapshots.")
            else:
                claims.append(validate_claim(ClaimDraft("general_tax_itemization", "The supplied bill guide says applicable taxes and surcharges are itemized separately from the plan charge; it does not specify whether they are refunded or recalculated after a general billing adjustment.", ["KB-1003"])))
                answer = "The bill guide says applicable taxes and regulatory surcharges are itemized separately from the plan charge. It does not specify whether they are refunded or recalculated after a general billing adjustment, so I can't infer that treatment; a billing specialist must confirm it."
            action = Action.ESCALATE
        elif intent == "outage_deadline":
            if facts.outage_end is not None and facts.request_date is not None:
                delta = facts.request_days_after_resolution
                window = int(self.rules["RULE-3"]["request_window_calendar_days_inclusive"])
                deadline = facts.outage_end.date() + timedelta(days=window)
                timely = delta is not None and 0 <= delta <= window
                decision = Decision("RULE-3", "eligible" if timely else "ineligible", deadline=deadline, reason="Request timing uses calendar dates from the outage resolution date.", calculations={"request_day_delta": delta, "deadline": deadline})
                resolution_date = facts.outage_end.date()
                def fmt_date(value):
                    return value.strftime("%B %d, %Y").replace(" 0", " ")

                if timely:
                    request_timing = f"The request dated {fmt_date(facts.request_date)} is {delta} days after resolution and timely."
                elif delta is not None and delta < 0:
                    request_timing = f"The request dated {fmt_date(facts.request_date)} precedes the reported resolution and is outside the documented window."
                else:
                    request_timing = f"The request dated {fmt_date(facts.request_date)} is {delta} days after resolution and outside the documented window."
                answer = (
                    f"A service-outage credit request is due within {window} calendar days after resolution. "
                    f"For the reported {fmt_date(resolution_date)} resolution, the calculated deadline is {fmt_date(deadline)}. "
                    f"{request_timing}"
                )
                delta = facts.request_days_after_resolution
                resolution_source = facts.fact_provenance.get("outage_end", {}).get("timestamp")
                deadline_sources = ["RULE-3", "KB-1005", *( [f"TRANSCRIPT-{resolution_source}"] if resolution_source else []), "TRANSCRIPT-METADATA"]
                claims.append(validate_claim(ClaimDraft("deadline", answer, deadline_sources, {
                    "request_deadline": deadline.isoformat(), "request_day_delta": delta,
                    "resolution_date": facts.outage_end.date().isoformat(), "request_date": facts.request_date.isoformat(),
                })))
            else:
                deadline_days = int(self.rules["RULE-3"]["request_window_calendar_days_inclusive"])
                decision = Decision("RULE-3", "informational", reason="The case-specific date needs the outage resolution date.", unresolved_checks=("outage_resolution_date",))
                answer = f"The supplied policy requires a service-outage credit request within {deadline_days} calendar days after resolution. Share the resolution date and I can calculate the calendar deadline; I don't have those case facts here."
                claims.append(validate_claim(ClaimDraft("deadline", answer, ["RULE-3", "KB-1005"], {"request_deadline": f"{deadline_days}-calendar-day rule"})))
        elif intent == "service_outage_policy":
            decision = Decision("RULE-3", "informational", reason="The specific outage policy is distinct from general goodwill; no customer-specific credit is being decided.")
            track_claim = "A NOC-confirmed outage credit is calculated as the current monthly recurring charge ÷ 30, multiplied by the number of full days without service."
            eligibility_claim = (
                "Rule 3 requires a valid NOC outage ticket covering the service address and dates; the ticketed outage must last longer than 4 consecutive hours and the request must be made within 30 calendar days of resolution."
            )
            cap_claim = "The Rule 3 outage credit is separate from, and not capped by, Rule 2's $75 goodwill limit."
            approval_claim = (
                "Tier 2 review is required before issuance for an outage credit above $300, an outage above 30 consecutive days, "
                "or a ticket that cannot be verified. A second dispute within 90 days of a prior resolved dispute or a flagged "
                "fraud indicator also requires Tier 2 review."
            )
            answer = " ".join((track_claim, eligibility_claim, cap_claim, approval_claim, "These are conditional controls; this prototype does not issue credits."))
            claims.append(validate_claim(ClaimDraft("outage_policy_selection", cap_claim, ["RULE-3", "KB-1004"])))
            claims.append(validate_claim(ClaimDraft("outage_calculation_method", track_claim, ["RULE-3", "KB-1004"])))
            claims.append(validate_claim(ClaimDraft("outage_eligibility", eligibility_claim, ["RULE-3", "KB-1004", "KB-1005"])))
            claims.append(validate_claim(ClaimDraft("outage_approval_conditions", approval_claim, ["RULE-4", "RULE-5", "KB-1006", "KB-1007"])))
            claims.append(validate_claim(ClaimDraft("account_check_pending", "This policy answer does not determine whether any condition applies to a specific account.", ["RULE-4", "RULE-5", "KB-1006", "KB-1007"])))
            if any(term in question.lower() for term in ("late", "goodwill")):
                goodwill_text = "Rule 2 is limited to confirmed general billing errors and applies a cumulative $75 limit per rolling 12-month period; account credit history is needed to determine remaining allowance."
                answer += " A late outage request does not automatically qualify for general goodwill: Rule 2 requires a confirmed billing error outside a more-specific policy and a review of the account's credit history. These facts do not establish eligibility."
                claims.append(validate_claim(ClaimDraft("general_goodwill", goodwill_text, ["RULE-2", "KB-1002"])))
            if "kb-1010" in question.lower() or "plan change formula" in question.lower():
                answer += " KB-1010 describes plan changes and does not govern outage-credit decisions; KB-1004 is the separate policy for service-outage credits."
                claims.append(validate_claim(ClaimDraft("kb:KB-1010", "KB-1010 describes plan changes and does not govern outage-credit decisions; KB-1004 is the separate policy for service-outage credits.", ["KB-1010"])))
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
            claims.append(validate_claim(ClaimDraft("repeat_fraud_policy", policy_text, ["RULE-4", "RULE-5", "KB-1006", "KB-1007"])))
            claims.append(validate_claim(ClaimDraft(
                "account_check_pending", "Whether either condition applies to this account is not established by the policy text alone.",
                ["RULE-4", "RULE-5", "KB-1006", "KB-1007"],
            )))
        elif intent == "ticket_status":
            decision = Decision(None, "approval_unknown", reason="This offline prototype cannot verify ticket or account approval state.", unresolved_checks=("ticket_verification", "credit_approval"))
            action = Action.ESCALATE
            ticket_sources = ["RULE-4", "KB-1006"]
            ticket_stamp = facts.fact_provenance.get("ticket_id", {}).get("timestamp")
            if ticket_stamp:
                ticket_sources.append(f"TRANSCRIPT-{ticket_stamp}")
            reported_by_agent = (facts.ticket_status == TicketStatus.AGENT_REPORTED and bool(ticket_stamp)) or any(
                term in question.lower() for term in ("agent said", "agent told me", "my agent reported", "ticket was confirmed", "agent confirmed", "agent found")
            )
            prefix = "A reported ticket status is not an account authorization record." if reported_by_agent else "I have no account-side approval record."
            answer = f"{prefix} Credit authorization remains unknown: I can't independently verify the NOC ticket or check the account-side decision; no credit issuance can be confirmed here. A billing specialist must review the account."
            claim_text = (
                "The reported ticket status does not establish credit approval; NOC verification and account approval remain unconfirmed."
                if reported_by_agent else "Credit authorization is not established by the available account evidence; account approval remains unconfirmed."
            )
            claims.append(validate_claim(ClaimDraft(
                "ticket_approval_unknown",
                claim_text,
                ticket_sources,
            )))
        elif intent == "combined_dispute_and_outage":
            invoice_decision = self.engine.evaluate_invoice_dispute(facts)
            outage_decision = self.engine.evaluate_service_outage(facts, invoice_dispute=invoice_decision)
            decision = invoice_decision
            additional_decisions = (outage_decision,)
            invoice_window = int(self.rules["RULE-1"]["dispute_window_calendar_days"])
            elapsed = invoice_decision.calculations.get("days_since_statement")
            invoice_text = (
                f"A specific invoice charge may be disputed within {invoice_window} calendar days of the statement date; "
                f"after that period, Tier 2 sign-off is required and resolution is not guaranteed."
            )
            if elapsed is not None:
                elapsed = int(elapsed)
                qualifier = "within" if 0 <= elapsed <= invoice_window else "outside"
                invoice_text = f"The {elapsed}-day interval is {qualifier} the policy window. " + invoice_text
            invoice_sources = ["RULE-1", "KB-1001"]
            dispute_stamp = facts.fact_provenance.get("disputed_item", {}).get("timestamp")
            if dispute_stamp:
                invoice_sources.append(f"TRANSCRIPT-{dispute_stamp}")
            if facts.fact_provenance.get("request_date", {}).get("source_id") == "TRANSCRIPT-METADATA":
                invoice_sources.append("TRANSCRIPT-METADATA")
            claims.append(validate_claim(ClaimDraft("dispute_policy", invoice_text, invoice_sources, {"elapsed_days": elapsed})))
            if invoice_decision.status == "eligible":
                invoice_summary = "the invoice charge is within Rule 1's 60-calendar-day dispute window"
            elif invoice_decision.status == "needs_tier2_approval":
                invoice_summary = "the invoice dispute is beyond Rule 1's 60-calendar-day window, so Tier 2 may consider it but resolution is not guaranteed"
            else:
                invoice_summary = "Rule 1 timing for the invoice dispute is unresolved because the statement date or specific charge is missing"
            distinction = (
                "The Rule 1 billing dispute for the invoice charge is separate from a Rule 3 service-outage credit."
            )
            claims.append(validate_claim(ClaimDraft(
                "dispute_vs_outage_credit", distinction,
                ["RULE-1", "KB-1001", "RULE-3", "KB-1004"],
            )))
            outage_text = (
                "Rule 3 requires a valid NOC outage ticket covering the service address and dates; the outage must last longer than 4 consecutive hours and the request must be made within 30 calendar days of resolution."
            )
            claims.append(validate_claim(ClaimDraft("outage_eligibility", outage_text, ["RULE-3", "KB-1004", "KB-1005"])))
            summaries = ["Invoice dispute: " + invoice_summary + "."]
            if outage_decision.selected_rule_id == "RULE-3" and outage_decision.credit_amount is not None:
                facts_quote_ids = [facts.fact_provenance[k].get("timestamp") for k in ("monthly_recurring_charge", "outage_start", "ticket_id") if k in facts.fact_provenance]
                turn_ids = [f"TRANSCRIPT-{stamp}" for stamp in facts_quote_ids]
                amount = outage_decision.credit_amount
                amount_claim = validate_claim(ClaimDraft("outage_amount", f"The calculated service-outage amount is ${amount:.2f}.", ["RULE-3", "KB-1004", *turn_ids], {
                    "monthly_recurring_charge": str(facts.monthly_recurring_charge),
                    "full_outage_days": facts.full_outage_days or 0,
                    "amount": str(amount),
                    "outage_start": facts.outage_start.isoformat() if facts.outage_start else None,
                    "outage_end": facts.outage_end.isoformat() if facts.outage_end else None,
                }))
                claims.append(amount_claim)
                summaries.append(f"Separate Rule 3 outage-credit calculation: ${amount:.2f}.")
                if outage_decision.unresolved_checks:
                    pending = ", ".join(outage_decision.unresolved_checks)
                    summaries.append(f"This is provisional; required checks remain pending ({pending}), and no credit has been issued.")
                    claims.append(validate_claim(ClaimDraft(
                        "account_check_pending",
                        "NOC ticket verification and account-level repeat-dispute and fraud checks remain unknown; no credit has been issued.",
                        ["RULE-4", "RULE-5", "KB-1006", "KB-1007"],
                    )))
                elif outage_decision.status == "needs_tier2_approval":
                    summaries.append("Tier 2 review is required before any Rule 3 issuance.")
                else:
                    summaries.append("The Rule 3 checks are clear for frontline consideration; the prototype still does not issue credits.")
            else:
                summaries.append("Separate Rule 3 assessment: " + outage_decision.reason)
                if outage_decision.unresolved_checks:
                    summaries.append("Missing or unknown outage facts: " + ", ".join(outage_decision.unresolved_checks) + ".")
            answer = " ".join(summaries + [distinction, outage_text, "These are separate decisions; this prototype does not open a case or issue a credit."])
            if any(result.status == "needs_tier2_approval" for result in (invoice_decision, outage_decision)):
                action = Action.ESCALATE
            elif any(result.status in {"pending_missing_facts", "calculated_pending_checks"} for result in (invoice_decision, outage_decision)):
                action = Action.CLARIFY
            else:
                action = Action.ANSWER
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
            asking_about_documents = any(term in question.lower() for term in ("document", "paperwork", "receipt", "proof"))
            if asking_about_documents:
                answer = (
                    "The supplied guide does not specify required supporting documents. It says Billing should confirm the account holder's identity "
                    "and identify the exact invoice charge by date, amount, and description. Please confirm with Billing whether additional paperwork is needed."
                )
                claims.append(validate_claim(ClaimDraft("billing_dispute_process", answer, ["KB-1001"])))
            else:
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
            claims.append(validate_claim(ClaimDraft("dispute_policy", policy_claim, ["RULE-1", "KB-1001"], {"elapsed_days": elapsed})))
            if has_outage_subject(question):
                distinction = (
                    "Filing a Rule 1 billing dispute for an outage charge is separate from requesting a Rule 3 service-outage credit."
                )
                answer += " " + distinction
                claims.append(validate_claim(ClaimDraft(
                    "dispute_vs_outage_credit", distinction,
                    ["RULE-1", "KB-1001", "RULE-3", "KB-1004"],
                )))
        elif intent == "general_goodwill":
            decision = Decision("RULE-2", "informational", reason="General goodwill is separate from more-specific policy tracks.", unresolved_checks=("credit_history",))
            answer = "A confirmed general billing error may qualify for a cumulative total of up to $75 per account in a rolling 12-month period. The account's credit history is needed to determine any remaining allowance; a total above $75 needs Tier 2 approval. Service-outage credits use a separate policy."
            claims.append(validate_claim(ClaimDraft("general_goodwill", answer, ["RULE-2", "KB-1002"])))
        elif intent == "retention":
            retention_source = ledger.fetch_exact("KB-1008", reason="extract retention offer amount and scope")
            source_text = retention_source.text if retention_source else ""
            cap_match = re.search(r"up to\s+\*\*\$(\d+(?:\.\d{1,2})?)\*\*", source_text, re.IGNORECASE)
            cap = f"up to ${Decimal(cap_match.group(1)):.2f}" if cap_match else "with no dollar cap stated in this excerpt"
            answer = f"The knowledge base describes a separate discretionary retention offer {cap} for eligible customers actively considering cancellation, handled by the Customer Retention queue. It cannot resolve a billing dispute or outage-credit request; this prototype cannot issue an offer."
            claims.append(validate_claim(ClaimDraft("kb:KB-1008", answer, ["KB-1008"])))
        elif intent == "device_protection":
            if any(term in question.lower() for term in ("tax", "surcharge", "regulatory fee")):
                device_tax_text = "The general bill guide lists applicable taxes and one-time device charges; the Device Protection article describes replacement credits but does not specify tax treatment for a replacement."
                answer = "The bill guide says invoices can include one-time device charges and applicable state and local taxes. The Device Protection article describes replacement credits but does not specify whether taxes apply to them, so I can't answer yes or no; ask a Device Protection or billing specialist."
                action = Action.ESCALATE
                claims.append(validate_claim(ClaimDraft("device_tax_scope", device_tax_text, ["KB-1003", "KB-1009"])))
            else:
                device_source = ledger.fetch_exact("KB-1009", reason="extract Device Protection deductible and claim limits")
                source_text = device_source.text if device_source else ""
                if "deductible" in question.lower():
                    answer = "The deductible is $25–$199 depending on device tier."
                    claims.append(validate_claim(ClaimDraft("device_deductible", answer, ["KB-1009"])))
                else:
                    body = "\n".join(line for line in source_text.splitlines() if not line.startswith("### ") and not line.startswith("**Category:**")).strip()
                    answer = body or "The Device Protection article is unavailable in the supplied evidence."
                    claims.append(validate_claim(ClaimDraft("kb_quote", body, ["KB-1009"])))
        elif intent == "plan_change_proration":
            answer = "The knowledge base says mid-cycle plan changes prorate old and new recurring charges by active days using monthly charge ÷ 30 × days active. That rule is for plan changes; KB-1004 describes the separate service-outage policy, which requires NOC confirmation."
            claims.append(validate_claim(ClaimDraft("kb:KB-1010", answer, ["KB-1010"])))
        elif intent == "mixed_retention_outage":
            decision = Decision("RULE-3", "pending_missing_facts", reason="Retention offers cannot substitute for outage-credit review.", unresolved_checks=("outage_case_facts",))
            action = Action.ESCALATE
            retention_source = ledger.fetch_exact("KB-1008", reason="check retention program boundary for mixed outage request")
            source_text = retention_source.text if retention_source else ""
            cap_match = re.search(r"up to\s+\*\*\$(\d+(?:\.\d{1,2})?)\*\*", source_text, re.IGNORECASE)
            cap = f"${Decimal(cap_match.group(1)):.2f}" if cap_match else "a discretionary offer"
            retention_claim = f"The {cap} retention program is separate and is not a substitute for an outage-credit decision."
            answer = retention_claim + " An outage request must be evaluated under the Service-Outage Credit policy using the reported service address, outage dates, NOC ticket, and request date. I can't promise or issue either credit here."
            claims.append(validate_claim(ClaimDraft("kb:KB-1008", retention_claim, ["KB-1008"])))
            claims.append(validate_claim(ClaimDraft("outage_policy_selection", "Qualifying outage requests use a separate Rule 3 credit that is not capped by Rule 2's $75 limit.", ["RULE-3", "KB-1004"])))
        else:
            extracted = self._extract_supported_fact(question, ledger)
            if extracted is not None:
                passage_id, excerpt = extracted
                decision = Decision(None, "informational", reason="A source sentence directly supports this descriptive question.")
                action = Action.ANSWER
                answer = excerpt
                claims.append(validate_claim(ClaimDraft("kb_quote", excerpt, [passage_id])))
            else:
                decision = Decision(None, "unsupported", reason="The supplied policy and knowledge-base topics do not support this answer.")
                action = Action.DECLINE
                answer = "I can't answer that from the supplied billing policy and knowledge-base excerpt. Please check the applicable product documentation or ask the relevant specialist."
                claims.append(validate_claim(ClaimDraft("unsupported", "No supplied source supports an answer to this question.", [])))

        # Refuse to render any affirmative template claim that fails semantic validation.
        if any(claim.status == "unsupported" for claim in claims) and intent != "unknown":
            action = Action.ESCALATE
            supported_claims = [claim.text for claim in claims if claim.status in {"grounded", "partially_grounded", "requires_account_check"}]
            if supported_claims:
                answer = " ".join(supported_claims) + " The supplied sources do not adequately support the remaining details; please have a billing specialist verify them."
            else:
                answer = "The supplied sources do not adequately support this answer. I won't guess; please have a billing specialist verify it."

        return TurnTrace(
            timestamp, _safe_question(question), intent, "offline deterministic", facts.to_public_dict(), tuple(candidates),
            decision, tuple(claims), action, answer, tuple(provenance_notes),
            tuple(record.to_dict() for record in ledger.records), additional_decisions,
        )

    def _extract_supported_fact(self, question: str, ledger: EvidenceLedger) -> tuple[str, str] | None:
        """Return a verbatim source sentence for descriptive, non-decision questions only."""
        lowered = question.lower()
        if any(term in lowered for term in ("am i eligible", "do i qualify", "can i get", "approve", "issue", "refund me", "credit me", "will i receive", "waive", "waiver")) or re.search(r"\bwill\b", lowered):
            return None
        stop = {"what", "which", "does", "do", "is", "are", "the", "a", "an", "how", "many", "much", "my", "your", "for", "from", "about", "with", "and", "or", "of", "to", "in", "on", "it", "can", "i", "include", "includes"}
        query_terms = {term for term in re.findall(r"[a-z0-9]+", lowered) if len(term) > 2 and term not in stop}
        equivalents = {
            "items": {"item", "items", "include", "includes"},
            "components": {"component", "components", "include", "includes"},
            "fees": {"fee", "fees", "surcharge", "surcharges"},
            "charges": {"charge", "charges"},
        }
        ranked = [record for record in ledger.records if record.access_type == "ranked_retrieval" and record.status == "found"]
        ranked.sort(key=lambda record: record.rank or 10**6)
        for record in ranked:
            passage = ledger.passages_for([record.passage_id]).get(record.passage_id)
            if passage is None or passage.source_kind not in {"kb", "policy"}:
                continue
            text = "\n".join(line for line in passage.text.splitlines() if not line.startswith("### ") and not line.startswith("**Category:")).strip()
            protected = text.replace("e.g.", "e<abbr>g<abbr>")
            for sentence in re.split(r"(?<=[.!?])\s+", protected):
                sentence = sentence.replace("e<abbr>g<abbr>", "e.g.")
                sentence = sentence.strip()
                if not sentence or sentence.startswith("###") or sentence.startswith("**Category:"):
                    continue
                sentence_terms = set(re.findall(r"[a-z0-9]+", sentence.lower()))
                if all(bool(equivalents.get(term, {term}) & sentence_terms) for term in query_terms):
                    return passage.passage_id, sentence
        return None

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
