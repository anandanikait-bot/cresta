from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any


class RuleConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RuleSet:
    version: str
    currency: str
    rules: dict[str, dict[str, Any]]
    source_authority: dict[str, int]
    supported_calculators: tuple[str, ...]

    def __getitem__(self, key: str) -> dict[str, Any]:
        return self.rules[key]


def load_rules(path: str | Path) -> RuleSet:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuleConfigError(f"Rules configuration is missing: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RuleConfigError(f"Rules configuration is invalid at {path}: {exc}") from exc
    required = {f"RULE-{n}" for n in range(1, 6)}
    if not isinstance(raw, dict) or not isinstance(raw.get("rules"), dict) or set(raw["rules"]) != required:
        raise RuleConfigError("Rules configuration must declare RULE-1 through RULE-5 exactly")
    for key in sorted(required):
        if raw["rules"][key].get("source_section_id") != key:
            raise RuleConfigError(f"{key} must be bound to its authoritative source section")
    calculators = raw.get("supported_calculators", [])
    if raw.get("currency") != "USD" or not isinstance(calculators, list) or set(calculators) != {"outage_credit"}:
        raise RuleConfigError("Unsupported currency or calculator configuration")
    for key, numeric in (("RULE-1", "dispute_window_calendar_days"), ("RULE-2", "rolling_period_months"), ("RULE-2", "cumulative_limit"), ("RULE-3", "minimum_duration_hours_exclusive"), ("RULE-3", "request_window_calendar_days_inclusive"), ("RULE-4", "outage_credit_tier2_exclusive"), ("RULE-4", "outage_duration_days_tier2_exclusive"), ("RULE-4", "repeat_dispute_window_days_inclusive")):
        if numeric not in raw["rules"][key]:
            raise RuleConfigError(f"{key} is missing configured predicate {numeric}")
    rule2, rule3 = raw["rules"]["RULE-2"], raw["rules"]["RULE-3"]
    if rule2.get("category") != "general_goodwill" or rule3.get("category") != "service_outage":
        raise RuleConfigError("RULE-2 and RULE-3 must remain distinct general-goodwill and service-outage tracks")
    if rule3.get("general_goodwill_cap_applies") is not False or rule3.get("formula") != "outage_credit":
        raise RuleConfigError("RULE-3 must use its named calculator and remain outside the RULE-2 goodwill cap")
    if int(rule3.get("priority", 0)) <= int(rule2.get("priority", 0)):
        raise RuleConfigError("The specific RULE-3 outage track must outrank general RULE-2 goodwill")
    return RuleSet(str(raw.get("version", "unknown")), "USD", raw["rules"], raw.get("source_authority", {}), tuple(calculators))


def validate_rule_bindings(rules: RuleSet, sources: Any) -> tuple[str, ...]:
    """Ensure approved numeric predicates still match their cited policy text."""
    by_id = {p.passage_id: re.sub(r"[*_`]", "", p.text) for p in sources.rules}
    checks = (
        ("RULE-1", r"within\s+(\d+)\s+calendar days", str(rules["RULE-1"]["dispute_window_calendar_days"]), "dispute window"),
        ("RULE-2", r"\$(\d+(?:\.\d{1,2})?)\s+per account", str(Decimal(str(rules["RULE-2"]["cumulative_limit"]))), "goodwill limit"),
        ("RULE-2", r"per account,\s*per rolling\s+(\d+)-month period", str(rules["RULE-2"]["rolling_period_months"]), "rolling goodwill period"),
        ("RULE-3", r"longer than\s+(\d+)\s+consecutive hours", str(rules["RULE-3"]["minimum_duration_hours_exclusive"]), "outage duration"),
        ("RULE-3", r"within\s+(\d+)\s+calendar days", str(rules["RULE-3"]["request_window_calendar_days_inclusive"]), "outage request window"),
        ("RULE-4", r"Service-Outage Credit calculated under Rule 3 exceeds\s+\$(\d+(?:\.\d{1,2})?)", str(Decimal(str(rules["RULE-4"]["outage_credit_tier2_exclusive"]))), "outage amount threshold"),
        ("RULE-4", r"exceeds\s+(\d+)\s+consecutive days", str(rules["RULE-4"]["outage_duration_days_tier2_exclusive"]), "outage duration approval threshold"),
        ("RULE-4", r"within\s+(\d+)\s+days", str(rules["RULE-4"]["repeat_dispute_window_days_inclusive"]), "repeat dispute window"),
    )
    conflicts: list[str] = []
    for passage_id, pattern, expected, label in checks:
        match = re.search(pattern, by_id.get(passage_id, ""), re.IGNORECASE)
        if not match:
            conflicts.append(f"Rule config binding conflict: could not find {label} in {passage_id}.")
        elif Decimal(match.group(1)) != Decimal(expected):
            conflicts.append(f"Rule config binding conflict: configured {label} {expected} does not match {passage_id} value {match.group(1)}.")
    return tuple(conflicts)
