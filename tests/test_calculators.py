import unittest
from datetime import date, datetime
from decimal import Decimal

from agent.calculators import calendar_months_ago, outage_credit, elapsed_days


class CalculatorTests(unittest.TestCase):
    def test_outage_credit_uses_decimal_and_rounds_to_cents(self):
        self.assertEqual(outage_credit(Decimal("225.00"), 11), Decimal("82.50"))
        self.assertEqual(outage_credit(Decimal("1.00"), 1), Decimal("0.03"))

    def test_no_full_day_means_zero_without_inventing_partial_day_credit(self):
        self.assertEqual(outage_credit(Decimal("225.00"), 0), Decimal("0.00"))

    def test_calendar_deadline_is_inclusive(self):
        self.assertEqual(elapsed_days(date(2026, 9, 21), date(2026, 10, 21)), 30)
        self.assertEqual(elapsed_days(date(2026, 9, 21), date(2026, 10, 22)), 31)

    def test_rolling_twelve_month_boundary_uses_calendar_months(self):
        self.assertEqual(calendar_months_ago(date(2025, 3, 31), 12), date(2024, 3, 31))
        self.assertEqual(calendar_months_ago(date(2025, 3, 31), 1), date(2025, 2, 28))


if __name__ == "__main__":
    unittest.main()
