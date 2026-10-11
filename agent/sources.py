from __future__ import annotations

import hashlib
import re
from datetime import date, datetime
from pathlib import Path

from .models import ConversationEvent, Passage, SourceBundle


class SourceLoadError(ValueError):
    """An authoritative source is missing or cannot be safely parsed."""


def _read(path: Path) -> str:
    if not path.is_file():
        raise SourceLoadError(f"Required source file is missing: {path}")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SourceLoadError(f"Could not read required source {path}: {exc}") from exc


def _sections(text: str, path: Path, pattern: re.Pattern[str], kind: str, authority: int) -> tuple[Passage, ...]:
    lines = text.splitlines()
    starts = [(index, pattern.match(line)) for index, line in enumerate(lines) if pattern.match(line)]
    if not starts:
        expected = "RULE-1" if kind == "policy" else "KB-1001"
        raise SourceLoadError(f"No recognized headings in {path}; expected {expected} section headings")
    passages: list[Passage] = []
    for offset, (start, match) in enumerate(starts):
        end = starts[offset + 1][0] - 1 if offset + 1 < len(starts) else len(lines) - 1
        title = lines[start].lstrip("# ").strip()
        if kind == "policy":
            rule_number = re.match(r"## Rule (\d+)\b", lines[start]).group(1)
            passage_id = f"RULE-{rule_number}"
        else:
            article_number = match.group(1)
            passage_id = f"KB-{article_number}"
        body = "\n".join(lines[start : end + 1]).strip()
        passages.append(Passage(passage_id, title, body, path.name, start + 1, end + 1, kind, authority))
    return tuple(passages)


def _parse_events(text: str, path: Path) -> tuple[tuple[ConversationEvent, ...], date]:
    date_match = re.search(r"^Date:\s*(.+?)\s*$", text, re.MULTILINE)
    if not date_match:
        raise SourceLoadError(f"Transcript is missing a recognizable Date header: {path}")
    try:
        transcript_date = datetime.strptime(date_match.group(1), "%B %d, %Y").date()
    except ValueError as exc:
        raise SourceLoadError(f"Transcript date is malformed in {path}: {date_match.group(1)}") from exc
    event_pattern = re.compile(r"^\[(\d{2}:\d{2})\]\s+(Customer|Agent)\s+\([^)]*\):\s*(.*)$")
    events: list[ConversationEvent] = []
    for line_number, line in enumerate(text.splitlines(), 1):
        match = event_pattern.match(line)
        if not match:
            continue
        events.append(ConversationEvent(match.group(1), match.group(2), match.group(3), len(events), f"TRANSCRIPT-{match.group(1)}", line_number))
    if not events:
        raise SourceLoadError(f"Transcript contains no timestamped customer/agent turns: {path}")
    return tuple(events), transcript_date


def _critical_conflicts(rules: tuple[Passage, ...], articles: tuple[Passage, ...]) -> tuple[str, ...]:
    by_id = {passage.passage_id: re.sub(r"[*_`]", "", passage.text) for passage in rules + articles}
    conflicts: list[str] = []
    required = {"RULE-1", "RULE-2", "RULE-3", "RULE-4", "RULE-5", "KB-1001", "KB-1002", "KB-1003", "KB-1004", "KB-1005", "KB-1006", "KB-1007"}
    missing = sorted(required - set(by_id))
    if missing:
        return tuple(f"Authoritative section {passage_id} is missing." for passage_id in missing)

    def first_amount(source: str, passage_id: str) -> str | None:
        match = re.search(r"\$(\d+(?:\.\d{1,2})?)", source)
        if not match:
            conflicts.append(f"Could not verify a monetary threshold in {passage_id}.")
            return None
        return match.group(1)

    rule_cap = first_amount(by_id["RULE-2"], "RULE-2")
    kb_cap = first_amount(by_id["KB-1002"], "KB-1002")
    if rule_cap != kb_cap:
        conflicts.append(f"Source conflict: RULE-2 general-credit cap is ${rule_cap}; KB-1002 states ${kb_cap}.")

    def outage_hours(source: str, passage_id: str) -> str | None:
        match = re.search(r"(?:longer than|more than|exceeds?)\s*(\d+)\s+consecutive hours", source, re.IGNORECASE)
        if not match:
            conflicts.append(f"Could not verify the outage-duration threshold in {passage_id}.")
            return None
        return match.group(1)

    policy_hours = outage_hours(by_id["RULE-3"], "RULE-3")
    kb_hours = outage_hours(by_id["KB-1004"], "KB-1004")
    if policy_hours != kb_hours:
        conflicts.append(f"Source conflict: RULE-3 outage threshold is {policy_hours} hours; KB-1004 states {kb_hours} hours.")

    def number(pattern: str, passage_id: str, label: str) -> str | None:
        match = re.search(pattern, by_id[passage_id], re.IGNORECASE | re.DOTALL)
        if not match:
            conflicts.append(f"Could not verify {label} in {passage_id}.")
            return None
        return match.group(1)

    dispute_days = number(r"within\s+(\d+)\s+calendar days", "RULE-1", "the dispute window")
    kb_dispute_days = number(r"within\s+(\d+)\s+days of the statement date", "KB-1001", "the dispute window")
    if dispute_days != kb_dispute_days:
        conflicts.append(f"Source conflict: RULE-1 dispute window is {dispute_days} days; KB-1001 states {kb_dispute_days} days.")

    outage_window = number(r"within\s+(\d+)\s+calendar days", "RULE-3", "the outage request window")
    for passage_id in ("KB-1004", "KB-1005"):
        kb_window = number(r"within\s+(\d+)\s+days", passage_id, "the outage request window")
        if outage_window != kb_window:
            conflicts.append(f"Source conflict: RULE-3 request window is {outage_window} days; {passage_id} states {kb_window} days.")

    policy_repeat_days = number(r"within\s+(\d+)\s+days\s+of a prior resolved dispute", "RULE-4", "the repeat-dispute window")
    for passage_id in ("KB-1006", "KB-1007"):
        kb_repeat_days = number(r"within\s+(\d+)\s+days\s+of (?:a prior resolved|a previous resolved) dispute", passage_id, "the repeat-dispute window")
        if policy_repeat_days != kb_repeat_days:
            conflicts.append(f"Source conflict: RULE-4 repeat-dispute window is {policy_repeat_days} days; {passage_id} states {kb_repeat_days} days.")

    outage_threshold = number(r"Service-Outage Credit calculated under Rule 3 exceeds\s+\$(\d+(?:\.\d{1,2})?)", "RULE-4", "the outage credit approval threshold")
    for passage_id, pattern in (("KB-1005", r"calculated credit is \$(\d+(?:\.\d{1,2})?) or less"), ("KB-1006", r"exceeds \$(\d+(?:\.\d{1,2})?)")):
        kb_threshold = number(pattern, passage_id, "the outage credit approval threshold")
        if outage_threshold != kb_threshold:
            conflicts.append(f"Source conflict: RULE-4 outage approval threshold is ${outage_threshold}; {passage_id} states ${kb_threshold}.")

    outage_days = number(r"underlying outage exceeds\s+(\d+)\s+consecutive days", "RULE-4", "the outage duration approval threshold")
    for passage_id, pattern in (("KB-1005", r"outage lasted\s+(\d+)\s+days or fewer"), ("KB-1006", r"outage exceeds\s+(\d+)\s+consecutive days")):
        kb_days = number(pattern, passage_id, "the outage duration approval threshold")
        if outage_days != kb_days:
            conflicts.append(f"Source conflict: RULE-4 outage duration threshold is {outage_days} days; {passage_id} states {kb_days} days.")

    return tuple(conflicts)


def load_sources(policy_path: str | Path, kb_path: str | Path, transcript_path: str | Path) -> SourceBundle:
    paths = {"policy": Path(policy_path), "kb": Path(kb_path), "transcript": Path(transcript_path)}
    contents = {key: _read(path) for key, path in paths.items()}
    rules = _sections(contents["policy"], paths["policy"], re.compile(r"^## Rule (\d+)\b"), "policy", 1)
    articles = _sections(contents["kb"], paths["kb"], re.compile(r"^### KB-(\d{4})\b"), "kb", 2)
    if [p.passage_id for p in rules] != [f"RULE-{number}" for number in range(1, 6)]:
        raise SourceLoadError(f"Policy headings must contain RULE-1 through RULE-5 in order: {paths['policy']}")
    events, transcript_date = _parse_events(contents["transcript"], paths["transcript"])

    warnings: list[str] = []
    declared = re.search(r"\b(\d+)\s+articles?\b", contents["kb"], re.IGNORECASE)
    if declared and int(declared.group(1)) != len(articles):
        warnings.append(f"KB header says {declared.group(1)} but {len(articles)} article headings were parsed; actual headings were used.")
    event_evidence = tuple(
        Passage(e.source_id, f"Transcript {e.timestamp} {e.speaker}", e.text, paths["transcript"].name, e.line_number, e.line_number, "transcript", 3)
        for e in events
    )
    metadata_line = next((i for i, line in enumerate(contents["transcript"].splitlines(), 1) if line.startswith("Date:")), None)
    metadata_text = next((line for line in contents["transcript"].splitlines() if line.startswith("Date:")), "")
    metadata_evidence = Passage("TRANSCRIPT-METADATA", "Transcript request date", metadata_text, paths["transcript"].name, metadata_line or 1, metadata_line or 1, "transcript", 3)
    documents = {
        key: {"file": path.name, "sha256": hashlib.sha256(contents[key].encode("utf-8")).hexdigest()}
        for key, path in paths.items()
    }
    return SourceBundle(rules, articles, events, event_evidence + (metadata_evidence,), transcript_date, tuple(warnings), _critical_conflicts(rules, articles), documents)
