from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Mapping

from .models import Candidate, Passage


def _sanitize_source_text(text: str) -> str:
    text = re.sub(r"\bNMB-\d+\b", "[REDACTED ACCOUNT]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:card\s+)?(?:ending|last\s+four|last\s+4)(?:\s+in)?\s+(?:is\s+)?\d{4}\b", "[REDACTED PAYMENT DETAIL]", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[REDACTED PAYMENT DETAIL]", text)
    return text


def _safe_excerpt(text: str, limit: int = 300) -> str:
    text = " ".join(_sanitize_source_text(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


@dataclass(frozen=True)
class EvidenceRecord:
    access_type: str
    passage_id: str
    reason: str
    status: str
    title: str | None = None
    source_file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    source_kind: str | None = None
    excerpt: str | None = None
    source_text: str | None = None
    rank: int | None = None
    score: float | None = None
    matched_terms: tuple[str, ...] = ()
    transcript_timestamp: str | None = None
    transcript_speaker: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class EvidenceLedger:
    """Per-turn access log; claims can see only passages already logged here."""

    def __init__(self, source_by_id: Mapping[str, Passage]):
        self._source_by_id = dict(source_by_id)
        self._logged: dict[str, Passage] = {}
        self._records: list[EvidenceRecord] = []

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records)

    def record_ranked(self, candidates: list[Candidate] | tuple[Candidate, ...]) -> None:
        for rank, candidate in enumerate(candidates, start=1):
            passage = self._source_by_id.get(candidate.passage_id)
            if passage is not None:
                self._logged[passage.passage_id] = passage
            self._records.append(self._record(
                "ranked_retrieval", candidate.passage_id, candidate.reason,
                passage, "found" if passage is not None else "missing", rank,
                candidate.score, candidate.matched_terms,
            ))

    def fetch_exact(self, passage_id: str, reason: str) -> Passage | None:
        if not reason.strip():
            raise ValueError("Exact-ID evidence fetches require a reason")
        passage = self._source_by_id.get(passage_id)
        if passage is not None:
            self._logged[passage_id] = passage
        self._records.append(self._record(
            "exact_id", passage_id, reason, passage,
            "found" if passage is not None else "missing",
        ))
        return passage

    def passages_for(self, passage_ids: list[str] | tuple[str, ...]) -> dict[str, Passage]:
        return {passage_id: self._logged[passage_id] for passage_id in passage_ids if passage_id in self._logged}

    def logged_passages(self) -> dict[str, Passage]:
        return dict(self._logged)

    def _record(
        self, access_type: str, passage_id: str, reason: str,
        passage: Passage | None, status: str, rank: int | None = None,
        score: float | None = None, matched_terms: tuple[str, ...] = (),
    ) -> EvidenceRecord:
        timestamp = speaker = None
        match = re.fullmatch(r"TRANSCRIPT-(\d{2}:\d{2})", passage_id)
        if match:
            timestamp = match.group(1)
            speaker_match = re.search(r"Transcript\s+\d{2}:\d{2}\s+(Customer|Agent)", passage.title if passage else "")
            speaker = speaker_match.group(1) if speaker_match else None
        return EvidenceRecord(
            access_type=access_type,
            passage_id=passage_id,
            reason=reason,
            status=status,
            title=passage.title if passage else None,
            source_file=passage.source_file if passage else None,
            line_start=passage.line_start if passage else None,
            line_end=passage.line_end if passage else None,
            source_kind=passage.source_kind if passage else None,
            excerpt=_safe_excerpt(passage.text) if passage else None,
            source_text=_sanitize_source_text(passage.text) if passage else None,
            rank=rank,
            score=score,
            matched_terms=matched_terms,
            transcript_timestamp=timestamp,
            transcript_speaker=speaker,
        )
