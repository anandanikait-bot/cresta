from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Truth(str, Enum):
    TRUE = "known_true"
    FALSE = "known_false"
    UNKNOWN = "unknown"


class TicketStatus(str, Enum):
    MISSING = "missing"
    UNKNOWN = "unknown"
    AGENT_REPORTED = "agent_reported"
    VERIFIED = "verified"
    UNVERIFIABLE = "unverifiable"


class Action(str, Enum):
    ANSWER = "ANSWER"
    CLARIFY = "CLARIFY"
    ESCALATE = "ESCALATE"
    DECLINE = "DECLINE"


def jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, ".2f")
    if is_dataclass(value):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    return value


@dataclass(frozen=True)
class Passage:
    passage_id: str
    title: str
    text: str
    source_file: str
    line_start: int
    line_end: int
    source_kind: str
    authority: int

    def excerpt(self, limit: int = 360) -> str:
        text = " ".join(self.text.split())
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    def to_dict(self) -> dict[str, Any]:
        return jsonable(self)


@dataclass(frozen=True)
class ConversationEvent:
    timestamp: str
    speaker: str
    text: str
    turn_index: int
    source_id: str
    line_number: int


@dataclass(frozen=True)
class CreditEntry:
    issued_date: date
    amount: Decimal
    category: str


@dataclass(frozen=True)
class CaseFacts:
    request_date: date | None = None
    invoice_statement_date: date | None = None
    disputed_item: str | None = None
    monthly_recurring_charge: Decimal | None = None
    outage_start: datetime | None = None
    outage_end: datetime | None = None
    service_address_match: Truth = Truth.UNKNOWN
    ticket_id: str | None = None
    ticket_status: TicketStatus = TicketStatus.UNKNOWN
    independent_ticket_verification: Truth = Truth.UNKNOWN
    account_verified: Truth = Truth.UNKNOWN
    prior_resolved_dispute: Truth = Truth.UNKNOWN
    prior_resolved_dispute_date: date | None = None
    fraud_indicator: Truth = Truth.UNKNOWN
    credit_history: tuple[CreditEntry, ...] | None = None
    billing_error_confirmed: Truth = Truth.UNKNOWN
    requested_goodwill_amount: Decimal | None = None
    fact_provenance: dict[str, dict[str, Any]] = field(default_factory=dict, compare=False)

    @property
    def outage_duration_hours(self) -> Decimal | None:
        if self.outage_start is None or self.outage_end is None or self.outage_end < self.outage_start:
            return None
        seconds = Decimal(str((self.outage_end - self.outage_start).total_seconds()))
        return seconds / Decimal("3600")

    @property
    def full_outage_days(self) -> int | None:
        hours = self.outage_duration_hours
        return None if hours is None else int(hours // Decimal("24"))

    @property
    def request_days_after_resolution(self) -> int | None:
        if self.request_date is None or self.outage_end is None:
            return None
        return (self.request_date - self.outage_end.date()).days

    def to_public_dict(self) -> dict[str, Any]:
        # Only structured case facts are serialized. Raw transcript metadata (account and
        # payment identifiers) is deliberately never copied into this model.
        return {
            "request_date": jsonable(self.request_date),
            "invoice_statement_date": jsonable(self.invoice_statement_date),
            "disputed_item": self.disputed_item,
            "monthly_recurring_charge": jsonable(self.monthly_recurring_charge),
            "outage_start": jsonable(self.outage_start),
            "outage_end": jsonable(self.outage_end),
            "outage_duration_hours": jsonable(self.outage_duration_hours),
            "full_outage_days": self.full_outage_days,
            "service_address_match": self.service_address_match.value,
            "ticket_id": self.ticket_id,
            "ticket_status": self.ticket_status.value,
            "independent_ticket_verification": self.independent_ticket_verification.value,
            "account_verified": self.account_verified.value,
            "prior_resolved_dispute": self.prior_resolved_dispute.value,
            "prior_resolved_dispute_date": jsonable(self.prior_resolved_dispute_date),
            "fraud_indicator": self.fraud_indicator.value,
            "credit_history": "unknown" if self.credit_history is None else jsonable(self.credit_history),
            "billing_error_confirmed": self.billing_error_confirmed.value,
            "fact_provenance": jsonable(self.fact_provenance),
        }


@dataclass(frozen=True)
class SourceBundle:
    rules: tuple[Passage, ...]
    articles: tuple[Passage, ...]
    events: tuple[ConversationEvent, ...]
    event_evidence: tuple[Passage, ...]
    transcript_date: date
    warnings: tuple[str, ...]
    conflicts: tuple[str, ...]
    documents: dict[str, dict[str, str]]

    @property
    def passages(self) -> tuple[Passage, ...]:
        return self.rules + self.articles

    @property
    def by_id(self) -> dict[str, Passage]:
        return {p.passage_id: p for p in self.passages + self.event_evidence}

    @property
    def customer_questions(self) -> tuple[ConversationEvent, ...]:
        return tuple(e for e in self.events if e.speaker == "Customer" and "?" in e.text)


@dataclass(frozen=True)
class Candidate:
    passage_id: str
    title: str
    score: float
    matched_terms: tuple[str, ...]
    reason: str
    excerpt: str


@dataclass(frozen=True)
class Decision:
    selected_rule_id: str | None
    status: str
    credit_amount: Decimal | None = None
    deadline: date | None = None
    reason: str = ""
    exclusions: tuple[str, ...] = ()
    approval_reasons: tuple[str, ...] = ()
    unresolved_checks: tuple[str, ...] = ()
    calculations: dict[str, Any] = field(default_factory=dict)

    @property
    def issuance_authorized(self) -> bool:
        """True only when an outage/goodwill credit is explicitly ready for frontline action."""
        return (
            self.selected_rule_id in {"RULE-2", "RULE-3"}
            and self.status == "eligible_for_frontline_action"
            and self.credit_amount is not None
            and self.credit_amount > Decimal("0.00")
        )


@dataclass(frozen=True)
class MaterialClaim:
    claim_type: str
    text: str
    support_ids: tuple[str, ...]
    status: str
    derived_inputs: dict[str, Any] = field(default_factory=dict)
    citations: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class TurnTrace:
    timestamp: str | None
    question: str
    intent: str
    language_mode: str
    facts_snapshot: dict[str, Any]
    retrieved: tuple[Candidate, ...]
    decision: Decision
    claims: tuple[MaterialClaim, ...]
    action: Action
    answer: str
    provenance_notes: tuple[str, ...] = ()
    evidence_ledger: tuple[dict[str, Any], ...] = ()
    additional_decisions: tuple[Decision, ...] = ()

    @property
    def evidence_ids(self) -> list[str]:
        return list(dict.fromkeys(sid for claim in self.claims for sid in claim.support_ids))

    def to_dict(self) -> dict[str, Any]:
        data = jsonable(self)
        data["decision"]["issuance_authorized"] = self.decision.issuance_authorized
        for raw, decision in zip(data.get("additional_decisions", []), self.additional_decisions):
            raw["issuance_authorized"] = decision.issuance_authorized
        data["evidence_ids"] = self.evidence_ids
        return data
