"""
Utility helper functions.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any, Optional


def parse_date_flexible(date_str: Any) -> Optional[date]:
    """Parse a date from various formats."""
    if date_str is None:
        return None
    if isinstance(date_str, (date, datetime)):
        return date_str if isinstance(date_str, date) else date_str.date()
    s = str(date_str).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%Y%m%d", "%m-%d-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def format_currency(val: float, decimals: int = 2) -> str:
    """Format number as currency string."""
    return f"${val:,.{decimals}f}"


def format_percentage(val: float, decimals: int = 5) -> str:
    """Format decimal as percentage string."""
    return f"{val * 100:.{decimals}f}%"


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division."""
    if denominator == 0:
        return default
    return numerator / denominator


def calculate_days_between(start: date, end: date) -> int:
    """Calculate number of days between two dates."""
    return (end - start).days


def get_prior_month_dates(payment_date: date) -> tuple[date, date]:
    """
    Get accrual period start and end dates (prior month 20th to current month 19th).
    """
    if payment_date.month == 1:
        accrual_start = date(payment_date.year - 1, 12, 20)
    else:
        accrual_start = date(payment_date.year, payment_date.month - 1, 20)

    accrual_end = date(payment_date.year, payment_date.month, 19)
    return accrual_start, accrual_end


def annualize_rate(periodic_rate: float, periods_per_year: int = 12) -> float:
    """Convert periodic rate to annual rate."""
    return (1 + periodic_rate) ** periods_per_year - 1


def monthly_rate(annual_rate: float) -> float:
    """Convert annual rate to monthly rate."""
    return (1 + annual_rate) ** (1 / 12) - 1
