from pathlib import Path
import unittest

from agent.retrieval import retrieve
from agent.sources import load_sources


ROOT = Path(__file__).resolve().parents[1]


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_sources(
            ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md",
            ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md",
            ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt",
        )

    def test_tax_paraphrase_retrieves_separate_tax_and_plan_charge_sources(self):
        ids = [x.passage_id for x in retrieve("Do government fees change when the outage allowance posts?", self.bundle.passages)]
        self.assertIn("KB-1003", ids[:4])
        self.assertIn("KB-1004", ids[:4])

    def test_outage_formula_does_not_make_plan_change_rule_governing(self):
        ids = [x.passage_id for x in retrieve("Use the daily proration for my service outage", self.bundle.passages)]
        self.assertIn("RULE-3", ids[:5])
        self.assertIn("KB-1004", ids[:5])

    def test_candidate_filter_can_exclude_unrelated_credit_programs(self):
        results = retrieve("retention credit to settle an outage dispute", self.bundle.passages, allowed_ids={"RULE-3", "KB-1004", "KB-1008"})
        self.assertTrue(all(x.passage_id in {"RULE-3", "KB-1004", "KB-1008"} for x in results))


if __name__ == "__main__":
    unittest.main()
