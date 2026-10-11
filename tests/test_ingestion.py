from pathlib import Path
import tempfile
import unittest

from agent.sources import SourceLoadError, load_sources


ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md"
KB = ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md"
TRANSCRIPT = ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt"


class IngestionTests(unittest.TestCase):
    def test_parses_numbered_rules_and_actual_kb_headings(self):
        bundle = load_sources(POLICY, KB, TRANSCRIPT)
        self.assertEqual([p.passage_id for p in bundle.rules], [f"RULE-{n}" for n in range(1, 6)])
        self.assertEqual(len(bundle.articles), 10)
        self.assertEqual(bundle.articles[-1].passage_id, "KB-1010")
        self.assertTrue(any("header says 9" in w.lower() for w in bundle.warnings))

    def test_passages_preserve_exact_excerpt_and_line_provenance(self):
        bundle = load_sources(POLICY, KB, TRANSCRIPT)
        outage = bundle.by_id["KB-1004"]
        original = KB.read_text(encoding="utf-8").splitlines()
        self.assertIn("recurring plan charge only", outage.text)
        self.assertEqual(outage.line_start, next(i for i, line in enumerate(original, 1) if line.startswith("### KB-1004")))
        self.assertEqual(outage.source_file, KB.name)

    def test_missing_source_fails_with_named_path(self):
        with self.assertRaisesRegex(SourceLoadError, "missing-policy.md"):
            load_sources(ROOT / "missing-policy.md", KB, TRANSCRIPT)

    def test_malformed_policy_headings_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "policy.md"
            bad.write_text("# Policy\nNo numbered rules here.\n", encoding="utf-8")
            with self.assertRaisesRegex(SourceLoadError, "RULE-1"):
                load_sources(bad, KB, TRANSCRIPT)

    def test_transcript_events_are_chronological_and_questions_keep_turns(self):
        bundle = load_sources(POLICY, KB, TRANSCRIPT)
        self.assertEqual([e.timestamp for e in bundle.customer_questions], ["14:09", "14:11", "14:13"])
        self.assertTrue(all("NMB-7745213" not in e.text and "4471" not in e.text for e in bundle.customer_questions))


if __name__ == "__main__":
    unittest.main()
