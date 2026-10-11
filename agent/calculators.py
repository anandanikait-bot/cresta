from calendar import monthrange
from datetime import date
from decimal import Decimal, ROUND_HALF_UP


CENT = Decimal("0.01")


def outage_credit(monthly_recurring_charge: Decimal, full_outage_days: int) -> Decimal:
    if monthly_recurring_charge < 0 or full_outage_days < 0:
        raise ValueError("charge and outage days must not be negative")
    return ((monthly_recurring_charge / Decimal("30")) * Decimal(full_outage_days)).quantize(CENT, rounding=ROUND_HALF_UP)


def elapsed_days(start: date, end: date) -> int:
    return (end - start).days


def calendar_months_ago(value: date, months: int) -> date:
    absolute_month = value.year * 12 + value.month - 1 - months
    year, month_index = divmod(absolute_month, 12)
    month = month_index + 1
    return date(year, month, min(value.day, monthrange(year, month)[1]))
