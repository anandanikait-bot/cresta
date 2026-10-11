import json
import unittest

from agent.language import interpret, validate_model_payload
from agent.models import CaseFacts


class LanguageTests(unittest.TestCase):
    def test_paraphrases_route_without_policy_decision_authority(self):
        self.assertEqual(interpret("Do government fees change when the outage allowance posts?").intent, "tax_recalculation")
        self.assertEqual(interpret("How long after service restoration can I ask for relief?").intent, "outage_deadline")
        self.assertEqual(interpret("How is my plan adjustment prorated mid-cycle?").intent, "plan_change_proration")

    def test_dispute_deadline_uses_rule_one_and_unrelated_warranty_is_not_outage(self):
        self.assertEqual(interpret("What is the deadline to dispute an invoice charge?").intent, "file_dispute")
        self.assertEqual(interpret("What is the deadline for my phone warranty?").intent, "unknown")
        self.assertEqual(interpret("What is the deadline for my phone warranty?", CaseFacts(ticket_id="OUT-58823")).intent, "unknown")

    def test_invalid_optional_language_json_falls_back_to_offline_intent(self):
        result, warning = validate_model_payload('{"intent":"approve_credit","amount":1000}', "Will regulatory levies change after the outage credit?")
        self.assertEqual(result.intent, "tax_recalculation")
        self.assertIsNotNone(warning)

    def test_strict_valid_language_shape_contains_only_interpretation_fields(self):
        payload = json.dumps({"intent": "tax_recalculation", "search_terms": ["fees", "outage"], "ambiguity": False})
        result, warning = validate_model_payload(payload, "Will fees change?")
        self.assertEqual(result.intent, "tax_recalculation")
        self.assertIsNone(warning)


if __name__ == "__main__":
    unittest.main()
