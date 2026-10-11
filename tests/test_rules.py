import json
from pathlib import Path
import tempfile
import unittest

from agent.rules import RuleConfigError, load_rules, validate_rule_bindings
from agent.sources import load_sources


ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


class RuleConfigTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = load_sources(
            DATA / "Nimbus Wireless — Billing Dispute & Refund Policy.md",
            DATA / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md",
            DATA / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt",
        )

    def test_approved_rule_values_are_bound_to_authoritative_policy(self):
        rules = load_rules(ROOT / "config" / "nimbus_policy_rules.json")
        self.assertEqual(validate_rule_bindings(rules, self.sources), ())
        self.assertTrue(all(rules[key]["source_section_id"] == key for key in rules.rules))

    def test_changed_configured_threshold_is_a_fail_closed_conflict(self):
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["rules"]["RULE-3"]["minimum_duration_hours_exclusive"] = 5
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            conflicts = validate_rule_bindings(load_rules(path), self.sources)
        self.assertTrue(any("outage duration" in conflict for conflict in conflicts))

    def test_changed_rolling_goodwill_period_is_a_fail_closed_conflict(self):
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["rules"]["RULE-2"]["rolling_period_months"] = 1
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            conflicts = validate_rule_bindings(load_rules(path), self.sources)
        self.assertTrue(any("rolling goodwill period" in conflict for conflict in conflicts))

    def test_unknown_calculator_and_missing_rule_are_rejected(self):
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["supported_calculators"] = ["eval"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(RuleConfigError):
                load_rules(path)
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["rules"]["RULE-3"]["source_section_id"] = "KB-1010"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(RuleConfigError):
                load_rules(path)

    def test_rule_three_must_remain_a_separate_exception_track(self):
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["rules"]["RULE-3"]["general_goodwill_cap_applies"] = True
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(RuleConfigError):
                load_rules(path)
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        raw["supported_calculators"] = ["outage_credit", "eval"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(RuleConfigError):
                load_rules(path)
        raw = json.loads((ROOT / "config" / "nimbus_policy_rules.json").read_text())
        del raw["rules"]["RULE-3"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rules.json"
            path.write_text(json.dumps(raw))
            with self.assertRaises(RuleConfigError):
                load_rules(path)


if __name__ == "__main__":
    unittest.main()
