from pathlib import Path
from dataclasses import replace
import unittest

from agent.evidence import EvidenceLedger
from agent.grounding import ClaimDraft, validate_claim
from agent.retrieval import retrieve
from agent.sources import load_sources

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class EvidenceLedgerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_sources(
            DATA / "Nimbus Wireless — Billing Dispute & Refund Policy.md",
            DATA / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md",
            DATA / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt",
        )

    def test_claim_validation_cannot_read_unlogged_source_passages(self):
        ledger = EvidenceLedger(self.bundle.by_id)
        draft = ClaimDraft(
            "outage_policy_selection", "Rule 3 is not capped by Rule 2's $75 limit.",
            ["RULE-3", "KB-1004"],
        )
        self.assertEqual(validate_claim(draft, ledger).status, "unsupported")
        ledger.fetch_exact("RULE-3", reason="outage policy claim")
        ledger.fetch_exact("KB-1004", reason="outage policy claim")
        self.assertEqual(validate_claim(draft, ledger).status, "grounded")

    def test_unrestricted_source_map_is_not_accepted_by_claim_validation(self):
        draft = ClaimDraft(
            "outage_policy_selection", "Rule 3 is not capped by Rule 2's $75 limit.",
            ["RULE-3", "KB-1004"],
        )
        self.assertEqual(validate_claim(draft, self.bundle.by_id).status, "unsupported")

    def test_ranked_and_missing_fetch_events_keep_reason_and_provenance(self):
        ledger = EvidenceLedger(self.bundle.by_id)
        candidates = retrieve("device protection deductible", self.bundle.passages, allowed_ids={"KB-1009"})
        ledger.record_ranked(candidates)
        ledger.fetch_exact("DOES-NOT-EXIST", reason="verify requested source id")
        records = [record.to_dict() for record in ledger.records]
        self.assertEqual(records[0]["access_type"], "ranked_retrieval")
        self.assertEqual(records[0]["rank"], 1)
        self.assertEqual(records[0]["passage_id"], "KB-1009")
        self.assertIn("device", " ".join(records[0]["matched_terms"]))
        self.assertTrue(records[0]["source_file"])
        self.assertGreater(records[0]["line_start"], 0)
        missing = records[-1]
        self.assertEqual(missing["status"], "missing")
        self.assertEqual(missing["reason"], "verify requested source id")

    def test_missing_fetch_cannot_support_and_transcript_provenance_is_preserved(self):
        ledger = EvidenceLedger(self.bundle.by_id)
        missing = ClaimDraft("kb:KB-9999", "This answer is supported.", ["KB-9999"])
        ledger.fetch_exact("KB-9999", reason="requested device article was unavailable")
        self.assertEqual(validate_claim(missing, ledger).status, "unsupported")
        ledger.fetch_exact("TRANSCRIPT-14:08", reason="check what the agent reported")
        transcript = ledger.records[-1].to_dict()
        self.assertEqual(transcript["transcript_timestamp"], "14:08")
        self.assertEqual(transcript["transcript_speaker"], "Agent")
        self.assertEqual(transcript["source_kind"], "transcript")
        self.assertEqual(transcript["source_file"], self.bundle.documents["transcript"]["file"])
        self.assertIn("confirmed outage ticket", transcript["source_text"].lower())

    def test_ledger_and_citation_text_redact_payment_details(self):
        ledger = EvidenceLedger(self.bundle.by_id)
        ledger.fetch_exact("TRANSCRIPT-14:04", reason="audit customer-provided verification details")
        text = ledger.records[-1].to_dict()["source_text"]
        self.assertNotIn("NMB-7745213", text)
        self.assertNotIn("4471", text)

    def test_ledger_keeps_full_source_text_for_long_passages(self):
        passage = self.bundle.by_id["KB-1009"]
        source_text = "Device Protection details. " + ("supported source text. " * 400)
        ledger = EvidenceLedger({"KB-1009": replace(passage, text=source_text)})
        ledger.fetch_exact("KB-1009", reason="preserve complete citation content")
        recorded = ledger.records[-1].to_dict()["source_text"]
        self.assertEqual(recorded, source_text)
        claim = validate_claim(ClaimDraft("kb_quote", source_text, ["KB-1009"]), ledger)
        self.assertEqual(claim.citations[0]["source_text"], source_text)


if __name__ == "__main__":
    unittest.main()
