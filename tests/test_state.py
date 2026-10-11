from pathlib import Path
import unittest
from dataclasses import replace

from agent.sources import load_sources
from agent.state import snapshots_for_questions
from agent.models import ConversationEvent, TicketStatus, Truth


ROOT = Path(__file__).resolve().parents[1]


class StateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bundle = load_sources(
            ROOT / "data" / "Nimbus Wireless — Billing Dispute & Refund Policy.md",
            ROOT / "data" / "NimbusCare Knowledge Base — Billing & Credits Excerpt.md",
            ROOT / "data" / "NimbusCare Chat Transcript — Dana Whitfield, October 5, 2026.txt",
        )

    def test_earlier_facts_are_available_and_unknown_later_checks_stay_unknown(self):
        snapshots = snapshots_for_questions(self.bundle)
        early = snapshots[0][1]
        self.assertEqual(early.outage_start.year, 2026)
        self.assertEqual(early.ticket_status, TicketStatus.AGENT_REPORTED)
        self.assertEqual(early.ticket_id, "OUT-58823")
        self.assertEqual(early.prior_resolved_dispute, Truth.UNKNOWN)
        self.assertEqual(early.fraud_indicator, Truth.UNKNOWN)
        self.assertTrue(all(facts.prior_resolved_dispute == Truth.UNKNOWN and facts.fraud_indicator == Truth.UNKNOWN for _, facts in snapshots))

    def test_by_1409_case_facts_have_reported_provenance_not_live_lookup(self):
        _, facts = snapshots_for_questions(self.bundle)[0]
        self.assertEqual(facts.account_verified, Truth.TRUE)
        self.assertEqual(facts.ticket_status, TicketStatus.AGENT_REPORTED)
        self.assertEqual(facts.independent_ticket_verification, Truth.UNKNOWN)
        self.assertEqual(facts.monthly_recurring_charge, __import__("decimal").Decimal("225.00"))
        self.assertEqual(facts.outage_duration_hours, 264)
        self.assertEqual(facts.full_outage_days, 11)

    def test_case_state_never_retains_or_renders_payment_secrets(self):
        for _, facts in snapshots_for_questions(self.bundle):
            public = str(facts.to_public_dict())
            self.assertNotIn("NMB-7745213", public)
            self.assertNotIn("4471", public)

    def test_ticket_reference_without_affirmative_coverage_does_not_confirm_address(self):
        events = tuple(
            replace(event, text="I cannot verify ticket OUT-58823; it does not cover your address.")
            if event.speaker == "Agent" and "OUT-58823" in event.text else event
            for event in self.bundle.events
        )
        snapshots = snapshots_for_questions(replace(self.bundle, events=events))
        facts = snapshots[0][1]
        self.assertEqual(facts.ticket_id, "OUT-58823")
        self.assertEqual(facts.ticket_status, TicketStatus.UNVERIFIABLE)
        self.assertEqual(facts.service_address_match, Truth.FALSE)
        self.assertEqual(facts.independent_ticket_verification, Truth.UNKNOWN)

        unverifiable_events = tuple(
            replace(event, text="I cannot verify ticket OUT-58823 at this time.")
            if event.speaker == "Agent" and "OUT-58823" in event.text else event
            for event in self.bundle.events
        )
        facts = snapshots_for_questions(replace(self.bundle, events=unverifiable_events))[0][1]
        self.assertEqual(facts.service_address_match, Truth.UNKNOWN)


if __name__ == "__main__":
    unittest.main()
