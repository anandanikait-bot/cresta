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

    def test_full_subject_routes_device_taxes_and_internet_outage_correctly(self):
        self.assertEqual(interpret("Will taxes apply to my device replacement?").intent, "device_protection")
        self.assertEqual(interpret("Will taxes apply to my replacement phone?").intent, "device_protection")
        self.assertEqual(interpret("Does the $75 refund cap apply if my internet stops working?").intent, "service_outage_policy")
        self.assertEqual(interpret("Can I dispute a tax charge on my invoice?").intent, "file_dispute")

    def test_combined_invoice_dispute_and_outage_credit_keeps_both_intents(self):
        result = interpret("I want to dispute the invoice charge and also request a service-outage credit.")
        self.assertEqual(result.intent, "combined_dispute_and_outage")

    def test_combined_request_routing_handles_natural_outage_credit_paraphrases(self):
        for question in (
            "Can I dispute an invoice charge and get a credit for my internet outage?",
            "I want to dispute this bill and request a credit for the outage.",
        ):
            with self.subTest(question=question):
                self.assertEqual(interpret(question).intent, "combined_dispute_and_outage")

    def test_ambiguous_coverage_requests_clarification_intent(self):
        result = interpret("Can I get compensation for 11 days without coverage?")
        self.assertTrue(result.ambiguity)
        self.assertEqual(result.intent, "clarification")

    def test_device_and_outage_topics_are_not_silently_collapsed(self):
        result = interpret("Does Device Protection cover my internet outage?")
        self.assertEqual(result.intent, "clarification")
        self.assertTrue(result.ambiguity)

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
