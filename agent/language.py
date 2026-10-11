from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import CaseFacts


INTENTS = {
    "service_outage_amount", "outage_deadline", "tax_recalculation", "file_dispute",
    "general_goodwill", "retention", "device_protection", "plan_change_proration",
    "mixed_retention_outage", "service_outage_policy", "repeat_fraud_policy", "unknown",
}


@dataclass(frozen=True)
class QuestionInterpretation:
    intent: str
    search_terms: tuple[str, ...]
    ambiguity: bool = False


def interpret(question: str, facts: CaseFacts | None = None) -> QuestionInterpretation:
    normalized = re.sub(r"\s+", " ", question.lower()).strip()
    has_outage = any(term in normalized for term in ("outage", "service was out", "service interruption", "service restoration", "service restored", "network went down", "without service"))
    has_retention = any(term in normalized for term in ("retention", "canceling", "cancellation", "loyalty credit"))
    if has_outage and has_retention:
        intent = "mixed_retention_outage"
    elif any(term in normalized for term in ("tax", "surcharge", "regulatory fee", "regulatory fees", "levy", "levies", "government fee", "government fees")):
        intent = "tax_recalculation"
    elif any(term in normalized for term in ("how do i file", "how to file", "open a dispute", "submit a billing dispute", "dispute a charge")):
        intent = "file_dispute"
    elif any(term in normalized for term in ("dispute", "challenge", "contest")) and any(term in normalized for term in ("invoice", "statement", "charge", "60 day", "60-day", "within", "after", "window", "timely", "deadline")):
        intent = "file_dispute"
    elif any(term in normalized for term in ("deadline", "how long", "time limit", "due date", "submit a request", "within how many days", "after the outage is fixed")):
        unrelated_topic = any(term in normalized for term in ("warranty", "device protection", "phone", "replacement", "lost device", "stolen device"))
        has_case_outage = facts is not None and (facts.outage_start is not None or facts.ticket_id is not None)
        intent = "outage_deadline" if has_outage or (has_case_outage and not unrelated_topic) else "unknown"
    elif any(term in normalized for term in ("repeat dispute", "second dispute", "prior resolved dispute", "fraud indicator", "fraud review", "fraud indicators")):
        intent = "repeat_fraud_policy"
    elif has_retention:
        intent = "retention"
    elif any(term in normalized for term in ("device protection", "replacement credit", "lost device", "stolen device")):
        intent = "device_protection"
    elif any(term in normalized for term in ("mid-cycle", "mid cycle", "plan change", "upgrade my plan", "downgrade my plan")) or ("prorat" in normalized and not has_outage):
        intent = "plan_change_proration"
    elif has_outage and (any(term in normalized for term in ("$75", "seventy five", "cap", "limit", "ceiling", "what rule", "which policy", "how does the policy", "qualify", "tier 2", "tier two", "approval", "escalat", "threshold", "formula", "$300", "300 dollars", "30 consecutive days", "ticket cannot be verified", "cannot verify the ticket")) or "kb-1010" in normalized or "plan change formula" in normalized):
        intent = "service_outage_policy"
    elif has_outage:
        intent = "service_outage_amount"
    elif any(term in normalized for term in ("$75", "seventy five", "goodwill", "refund limit", "refund cap", "credit cap", "billing error")):
        intent = "service_outage_amount" if facts and (facts.outage_start or facts.ticket_id) and any(term in normalized for term in ("$75", "seventy five", "refund limit", "refund cap", "credit cap")) else "general_goodwill"
    else:
        intent = "unknown"
    return QuestionInterpretation(intent, tuple(dict.fromkeys(re.findall(r"[a-z0-9$]+", normalized))), False)


def validate_model_payload(raw_payload: str, question: str, facts: CaseFacts | None = None) -> tuple[QuestionInterpretation, str | None]:
    """Validate an optional language adapter result; no policy decisions are accepted from it."""
    fallback = interpret(question, facts)
    try:
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict) or set(payload) != {"intent", "search_terms", "ambiguity"}:
            raise ValueError("unexpected fields")
        if payload["intent"] not in INTENTS or not isinstance(payload["search_terms"], list) or not all(isinstance(x, str) for x in payload["search_terms"]):
            raise ValueError("invalid intent or search terms")
        if not isinstance(payload["ambiguity"], bool):
            raise ValueError("ambiguity must be boolean")
        return QuestionInterpretation(payload["intent"], tuple(payload["search_terms"]), payload["ambiguity"]), None
    except (json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        return fallback, f"Optional language output rejected ({exc}); used offline interpretation."
