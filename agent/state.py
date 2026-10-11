from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime
from decimal import Decimal

from .models import CaseFacts, SourceBundle, TicketStatus, Truth


_OUTAGE = re.compile(
    r"went down on\s+([A-Za-z]+)\s+(\d+)(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM).*?"
    r"until\s+([A-Za-z]+)\s+(\d+)(?:st|nd|rd|th)?\s+at\s+(\d{1,2})(?::(\d{2}))?\s*(AM|PM)",
    re.IGNORECASE,
)
_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12}


def _clock(year: int, month_name: str, day: str, hour: str, minute: str | None, ampm: str) -> datetime:
    hour_value = int(hour) % 12 + (12 if ampm.upper() == "PM" else 0)
    return datetime(year, _MONTHS[month_name.lower()], int(day), hour_value, int(minute or 0))


def snapshots_for_questions(bundle: SourceBundle) -> tuple[tuple[object, CaseFacts], ...]:
    facts = CaseFacts(request_date=bundle.transcript_date, fact_provenance={
        "request_date": {"source_id": "TRANSCRIPT-METADATA", "quote": f"Date: {bundle.transcript_date:%B %-d, %Y}"},
    })
    result: list[tuple[object, CaseFacts]] = []
    for event in bundle.events:
        quote = event.text
        stamp = event.timestamp
        if event.speaker == "Agent" and ("you're verified" in quote.lower() or "you are verified" in quote.lower()):
            provenance = dict(facts.fact_provenance)
            provenance["account_verified"] = {"speaker": "Agent", "timestamp": stamp, "quote": quote}
            facts = replace(facts, account_verified=Truth.TRUE, fact_provenance=provenance)
        if event.speaker == "Customer":
            outage = _OUTAGE.search(quote)
            if outage:
                month1, day1, hour1, minute1, ampm1, month2, day2, hour2, minute2, ampm2 = outage.groups()
                start = _clock(bundle.transcript_date.year, month1, day1, hour1, minute1, ampm1)
                end = _clock(bundle.transcript_date.year, month2, day2, hour2, minute2, ampm2)
                charge_match = re.search(r"\$(\d+(?:\.\d{1,2})?)\s+recurring charge", quote, re.IGNORECASE)
                invoice_match = re.search(r"invoice dated\s+([A-Za-z]+)\s+(\d+)(?:st|nd|rd|th)?", quote, re.IGNORECASE)
                provenance = dict(facts.fact_provenance)
                provenance.update({
                    "outage_start": {"speaker": "Customer", "timestamp": stamp, "quote": quote},
                    "outage_end": {"speaker": "Customer", "timestamp": stamp, "quote": quote},
                    "disputed_item": {"speaker": "Customer", "timestamp": stamp, "quote": quote},
                })
                if charge_match:
                    provenance["monthly_recurring_charge"] = {"speaker": "Customer", "timestamp": stamp, "quote": quote}
                facts = replace(
                    facts, outage_start=start, outage_end=end,
                    monthly_recurring_charge=Decimal(charge_match.group(1)) if charge_match else facts.monthly_recurring_charge,
                    invoice_statement_date=datetime(bundle.transcript_date.year, _MONTHS[invoice_match.group(1).lower()], int(invoice_match.group(2))).date() if invoice_match else facts.invoice_statement_date,
                    disputed_item="monthly recurring charge" if "recurring charge" in quote.lower() else facts.disputed_item,
                    fact_provenance=provenance,
                )
        if event.speaker == "Agent":
            ticket = re.search(r"\b(OUT-\d{5})\b", quote)
            if ticket:
                provenance = dict(facts.fact_provenance)
                provenance["ticket_id"] = {"speaker": "Agent", "timestamp": stamp, "quote": quote}
                lowered = quote.lower()
                cannot_verify = re.search(r"\b(?:cannot|can't|could not|couldn't|unable to)\s+(?:independently\s+)?verify\b", lowered)
                address_mismatch = re.search(
                    r"\b(?:does not|doesn't|doesnt|is not|isn't|cannot|can't)\s+(?:cover|match|apply to|serve)\s+(?:your|the)\s+(?:service\s+)?address\b"
                    r"|\bnot\s+(?:for|at)\s+(?:your|the)\s+(?:service\s+)?address\b",
                    lowered,
                )
                affirmative_address = re.search(
                    r"\b(?:for|at)\s+(?:your|the)\s+(?:service\s+)?address\b"
                    r"|\b(?:covers?|matches?)\s+(?:your|the)\s+(?:service\s+)?address\b",
                    lowered,
                )
                if address_mismatch:
                    address_match = Truth.FALSE
                elif cannot_verify or not affirmative_address:
                    address_match = Truth.UNKNOWN
                else:
                    address_match = Truth.TRUE
                if address_match != Truth.UNKNOWN:
                    provenance["service_address_match"] = {"speaker": "Agent", "timestamp": stamp, "quote": quote}
                facts = replace(
                    facts,
                    ticket_id=ticket.group(1),
                    ticket_status=TicketStatus.UNVERIFIABLE if cannot_verify else TicketStatus.AGENT_REPORTED,
                    service_address_match=address_match,
                    fact_provenance=provenance,
                )
        if event.speaker == "Customer" and "?" in quote:
            result.append((event, facts))
    return tuple(result)
