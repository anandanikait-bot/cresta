from __future__ import annotations

import re
from calendar import month_name
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .calculators import outage_credit
from .evidence import EvidenceLedger, _safe_excerpt, _sanitize_source_text
from .models import MaterialClaim, Passage


@dataclass(frozen=True)
class ClaimDraft:
    claim_type: str
    text: str
    support_ids: list[str]
    derived_inputs: dict[str, Any] = field(default_factory=dict)


def _strip_negated_issuance_claims(text: str) -> str:
    verbs = r"(?:issued|posted|applied|credited|paid|approved|processed|refunded|reflected|sent|released|completed|received|delivered)"
    return re.sub(
        rf"\b(?:no|not|never)\s+(?:(?:service[- ]outage|outage|goodwill|account)\s+)*(?:credit|refund|payment)\s+(?:(?:has|have|had)\s+(?:ever\s+)?(?:already\s+)?(?:been\s+)?|(?:was|were|is|are)\s+|(?:already\s+|ever\s+)?){verbs}\b",
        "",
        text,
        flags=re.IGNORECASE,
    )


def _has_affirmative_issuance_claim(text: str) -> bool:
    verbs = r"(?:issued|posted|applied|credited|paid|approved|processed|refunded|reflected|sent|released|completed|received|delivered)"
    positive_text = _strip_negated_issuance_claims(text)
    patterns = (
        rf"\b(?:customer|account holder|account)\s+(?:(?:has|have|had|already|was|were|is|are|been)\s+)*(?:received|got|credited|issued|refunded|paid|processed|approved)\b",
        rf"\b(?:we|an agent|billing)\s+(?:(?:has|have|had|already)\s+)*(?:issued|posted|applied|approved|credited|paid|processed|refunded|sent|released)\b",
        rf"\b(?:the )?(?:credit|refund|payment)\s+(?:(?:has|have|had)\s+(?:already\s+)?(?:been\s+)?|(?:was|were|is|are)\s+|(?:already\s+|now\s+)?){verbs}\b",
        r"\b(?:customer|account holder|account)\s+(?:will|shall|must|may|might|can|could)\s+(?:receive|get|be\s+(?:issued|credited|refunded|approved|paid))\b",
        rf"\$\s*\d[\d,]*(?:\.\d+)?\s+(?:(?:has|have|had)\s+(?:already\s+)?(?:been\s+)?|(?:was|were|is|are)\s+|(?:already\s+|now\s+)?){verbs}\b",
    )
    return any(re.search(pattern, positive_text, re.IGNORECASE) for pattern in patterns)


def validate_claim(draft: ClaimDraft, evidence: EvidenceLedger) -> MaterialClaim:
    ids = tuple(dict.fromkeys(draft.support_ids))
    available = evidence.passages_for(ids) if isinstance(evidence, EvidenceLedger) else {}
    supported = False
    status = "unsupported"
    if draft.claim_type == "outage_amount":
        try:
            charge = Decimal(str(draft.derived_inputs["monthly_recurring_charge"]))
            days = int(draft.derived_inputs["full_outage_days"])
            claimed = Decimal(str(draft.derived_inputs["amount"]))
            recomputed = outage_credit(charge, days)
            start = datetime.fromisoformat(str(draft.derived_inputs["outage_start"]))
            end = datetime.fromisoformat(str(draft.derived_inputs["outage_end"]))
            transcript_support = [p for p in available.values() if p.source_kind == "transcript" and p.passage_id.startswith("TRANSCRIPT-") and p.passage_id != "TRANSCRIPT-METADATA"]

            def mentions(value: datetime, text: str) -> bool:
                month_day = re.search(rf"\b{month_name[value.month]}\s+{value.day}(?:st|nd|rd|th)?\b", text, re.IGNORECASE)
                hour = value.hour % 12 or 12
                ampm = "AM" if value.hour < 12 else "PM"
                time = re.search(rf"\b{hour}(?::{value.minute:02d})?\s*{ampm}\b", text, re.IGNORECASE)
                return bool(month_day and time)

            facts_supported = any(f"${charge:.2f}" in p.text and mentions(start, p.text) and mentions(end, p.text) for p in transcript_support)
            duration = end - start
            derived_days_match = duration >= timedelta() and duration.days == days
            rule3 = re.sub(r"[*_`]", "", available["RULE-3"].text).lower() if "RULE-3" in available else ""
            kb4 = re.sub(r"[*_`]", "", available["KB-1004"].text).lower() if "KB-1004" in available else ""
            formula_pattern = r"monthly recurring charge\s*÷\s*30.*?full days"
            formula_supported = all(re.search(formula_pattern, source, re.DOTALL) for source in (rule3, kb4))
            money_tokens = re.findall(r"(?<!\w)(-?)\$\s*(\d[\d,]*(?:\.\d+)?)", draft.text)
            rendered_amounts = {Decimal(sign + value.replace(",", "")) for sign, value in money_tokens}
            precision_supported = all("." not in value or len(value.rsplit(".", 1)[1]) <= 2 for _, value in money_tokens)
            amount_text_supported = (
                bool(money_tokens) and precision_supported
                and rendered_amounts == {recomputed}
            )
            promise_or_issuance = _has_affirmative_issuance_claim(draft.text)
            calculation_language = bool(re.search(r"\b(?:calculat\w*|estimat\w*|provisional)\b", draft.text, re.IGNORECASE))
            supported = (
                formula_supported and claimed == recomputed and facts_supported and derived_days_match
                and duration > timedelta(hours=4) and amount_text_supported
                and calculation_language and not promise_or_issuance
            )
        except (KeyError, InvalidOperation, ValueError, TypeError, OverflowError):
            supported = False
    elif draft.claim_type == "tax_recalculation_unknown":
        if {"KB-1003", "KB-1004"}.issubset(available):
            kb3 = re.sub(r"[*_`]", "", available["KB-1003"].text).lower()
            kb4 = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            claim = draft.text.lower()
            definite_text = re.sub(r"(?:do not say|does not say|does not specify|don't say|doesn't specify)\s+(?:whether|if)\b[^.;]*", "", claim)
            definite_tax_claim = re.search(
                r"\b(?:taxes|surcharges)\b[^.;]{0,80}\b(?:refund(?:ed)?|recalculat\w*|reduc(?:ed|tion)|return(?:ed)?)\b",
                definite_text,
            )
            supported = definite_tax_claim is None and all(("taxes and surcharges" in kb3, "itemized separately" in kb3,
                             "recurring plan charge" in kb4, "not say" in claim or "does not specify" in claim,
                             "tax" in claim, "recalculat" not in kb3 + kb4))
        status = "partially_grounded" if supported else "unsupported"
    elif draft.claim_type == "general_tax_itemization":
        if "KB-1003" in available:
            source = re.sub(r"[*_`]", "", available["KB-1003"].text).lower()
            claim = draft.text.lower()
            supported = (
                "taxes and surcharges" in source and "itemized separately" in source
                and "itemized separately" in claim and "recalculat" not in source
            )
        status = "partially_grounded" if supported else "unsupported"
    elif draft.claim_type == "device_tax_scope":
        if {"KB-1003", "KB-1009"}.issubset(available):
            kb3 = re.sub(r"[*_`]", "", available["KB-1003"].text).lower()
            kb9 = re.sub(r"[*_`]", "", available["KB-1009"].text).lower()
            claim = draft.text.lower()
            supported = all(("one-time device or accessory charges" in kb3, "applicable state and local taxes" in kb3,
                             "device replacement credit" in kb9, "does not specify" in claim,
                             "tax" in claim and "replacement" in claim, "tax" not in kb9))
        status = "partially_grounded" if supported else "unsupported"
    elif draft.claim_type == "kb_quote":
        passage = next(iter(available.values()), None)
        if passage is not None:
            def plain(text: str) -> str:
                return re.sub(r"\s+", " ", re.sub(r"[*_`]", "", text)).strip().casefold()
            supported = plain(draft.text) in plain(passage.text)
    elif draft.claim_type == "outage_policy_selection":
        if {"RULE-3", "KB-1004"}.issubset(available):
            policy = re.sub(r"[*_`]", "", available["RULE-3"].text).lower()
            kb = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            claim = draft.text.lower()
            supported = (
                re.search(r"longer than\s+4 consecutive hours", policy) is not None
                and re.search(r"within\s+30 calendar days", policy) is not None
                and "not capped by" in policy
                and "not limited by" in kb
                and "$75" in claim
                and ("not capped" in claim or "not limited" in claim)
                and "rule 2" in claim
            )
    elif draft.claim_type == "outage_calculation_method":
        if {"RULE-3", "KB-1004"}.issubset(available):
            rule3 = re.sub(r"[*_`]", "", available["RULE-3"].text).lower()
            kb4 = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            claim = draft.text.lower()
            supported = all((
                "monthly recurring charge ÷ 30" in rule3,
                "full days" in rule3,
                "monthly recurring charge ÷ 30" in kb4,
                "full days" in kb4,
                "monthly recurring charge" in claim,
                "full days" in claim,
                "÷ 30" in claim,
            ))
    elif draft.claim_type == "outage_eligibility":
        if {"RULE-3", "KB-1004", "KB-1005"}.issubset(available):
            rule3 = re.sub(r"[*_`]", "", available["RULE-3"].text).lower()
            kb4 = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            kb5 = re.sub(r"[*_`]", "", available["KB-1005"].text).lower()
            claim = draft.text.lower()
            supported = all((
                "valid noc outage ticket number exists covering" in rule3,
                "at your service address" in kb4,
                "covering that address and date range" in kb5,
                re.search(r"longer than\s+4 consecutive hours", rule3) is not None,
                re.search(r"within\s+30 calendar days", rule3) is not None,
                re.search(r"more than\s+4 consecutive hours", kb4) is not None,
                re.search(r"within 30 days of the outage being resolved", kb4) is not None,
                "valid noc outage ticket" in claim,
                "service address" in claim,
                "dates" in claim,
                "4 consecutive hours" in claim,
                "30 calendar days" in claim,
                "resolution" in claim,
            ))
    elif draft.claim_type == "outage_approval_conditions":
        required = {"RULE-4", "RULE-5", "KB-1006", "KB-1007"}
        if required.issubset(available):
            rule4 = re.sub(r"[*_`]", "", available["RULE-4"].text).lower()
            rule5 = re.sub(r"[*_`]", "", available["RULE-5"].text).lower()
            kb6 = re.sub(r"[*_`]", "", available["KB-1006"].text).lower()
            kb7 = re.sub(r"[*_`]", "", available["KB-1007"].text).lower()
            claim = draft.text.lower()
            policy_supported = all((
                re.search(r"service-outage credit.*?exceeds\s+\$300", rule4, re.DOTALL),
                re.search(r"outage exceeds\s+30 consecutive days", rule4),
                "cannot be verified" in rule4,
                re.search(r"second billing dispute.*?within\s+90 days.*?prior resolved dispute", rule4, re.DOTALL),
                "fraud indicators are present" in rule4,
                "tier 2 billing specialist" in rule4,
                "service-outage credit (kb-1004) exceeds $300.00" in kb6,
                "outage exceeds 30 consecutive days" in kb6,
                "second dispute within 90 days of a prior resolved dispute" in kb6,
                "within 90 days of a previous resolved dispute" in kb7,
                "fraud indicators are present" in kb7,
            ))
            claim_supported = all((
                "$300" in claim,
                "30 consecutive days" in claim,
                "cannot be verified" in claim,
                "90 days" in claim and "prior resolved dispute" in claim,
                "fraud indicator" in claim,
                "tier 2" in claim,
            ))
            false_75_gate = bool(re.search(r"(?:above|over|exceed(?:s|ing)?)\s+\$75.{0,80}(?:automatically|requires?|triggers?).{0,40}tier\s*2", claim))
            supported = bool(policy_supported and claim_supported and not false_75_gate)
    elif draft.claim_type == "repeat_fraud_policy":
        required = {"RULE-4", "RULE-5", "KB-1006", "KB-1007"}
        if required.issubset(available):
            rule5 = re.sub(r"[*_`]", "", available["RULE-5"].text).lower()
            kb7 = re.sub(r"[*_`]", "", available["KB-1007"].text).lower()
            claim = draft.text.lower()
            supported = all((
                "second billing dispute within 90 days" in rule5,
                "prior resolved dispute" in rule5,
                "flagged fraud indicator" in rule5,
                "tier 2 billing specialist" in rule5,
                "within 90 days of a previous resolved dispute" in kb7,
                "fraud indicators are present" in kb7,
                "second dispute" in claim or "second billing dispute" in claim,
                "90 days" in claim,
                "prior resolved dispute" in claim,
                "fraud" in claim,
                "tier 2" in claim,
            ))
    elif draft.claim_type == "dispute_policy":
        required = {"RULE-1", "KB-1001"}
        if required.issubset(available):
            rule1 = re.sub(r"[*_`]", "", available["RULE-1"].text).lower()
            kb1 = re.sub(r"[*_`]", "", available["KB-1001"].text).lower()
            claim = draft.text.lower()
            supported = all((
                "60 calendar days" in rule1,
                "tier 2 billing specialist sign-off" in rule1,
                "not guaranteed resolution" in rule1,
                "specific line item or charge" in rule1,
                "specific charge" in kb1,
                "60 calendar days" in claim,
                "tier 2" in claim,
                "not guaranteed" in claim,
                "specific invoice charge" in claim or "specific charge" in claim,
            ))
            elapsed = draft.derived_inputs.get("elapsed_days")
            if elapsed is not None:
                match = re.search(r"within\s+(\d+) calendar days", rule1)
                try:
                    days = int(elapsed)
                    window = int(match.group(1)) if match else -1
                    expected = "within" if 0 <= days <= window else "outside"
                    supported = supported and window >= 0 and f"{days}-day interval is {expected}" in claim
                except (TypeError, ValueError, OverflowError):
                    supported = False
    elif draft.claim_type == "dispute_vs_outage_credit":
        required = {"RULE-1", "KB-1001", "RULE-3", "KB-1004"}
        if required.issubset(available):
            rule1 = re.sub(r"[*_`]", "", available["RULE-1"].text).lower()
            kb1 = re.sub(r"[*_`]", "", available["KB-1001"].text).lower()
            rule3 = re.sub(r"[*_`]", "", available["RULE-3"].text).lower()
            kb4 = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            claim = draft.text.lower()
            supported = all((
                "specific line item or charge" in rule1,
                "determine which policy governs the disputed charge" in kb1,
                "service-outage credit" in rule3,
                "separate policy track from rule 2" in rule3,
                "service-outage credit" in kb4,
                "billing dispute" in claim,
                "separate" in claim,
                "service-outage credit" in claim,
                "rule 1" in claim,
                "rule 3" in claim,
            ))
    elif draft.claim_type == "deadline":
        supported = {"RULE-3", "KB-1005"}.issubset(available) and bool(draft.derived_inputs.get("request_deadline"))
        deadline_value = str(draft.derived_inputs.get("request_deadline", ""))
        policy = re.sub(r"[*_` ]", "", available.get("RULE-3").text).lower() if "RULE-3" in available else ""
        kb5 = re.sub(r"[*_` ]", "", available.get("KB-1005").text).lower() if "KB-1005" in available else ""
        rule_window = re.search(r"within(\d+)calendardays", policy)
        kb_window = re.search(r"within(\d+)days", kb5)
        source_window = int(rule_window.group(1)) if rule_window else None
        kb_window_days = int(kb_window.group(1)) if kb_window else None
        claim = re.sub(r"\s+", " ", draft.text).lower()
        if source_window is None or kb_window_days != source_window:
            supported = False
        else:
            window_phrase = f"within {source_window} calendar days"
            supported = supported and window_phrase in claim
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline_value):
                try:
                    resolution = date.fromisoformat(str(draft.derived_inputs["resolution_date"]))
                    request = date.fromisoformat(str(draft.derived_inputs["request_date"]))
                    deadline = date.fromisoformat(deadline_value)
                    resolution_evidence = any(
                        p.source_kind == "transcript" and p.passage_id.startswith("TRANSCRIPT-") and p.passage_id != "TRANSCRIPT-METADATA"
                        and re.search(rf"\b{month_name[resolution.month]}\s+{resolution.day}(?:st|nd|rd|th)?\b", p.text, re.IGNORECASE)
                        for p in available.values()
                    )
                    metadata = available.get("TRANSCRIPT-METADATA")
                    request_supported = metadata is not None and request.strftime("%B %d, %Y").replace(" 0", " ").lower() in metadata.text.lower()
                    def rendered_dates(text: str) -> set[date]:
                        values: set[date] = set()
                        pattern = r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b"
                        for month, day, year in re.findall(pattern, text, re.IGNORECASE):
                            try:
                                values.add(datetime.strptime(f"{month} {day} {year}", "%B %d %Y").date())
                            except ValueError:
                                continue
                        return values

                    dates_supported = rendered_dates(draft.text) == {resolution, request, deadline}
                    delta = (request - resolution).days
                    negative_timing = bool(re.search(
                        r"\b(?:not timely|isn't timely|wasn't timely|untimely|outside (?:the )?(?:documented )?window|beyond (?:the )?(?:documented )?window)\b",
                        claim,
                    ))
                    uncertain_timing = bool(re.search(
                        r"\b(?:unknown whether|unclear whether|can't tell whether|cannot tell whether|not sure whether|may or may not|might be|could be|possibly|perhaps)\b",
                        claim,
                    ))
                    positive_timing = bool(re.search(r"\b(?:is|was|remains)\s+timely\b|\band\s+timely\b", claim)) and not negative_timing
                    timing_supported = not uncertain_timing and ((0 <= delta <= source_window and positive_timing) or (not 0 <= delta <= source_window and negative_timing))
                    elapsed_claims = {int(value) for value in re.findall(r"\b(\d+)\s+days?\s+(?:later|after|since)\b", claim)}
                    supported = (
                        supported and resolution_evidence and request_supported
                        and deadline == resolution + timedelta(days=source_window)
                        and draft.derived_inputs.get("request_day_delta") == delta
                        and dates_supported and timing_supported
                        and (not elapsed_claims or elapsed_claims == {delta})
                    )
                except (KeyError, ValueError, OverflowError):
                    supported = False
            else:
                # Informational answers may state the policy window without case dates.
                stated_windows = {int(value) for value in re.findall(r"\b(\d+)\s+calendar days\b", claim)}
                supported = supported and stated_windows == {source_window}
    elif draft.claim_type == "account_check_pending":
        required = {"RULE-4", "RULE-5", "KB-1006", "KB-1007"}
        claim = draft.text.lower()
        pending_language = any(term in claim for term in ("not established", "does not determine", "unknown", "unconfirmed", "pending"))
        positive_text = _strip_negated_issuance_claims(claim)
        affirmative_status = re.search(
            r"\b(?!(?:no|not|never)\s+)(?:fraud|fraud indicator|repeat-dispute checks?)\s+(?:has been|was|is|are)\s+(?:clear|cleared|found)\b",
            positive_text,
        ) or _has_affirmative_issuance_claim(positive_text) or re.search(r"\bno fraud was found\b", claim)
        supported = required.issubset(available) and pending_language and affirmative_status is None
        status = "requires_account_check" if supported else "unsupported"
    elif draft.claim_type == "ticket_approval_unknown":
        if {"RULE-4", "KB-1006"}.issubset(available):
            rule4 = re.sub(r"[*_`]", "", available["RULE-4"].text).lower()
            kb6 = re.sub(r"[*_`]", "", available["KB-1006"].text).lower()
            claim = draft.text.lower()
            ticket_reported = any(p.source_kind == "transcript" and "ticket" in p.text.lower() for p in available.values())
            status_unknown = "does not establish credit approval" in claim or "authorization is not established" in claim
            affirmative_status = _has_affirmative_issuance_claim(claim)
            supported = "cannot be verified" in rule4 and "ticket can't be verified" in kb6 and status_unknown and not affirmative_status
            if ticket_reported:
                supported = supported and "out-" in " ".join(p.text.lower() for p in available.values() if p.source_kind == "transcript")
        status = "requires_account_check" if supported else "unsupported"
    elif draft.claim_type == "plan_charge_scope":
        if "KB-1004" in available:
            source = re.sub(r"[*_`]", "", available["KB-1004"].text).lower()
            claim = draft.text.lower()
            supported = "recurring plan charge only" in source and "recurring plan charge" in claim and "only" in claim
    elif draft.claim_type == "general_goodwill":
        if {"RULE-2", "KB-1002"}.issubset(available):
            policy = re.sub(r"[*_`]", "", available["RULE-2"].text).lower()
            kb = re.sub(r"[*_`]", "", available["KB-1002"].text).lower()
            claim = draft.text.lower()
            claim_amounts = {Decimal(value) for value in re.findall(r"\$(\d+(?:\.\d{1,2})?)", claim)}
            source_amounts = {Decimal(value) for source in (policy, kb) for value in re.findall(r"\$(\d+(?:\.\d{1,2})?)", source)}
            per_incident = re.search(r"\b(?:per|each)[- ]incident\b", claim) is not None
            explicitly_not_per_incident = re.search(r"\bnot\s+(?:a\s+)?per[- ]incident\b", claim) is not None
            incident_scoped = per_incident and not explicitly_not_per_incident
            supported = (
                "confirmed" in policy and "billing error" in policy and "rolling 12-month period" in policy
                and "confirmed billing errors" in kb and "rolling 12-month period" in kb
                and "account" in claim and ("cumulative" in claim or "total" in claim)
                and not incident_scoped
                and claim_amounts.issubset(source_amounts)
                and ("tier 2" not in claim or "tier 2" in policy and "tier 2" in kb)
            )
    elif draft.claim_type == "billing_dispute_process":
        if "KB-1001" in available:
            source = re.sub(r"[*_`]", "", available["KB-1001"].text).lower()
            claim = draft.text.lower()
            source_omits_document_checklist = not any(term in source for term in ("required documents", "supporting documents", "receipts required"))
            supported = all((
                source_omits_document_checklist,
                "does not specify" in claim or "doesn't specify" in claim,
                "supporting documents" in claim or "paperwork" in claim,
                "confirm the account holder's identity" in source,
                "date, amount, description" in source,
                "date, amount, and description" in claim,
                "billing" in claim,
            ))
    elif draft.claim_type.startswith("kb:"):
        passage_id = draft.claim_type.removeprefix("kb:")
        passage = available.get(passage_id)
        if passage is not None:
            source = re.sub(r"[*_`]", "", passage.text).lower()
            claim = draft.text.lower()
            claim_amounts = {Decimal(value) for value in re.findall(r"\$(\d+(?:\.\d{1,2})?)", claim)}
            source_amounts = {Decimal(value) for value in re.findall(r"\$(\d+(?:\.\d{1,2})?)", source)}
            supported = claim_amounts.issubset(source_amounts)
            if passage_id == "KB-1008":
                if "retention" in claim or "loyalty" in claim:
                    supported = supported and "retention" in source
                if "considering cancellation" in claim or "cancellation" in claim:
                    supported = supported and "actively considering cancellation" in source
                if "customer retention queue" in claim:
                    supported = supported and "customer retention queue" in source
                if "billing dispute" in claim or "outage" in claim:
                    separate_claim = any(term in claim for term in ("separate", "not a substitute", "cannot resolve", "cannot be used to resolve"))
                    supported = supported and separate_claim and "not a substitute for a billing dispute resolution" in source and "cannot be used to resolve" in source
            elif passage_id == "KB-1009":
                supported = supported and ("device protection" not in claim or "device protection" in source)
                supported = supported and ("device protection team" not in claim or "device protection team" in source)
            elif passage_id == "KB-1010":
                supported = supported and ("plan change" not in claim or "mid-cycle plan change" in source)
                supported = supported and ("prorat" not in claim or "prorat" in source)
                supported = supported and ("formula" not in claim or "monthly charge ÷ 30" in source and "days active" in source)
                if "outage" in claim:
                    positive_authority = re.search(r"\b(?:authoriz\w*|allows?|permits?|approves?)\b", claim) is not None
                    supported = supported and not positive_authority and "unrelated to service-outage credits" in source and "kb-1004" in claim and "separate" in claim
    elif draft.claim_type == "device_deductible":
        if "KB-1009" in available:
            source = re.sub(r"[*_` ]", "", available["KB-1009"].text).lower()
            claim = re.sub(r"[*_` ]", "", draft.text).lower()
            supported = all((
                "nimbusdeviceprotection" in source,
                "deductible($25–$199dependingondevicetier)" in source,
                "$25–$199" in draft.text,
                "depending on device tier" in draft.text.lower(),
            ))
    elif draft.claim_type == "credit_issuance_status":
        # Issuance is only supportable from a logged action record describing that
        # account action. Policy and transcript text cannot prove it occurred.
        action_records = [p for p in available.values() if p.source_kind == "action_record"]
        supported = bool(action_records) and all(draft.text.casefold() in p.text.casefold() for p in action_records)
    elif draft.claim_type == "source_conflict":
        status = "partially_grounded" if len(available) >= 2 else "unsupported"
        supported = len(available) >= 2
    if supported and status == "unsupported":
        status = "grounded"
    citations = tuple({
        "id": passage.passage_id,
        "title": passage.title,
        "source_file": passage.source_file,
        "line_start": passage.line_start,
        "line_end": passage.line_end,
        "excerpt": _safe_excerpt(passage.text),
        "source_text": _sanitize_source_text(passage.text),
    } for passage in available.values())
    return MaterialClaim(draft.claim_type, draft.text, ids, status, draft.derived_inputs, citations)
