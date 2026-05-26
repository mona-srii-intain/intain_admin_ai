"""
ABS/MBS Waterfall Computation Engine.

Computes the full payment waterfall for a given deal and payment date.

Key Computations:
  1. Aggregate loantape data (collections, balances, delinquency)
  2. Compute Net WAC from loan-level data
  3. Compute pass-through rates for floating-rate classes
  4. Compute interest due for each class
  5. Execute interest priority of payments waterfall
  6. Execute principal priority of payments waterfall
  7. Execute monthly excess cashflow waterfall
  8. Compute collateral performance metrics (CDR, CPR, delinquency buckets)
  9. Compute fee and expense payments
  10. Evaluate trigger tests / events
"""
from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from models.deal import CertificateClass, DealConfig, FeeConfig, WaterfallStep
from models.waterfall import (
    AccountEntry,
    ClassPaymentSummary,
    CollateralBucket,
    CollateralPerformanceHistory,
    CollateralRates,
    CollateralRealizedLossEntry,
    DashboardKPI,
    DelinquencyMatrix,
    DelinquencyMatrixCell,
    DistributionAllocation,
    EventTest,
    ExpenseEntry,
    FeeEntry,
    LoanDetail,
    ServicerBalance,
    StructuralFeatures,
    TriggerChip,
    WaterfallResult,
    WaterfallTraceStep,
)

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Days count for 30-day month
DAYS_30 = 30
DAYS_360 = 360


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _safe(val: Any, default: float = 0.0) -> float:
    """Safely convert to float."""
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Prior-period state helpers
#
# Several report sections (1(f) Cumulative, 2(c) rolling rates, 2(d) cumulative
# realized loss, 3(b) Accounts, 4 Fees beginning shortfall, 5 Expenses,
# trigger evaluation) depend on values from the immediately prior period and
# in some cases from a window of prior periods. These helpers read from raw
# WaterfallResult dicts loaded by deal_store.get_prior_waterfall /
# deal_store.list_prior_waterfall_results.
# ---------------------------------------------------------------------------

def _prior_class_field(
    prior_result: Optional[Dict],
    class_name: str,
    field: str,
    default: float = 0.0,
) -> float:
    """Read a numeric field from one class's entry in a prior period result."""
    if not prior_result:
        return default
    for cd in prior_result.get("class_details", []) or []:
        if cd.get("class_name") == class_name:
            try:
                return float(cd.get(field) or default)
            except (TypeError, ValueError):
                return default
    return default


def _prior_top_field(
    prior_result: Optional[Dict],
    field: str,
    default: float = 0.0,
) -> float:
    """Read a top-level numeric field from a prior period result."""
    if not prior_result:
        return default
    try:
        return float(prior_result.get(field) or default)
    except (TypeError, ValueError):
        return default


def _prior_fee_shortfall(prior_result: Optional[Dict], fee_name: str) -> float:
    """Look up a fee's prior-period ``ending_shortfall`` to seed this period's
    ``beginning_shortfall``. Zero if the fee was not present prior."""
    if not prior_result:
        return 0.0
    for f in prior_result.get("fees_detail", []) or []:
        if f.get("fee_name") == fee_name:
            try:
                return float(f.get("ending_shortfall") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _prior_account_balance(prior_result: Optional[Dict], account_name: str) -> float:
    """Look up a reserve account's prior-period post-payment ending balance."""
    if not prior_result:
        return 0.0
    for a in prior_result.get("reserve_accounts", []) or []:
        if a.get("account_name") == account_name:
            try:
                return float(a.get("ending_balance_post_payment") or 0.0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _rolling_average(values: List[float], window: int) -> float:
    """Trailing rolling average over the last ``window`` values. Empty → 0."""
    if not values:
        return 0.0
    tail = values[-window:] if window and window > 0 else values
    return (sum(tail) / len(tail)) if tail else 0.0


def _build_rate_series_from_history(
    prior_history: List[Dict],
    current_cpr_1m: float,
    current_cdr_1m: float,
) -> Tuple[List[float], List[float]]:
    """Build chronological 1-month CPR and CDR series from prior periods
    plus the current period. Oldest first, current last."""
    cpr_series: List[float] = []
    cdr_series: List[float] = []
    for h in prior_history or []:
        rates = (h.get("collateral_rates") or {}) if isinstance(h, dict) else {}
        try:
            cpr_series.append(float(rates.get("cpr_1m") or 0.0))
        except (TypeError, ValueError):
            cpr_series.append(0.0)
        try:
            cdr_series.append(float(rates.get("cdr_1m") or 0.0))
        except (TypeError, ValueError):
            cdr_series.append(0.0)
    cpr_series.append(current_cpr_1m)
    cdr_series.append(current_cdr_1m)
    return cpr_series, cdr_series


def _rolling_60plus_delinquency_pct(
    prior_history: List[Dict],
    current_60plus_balance: float,
    current_pool_balance: float,
    current_a1_balance: float,
    window: int = 6,
) -> float:
    """Trailing N-period average of (60+ DPD balance) / (pool − A-1 balance).

    Default window=6 matches the Delinquency Trigger definition in the
    indenture (6-month rolling average of HELOCs 60+ days delinquent as a
    percent of certificate principal excluding Class A-1).
    """
    series: List[float] = []
    delinq_buckets = (
        "60-89 Days", "90-119 Days", "120-149 Days", "150-179 Days", "180+ Days",
    )
    for h in prior_history or []:
        pool = _safe((h or {}).get("current_pool_balance"))
        a1 = 0.0
        for cd in (h or {}).get("class_details", []) or []:
            if cd.get("class_name") == "A-1":
                a1 = _safe(cd.get("ending_principal"))
                break
        denom = pool - a1
        d60 = 0.0
        for b in (h or {}).get("performance_buckets", []) or []:
            if b.get("bucket") in delinq_buckets:
                d60 += _safe(b.get("amount"))
        series.append((d60 / denom) if denom > 0 else 0.0)

    curr_denom = current_pool_balance - current_a1_balance
    curr_pct = (current_60plus_balance / curr_denom) if curr_denom > 0 else 0.0
    series.append(curr_pct)

    return _rolling_average(series, window)


def _compute_accrual_dates(payment_date: date) -> Tuple[date, date, int]:
    """
    Compute accrual start / end dates and days accrued for a payment date.
    
    Convention (typical ABS):
      - Accrual period: 20th of prior month through 19th of current month (for 20th payment)
      - Or: first of prior month through last of prior month (for 25th payment)
    We default to a 30-day period ending on the day before payment date.
    """
    accrual_end = date(payment_date.year, payment_date.month, 19)
    # accrual start = 20th of previous month
    if payment_date.month == 1:
        accrual_start = date(payment_date.year - 1, 12, 20)
    else:
        accrual_start = date(payment_date.year, payment_date.month - 1, 20)

    days = (accrual_end - accrual_start).days + 1
    return accrual_start, accrual_end, days


def _compute_interest(
    principal: float,
    annual_rate: float,
    days: int,
    convention: str,
) -> float:
    """
    Compute interest accrual.
    Conventions:
      actual/360: principal * rate * days / 360
      30/360: principal * rate * 30 / 360
      actual/365: principal * rate * days / 365
    """
    if principal <= 0 or annual_rate <= 0:
        return 0.0
    convention_lower = (convention or "actual/360").lower()
    if "365" in convention_lower:
        return principal * annual_rate * days / 365.0
    elif "30" in convention_lower:
        return principal * annual_rate * DAYS_30 / DAYS_360
    else:  # default actual/360
        return principal * annual_rate * days / DAYS_360


def _compute_weighted_average_rate(
    loans: List[Dict],
    balance_col: str = "CURRENT_PRINCIPAL_BALANCE",
    rate_col: str = "CURRENT_INTEREST_RATE",
) -> float:
    """Compute weighted average interest rate across all loans."""
    total_balance = 0.0
    weighted_rate = 0.0
    for loan in loans:
        bal = _safe(loan.get(balance_col))
        rate = _safe(loan.get(rate_col))
        if bal > 0 and rate > 0:
            total_balance += bal
            weighted_rate += bal * rate
    if total_balance > 0:
        return weighted_rate / total_balance
    return 0.0


def _compute_net_wac(loans: List[Dict], servicing_fee_rate: float) -> float:
    """Compute Net WAC = Gross WAC - weighted average servicing fee."""
    gross_wac = _compute_weighted_average_rate(loans)
    return max(0.0, gross_wac - servicing_fee_rate)


def _is_main_capital_class(c: CertificateClass) -> bool:
    """True when a class is part of the main capital stack (A/M/B classes
    that absorb losses and receive principal distributions).

    Excludes notional / residual / exchangeable classes, classes with
    pass-through rate types (excess_cashflow, io), and the structurally
    pass-through classes X / XS / BX that some indenture extractions
    mis-label as floating subordinate bonds.
    """
    if c.is_notional or c.is_residual or c.is_exchangeable:
        return False
    rate_type = (c.interest_rate_type or "").lower()
    if rate_type in ("excess_cashflow", "io", "residual"):
        return False
    if c.class_name in ("X", "XS", "BX"):
        return False
    return True


def _credit_support_pct(
    target_class: str,
    classes: List[CertificateClass],
    balances: Dict[str, float],
) -> float:
    """Credit support % at one class.

    = (sum of balances of all classes strictly subordinate to target_class)
      ÷ (total balance of all main, non-notional/non-residual cert classes)

    "Subordinate" is determined by ``principal_priority`` — higher value = more
    junior. See ``_is_main_capital_class`` for which classes participate.
    """
    target = next((c for c in classes if c.class_name == target_class), None)
    if target is None:
        return 0.0
    target_pri = target.principal_priority

    main_classes = [c for c in classes if _is_main_capital_class(c)]
    denom = sum(balances.get(c.class_name, 0.0) for c in main_classes)
    if denom <= 0:
        return 0.0

    subordinate_bal = sum(
        balances.get(c.class_name, 0.0)
        for c in main_classes
        if c.principal_priority > target_pri
    )
    return subordinate_bal / denom


def _class_day_count(convention: Optional[str], actual_days: int) -> int:
    """Resolve the day-count number used for one class's interest accrual.

    30/360 convention always uses 30 days regardless of calendar; actual/* uses
    the period's actual calendar days. Returns the integer that should appear
    in the report's "Accrual Days" column for the class.
    """
    conv = (convention or "actual/360").lower()
    if "30" in conv and "30/360" in conv:
        return 30
    if conv.startswith("30"):
        return 30
    return actual_days


def _get_class_rate(cls: CertificateClass, sofr_rate: float, net_wac: float) -> float:
    """
    Determine the effective pass-through rate for a class this period.
    
    Floating: rate = min(SOFR + margin, Net WAC), floored at 0%
    Fixed: use fixed_rate, capped at Net WAC
    """
    rate_type = (cls.interest_rate_type or "fixed").lower()
    if rate_type == "floating":
        margin = cls.margin or 0.0
        raw_rate = sofr_rate + margin
        cap = cls.rate_cap if cls.rate_cap is not None else net_wac
        rate = min(raw_rate, cap) if cap > 0 else raw_rate
        return max(0.0, rate)
    elif rate_type in ("fixed",):
        rate = cls.fixed_rate or 0.0
        cap = cls.rate_cap if cls.rate_cap is not None else net_wac
        rate = min(rate, cap) if cap > 0 else rate
        return max(0.0, rate)
    elif rate_type in ("principal_only",):
        # B-4 style: no interest distributions, principal only
        return 0.0
    elif rate_type in ("residual",):
        return 0.0
    elif rate_type in ("excess_cashflow",):
        # X class: receives monthly excess cashflow, no regular interest
        return 0.0
    elif rate_type in ("exchangeable",):
        # BX: exchanges with depositable classes; no independent rate
        return 0.0
    elif rate_type == "io":
        # IO / Excess Servicing Strip (A-IO-S): fixed rate stated in deal
        return cls.fixed_rate or 0.0
    return cls.fixed_rate or 0.0


# ---------------------------------------------------------------------------
# Loantape aggregation
# ---------------------------------------------------------------------------

def aggregate_loantape(loans: List[Dict]) -> Dict[str, Any]:
    """
    Aggregate loan-level data into pool-level statistics needed for waterfall.
    
    Returns a rich stats dictionary covering:
      - Balance roll (beginning, ending, changes)
      - Collections (interest, principal components)
      - Fees (servicing)
      - Delinquency buckets
      - Servicer breakdown
    """
    stats: Dict[str, Any] = {
        "loan_count": 0,
        "total_beginning_balance": 0.0,
        "total_ending_balance": 0.0,
        "total_interest_collected": 0.0,
        "total_gross_interest": 0.0,
        "total_principal_collected": 0.0,
        "total_scheduled_principal": 0.0,
        "total_curtailments": 0.0,
        "total_pif_principal": 0.0,
        "total_liquidations": 0.0,
        "total_liquidations_count": 0,
        "total_net_liquidation_proceeds": 0.0,
        "total_repurchases": 0.0,
        "total_funded_draws": 0.0,
        "total_capitalized": 0.0,
        "total_servicing_fees": 0.0,
        "total_charge_offs": 0.0,
        "total_recoveries": 0.0,
        # 1-D delinquency buckets (existing reporting)
        "bucket_current": {"balance": 0.0, "count": 0},
        "bucket_1_29": {"balance": 0.0, "count": 0},
        "bucket_30_59": {"balance": 0.0, "count": 0},
        "bucket_60_89": {"balance": 0.0, "count": 0},
        "bucket_90_119": {"balance": 0.0, "count": 0},
        "bucket_120_149": {"balance": 0.0, "count": 0},
        "bucket_150_179": {"balance": 0.0, "count": 0},
        "bucket_180_plus": {"balance": 0.0, "count": 0},
        "bucket_foreclosure": {"balance": 0.0, "count": 0},
        "bucket_bankruptcy": {"balance": 0.0, "count": 0},
        "bucket_reo": {"balance": 0.0, "count": 0},
        "bucket_forbearance": {"balance": 0.0, "count": 0},
        # 2-D delinquency matrix: matrix[(dpd_bucket, disposition)] = {"balance", "count"}
        # dpd_bucket ∈ {Current, 1-29, 30-59, 60-89, 90-119, 120-149, 150-179, 180+}
        # disposition ∈ {Delinquent, Foreclosure, Bankruptcy, REO, Forbearance}
        "delinq_matrix": {},
        # Servicer breakdown
        "servicer_balances": {},
        # Rate stats
        "gross_wac": 0.0,
        "weighted_rate_sum": 0.0,
        # Loan-level detail for PIF / bankruptcy / etc.
        "loans_pif": [],
        "loans_reo": [],
        "loans_foreclosure": [],
        "loans_bankruptcy": [],
        "loans_forbearance": [],
        "loans_modified": [],
        "loans_realized_loss": [],
    }

    total_balance_for_wac = 0.0

    for loan in loans:
        stats["loan_count"] += 1

        ending_bal = _safe(loan.get("CURRENT_PRINCIPAL_BALANCE"))
        beginning_bal = _safe(loan.get("BEGINNING_LOAN_BALANCE") or loan.get("PRIOR_PRINCIPAL_BALANCES"), ending_bal)
        interest = _safe(loan.get("INTEREST_PAYMENT"))
        gross_interest = _safe(loan.get("GROSS_INTEREST") or loan.get("INTEREST_PAYMENT"))
        principal = _safe(loan.get("PRINCIPAL_PAYMENT"))
        sched_principal = _safe(loan.get("PRINCIPAL_PAYMENT_SCHEDULED"))
        curtailments = _safe(loan.get("PRINCIPAL_PAYMENT_CURTAILMENTS"))
        pif = _safe(loan.get("PRINCIPAL_PAYMENT_PIF"))
        liquidated = _safe(loan.get("PRINCIPAL_LIQUIDATED"))
        repurchased = _safe(loan.get("PRINCIPAL_PAYMENT_REPURCHASE"))
        funded = _safe(loan.get("FUNDED") or loan.get("FUNDED_REMIT_BAL"))
        capitalized = _safe(loan.get("CAPITALIZED_AMOUNTS"))
        svc_fees = _safe(loan.get("SERVICING_FEES"))
        charge_offs = _safe(loan.get("CHARGE_OFFS") or loan.get("ALLOCATED_LOSSES"))
        recoveries = _safe(loan.get("CUMULATIVE_RECOVERIES") or loan.get("RECOVERIES"))

        stats["total_beginning_balance"] += beginning_bal
        stats["total_ending_balance"] += ending_bal
        stats["total_interest_collected"] += interest
        stats["total_gross_interest"] += gross_interest
        stats["total_principal_collected"] += principal
        stats["total_scheduled_principal"] += sched_principal
        stats["total_curtailments"] += curtailments
        stats["total_pif_principal"] += pif
        stats["total_liquidations"] += liquidated
        stats["total_repurchases"] += repurchased
        stats["total_funded_draws"] += funded
        stats["total_capitalized"] += capitalized
        stats["total_servicing_fees"] += svc_fees
        stats["total_charge_offs"] += charge_offs
        stats["total_recoveries"] += recoveries

        # WAC accumulation
        rate = _safe(loan.get("CURRENT_INTEREST_RATE"))
        if ending_bal > 0 and rate > 0:
            stats["weighted_rate_sum"] += ending_bal * rate
            total_balance_for_wac += ending_bal

        # Delinquency bucketing — DPD bucket (orthogonal to disposition)
        days_arrears = _safe(loan.get("NUMBER_OF_DAYS_IN_ARREARS"))
        loan_status = str(loan.get("LOAN_STATUS") or loan.get("ACCOUNT_STATUS") or "").lower()
        acct_status = str(loan.get("ACCOUNT_STATUS") or "").lower()

        if days_arrears >= 180:
            dpd_bucket = "180+"
        elif days_arrears >= 150:
            dpd_bucket = "150-179"
        elif days_arrears >= 120:
            dpd_bucket = "120-149"
        elif days_arrears >= 90:
            dpd_bucket = "90-119"
        elif days_arrears >= 60:
            dpd_bucket = "60-89"
        elif days_arrears >= 30:
            dpd_bucket = "30-59"
        elif days_arrears >= 1:
            dpd_bucket = "1-29"
        else:
            dpd_bucket = "Current"

        # Disposition (mutually exclusive — first match wins)
        if "reo" in loan_status or "reo" in acct_status:
            disposition = "REO"
        elif "foreclosure" in loan_status or "foreclosure" in acct_status:
            disposition = "Foreclosure"
        elif "bankruptcy" in loan_status or "bankruptcy" in acct_status or bool(loan.get("BANKRUPTCY_FLAG")):
            disposition = "Bankruptcy"
        elif "forbearance" in loan_status or "forbearance" in acct_status:
            disposition = "Forbearance"
        else:
            disposition = "Delinquent"  # generic — includes Current loans

        # 1-D buckets: keep existing disposition-priority semantics (REO/FC/BK/FB
        # override DPD). This preserves the existing performance_buckets table.
        if disposition == "REO":
            stats["bucket_reo"]["balance"] += ending_bal
            stats["bucket_reo"]["count"] += 1
            stats["loans_reo"].append(_loan_detail(loan))
        elif disposition == "Foreclosure":
            stats["bucket_foreclosure"]["balance"] += ending_bal
            stats["bucket_foreclosure"]["count"] += 1
            stats["loans_foreclosure"].append(_loan_detail(loan))
        elif disposition == "Bankruptcy":
            stats["bucket_bankruptcy"]["balance"] += ending_bal
            stats["bucket_bankruptcy"]["count"] += 1
            stats["loans_bankruptcy"].append(_loan_detail(loan))
        elif disposition == "Forbearance":
            stats["bucket_forbearance"]["balance"] += ending_bal
            stats["bucket_forbearance"]["count"] += 1
            stats["loans_forbearance"].append(_loan_detail(loan))
        else:
            # disposition == "Delinquent" → bucket by DPD
            bucket_key = {
                "Current": "bucket_current",
                "1-29": "bucket_1_29",
                "30-59": "bucket_30_59",
                "60-89": "bucket_60_89",
                "90-119": "bucket_90_119",
                "120-149": "bucket_120_149",
                "150-179": "bucket_150_179",
                "180+": "bucket_180_plus",
            }[dpd_bucket]
            stats[bucket_key]["balance"] += ending_bal
            stats[bucket_key]["count"] += 1

        # 2-D matrix: ALWAYS bucket by (dpd_bucket, disposition) regardless of
        # 1-D collapse. This means a loan in Foreclosure that is 60-89 DPD
        # contributes to cell ("60-89", "Foreclosure").
        cell_key = (dpd_bucket, disposition)
        cell = stats["delinq_matrix"].setdefault(cell_key, {"balance": 0.0, "count": 0})
        cell["balance"] += ending_bal
        cell["count"] += 1

        # Liquidation tracking for Section 2(d)
        if liquidated > 0:
            stats["total_liquidations_count"] += 1
            # Net liquidation proceeds = principal recovered net of liquidation expenses.
            # Loantape does not expose PRINCIPAL_NET_LIQUIDATED, so we use
            # PRINCIPAL_LIQUIDATED less CHARGE_OFFS as the closest proxy.
            stats["total_net_liquidation_proceeds"] += max(0.0, liquidated - charge_offs)

        # PIF tracking
        if pif > 0:
            stats["loans_pif"].append(_loan_detail(loan))

        # Realized loss tracking
        if charge_offs > 0:
            stats["loans_realized_loss"].append(_loan_detail(loan))

        # Modification tracking
        if loan.get("MODIFICATION_FLAG") or loan.get("LOAN_MODIFICATION_TYPE"):
            stats["loans_modified"].append(_loan_detail(loan))

        # Servicer balance tracking
        servicer = str(loan.get("SERVICER_NAME") or loan.get("SERVICER") or "Unknown")
        if servicer not in stats["servicer_balances"]:
            stats["servicer_balances"][servicer] = {
                "beginning_upb": 0.0,
                "ending_upb": 0.0,
                "servicing_fee": 0.0,
                "loan_count": 0,
            }
        stats["servicer_balances"][servicer]["beginning_upb"] += beginning_bal
        stats["servicer_balances"][servicer]["ending_upb"] += ending_bal
        stats["servicer_balances"][servicer]["servicing_fee"] += svc_fees
        stats["servicer_balances"][servicer]["loan_count"] += 1

    # Compute gross WAC
    if total_balance_for_wac > 0:
        stats["gross_wac"] = stats["weighted_rate_sum"] / total_balance_for_wac

    # If scheduled/curtailments/PIF are all zero but total principal > 0,
    # try to infer breakdown from principal collected vs. beginning/ending balance
    if (stats["total_scheduled_principal"] == 0 and
            stats["total_curtailments"] == 0 and
            stats["total_pif_principal"] == 0 and
            stats["total_principal_collected"] > 0):
        # All principal treated as unscheduled (prepayment)
        stats["total_curtailments"] = stats["total_principal_collected"]

    return stats


def _loan_detail(loan: Dict) -> Dict:
    """Extract key fields from a loan record for detail reporting."""
    return {
        "loan_id": str(loan.get("LOAN_ID", "")),
        "beginning_principal": _safe(loan.get("BEGINNING_LOAN_BALANCE") or loan.get("PRIOR_PRINCIPAL_BALANCES")),
        "ending_principal": _safe(loan.get("CURRENT_PRINCIPAL_BALANCE")),
        "interest_paid": _safe(loan.get("INTEREST_PAYMENT")),
        "principal_paid": _safe(loan.get("PRINCIPAL_PAYMENT")),
        "status": str(loan.get("LOAN_STATUS") or loan.get("ACCOUNT_STATUS") or ""),
        "days_delinquent": int(_safe(loan.get("NUMBER_OF_DAYS_IN_ARREARS"))),
        "interest_rate": _safe(loan.get("CURRENT_INTEREST_RATE")),
        "deferred_amount": _safe(loan.get("DEFERRED_BEGINNING_BALANCE")),
        "cumulative_deferred": _safe(loan.get("DEFERRED_ENDING_BALANCE")),
        "realized_loss": _safe(loan.get("ALLOCATED_LOSSES") or loan.get("CHARGE_OFFS")),
    }


# ---------------------------------------------------------------------------
# Fee computation
# ---------------------------------------------------------------------------

def compute_deal_fees(
    deal_config: DealConfig,
    pool_balance: float,
    days: int,
    prior_result: Optional[Dict] = None,
    prior_shortfalls: Optional[Dict[str, float]] = None,
) -> Tuple[float, List[FeeEntry], float, List[FeeEntry]]:
    """
    Compute all transaction fees AND expenses for this distribution period.

    Fees (category="fee") are recurring periodic costs:
      - percentage: pool_balance * fee_rate * days / 360
      - fixed: fixed_amount / 12

    Expenses (category="expense") are irregular costs:
      - fixed: fixed_amount (used directly, not divided by 12)
      - percentage: pool_balance * fee_rate * days / 360

    ``shortfall_carried`` fees/expenses accumulate beginning_shortfall from
    prior period (via ``prior_shortfalls`` or ``prior_result``). Servicing
    fees are deducted at the loan level and are skipped here.

    Returns:
        (total_fees_amount, fee_entries, total_expenses_amount, expense_entries)
    """
    fee_entries: List[FeeEntry] = []
    expense_entries: List[FeeEntry] = []
    total_fees = 0.0
    total_expenses = 0.0

    def _resolve_shortfall(name: str, carry: bool) -> float:
        if not carry:
            return 0.0
        if prior_shortfalls is not None and name in prior_shortfalls:
            try:
                return float(prior_shortfalls.get(name) or 0.0)
            except (TypeError, ValueError):
                return 0.0
        return _prior_fee_shortfall(prior_result, name)

    # ── Pass 1: Fees (category="fee") ──────────────────────────────────────
    fees_only = [f for f in deal_config.fees if (getattr(f, "category", "fee") or "fee") == "fee"]
    for fee in sorted(fees_only, key=lambda f: f.priority):
        if fee.fee_name.lower() in ("servicing fee", "servicer fee"):
            continue

        if fee.fee_type == "percentage" and fee.fee_rate:
            amount = pool_balance * fee.fee_rate * days / DAYS_360
        elif fee.fee_type == "fixed" and fee.fixed_amount:
            amount = fee.fixed_amount / 12.0
        else:
            amount = 0.0

        if fee.fee_cap:
            amount = min(amount, fee.fee_cap / 12.0)
        amount = max(0.0, amount)

        carry = bool(getattr(fee, "shortfall_carried", True))
        beginning_shortfall = _resolve_shortfall(fee.fee_name, carry)
        total_due = beginning_shortfall + amount
        amount_paid = total_due  # assumed fully paid from available funds
        ending_shortfall = max(0.0, total_due - amount_paid) if carry else 0.0

        fee_entries.append(FeeEntry(
            fee_name=fee.fee_name,
            beginning_shortfall=beginning_shortfall,
            current_due=amount,
            total_due=total_due,
            amount_paid=amount_paid,
            ending_shortfall=ending_shortfall,
        ))
        total_fees += amount_paid

    # ── Pass 2: Expenses (category="expense") ──────────────────────────────
    expenses_only = [f for f in deal_config.fees if (getattr(f, "category", "fee") or "fee") == "expense"]
    for exp in sorted(expenses_only, key=lambda f: f.priority):
        if exp.fee_type == "fixed" and exp.fixed_amount:
            # Expenses charged as stated (not divided by 12)
            amount = exp.fixed_amount
        elif exp.fee_type == "percentage" and exp.fee_rate:
            amount = pool_balance * exp.fee_rate * days / DAYS_360
        elif exp.fixed_amount:
            amount = exp.fixed_amount
        else:
            amount = 0.0

        if exp.fee_cap:
            amount = min(amount, exp.fee_cap)
        amount = max(0.0, amount)

        carry = bool(getattr(exp, "shortfall_carried", True))
        beginning_shortfall = _resolve_shortfall(exp.fee_name, carry)
        total_due = beginning_shortfall + amount
        amount_paid = total_due
        ending_shortfall = max(0.0, total_due - amount_paid) if carry else 0.0

        expense_entries.append(FeeEntry(
            fee_name=exp.fee_name,
            beginning_shortfall=beginning_shortfall,
            current_due=amount,
            total_due=total_due,
            amount_paid=amount_paid,
            ending_shortfall=ending_shortfall,
        ))
        total_expenses += amount_paid

    return total_fees, fee_entries, total_expenses, expense_entries


# ---------------------------------------------------------------------------
# CDR / CPR calculation
# ---------------------------------------------------------------------------

def compute_cdr_cpr(
    beginning_balance: float,
    new_defaults: float,
    scheduled_principal: float,
    unscheduled_principal: float,
) -> Tuple[float, float, float, float]:
    """
    Compute CDR and CPR for one period.
    
    CDR (Conditional Default Rate):
      SMM_default = new_defaults / beginning_balance
      CDR = 1 - (1 - SMM_default)^12
    
    CPR (Conditional Prepayment Rate):
      SMM_prepay = unscheduled_principal / (beginning_balance - scheduled_principal)
      CPR = 1 - (1 - SMM_prepay)^12
    
    Returns: (smm_default, cdr_1m, smm_prepay, cpr_1m)
    """
    if beginning_balance <= 0:
        return 0.0, 0.0, 0.0, 0.0

    smm_default = new_defaults / beginning_balance
    cdr_1m = 1.0 - (1.0 - smm_default) ** 12 if smm_default < 1.0 else 1.0

    denom = beginning_balance - scheduled_principal
    if denom > 0 and unscheduled_principal >= 0:
        smm_prepay = unscheduled_principal / denom
        cpr_1m = 1.0 - (1.0 - smm_prepay) ** 12 if smm_prepay < 1.0 else 1.0
    else:
        smm_prepay = 0.0
        cpr_1m = 0.0

    return smm_default, cdr_1m, smm_prepay, cpr_1m


# ---------------------------------------------------------------------------
# Waterfall execution
# ---------------------------------------------------------------------------

def _validate_waterfall_steps(
    steps: List[WaterfallStep],
    valid_class_names: set,
) -> List[WaterfallStep]:
    """
    Filter and validate waterfall steps against the actual classes in the deal.

    Rules:
    - Steps with payment_type 'interest' or 'principal' MUST reference a valid class_name.
      Steps that reference a non-existent class (LLM hallucination) are dropped.
    - Steps with payment_type 'reserve', 'fee', 'excess' are kept regardless (no class needed).
    - If after filtering fewer than half of the 'interest'/'principal' steps remain,
      return an empty list so the caller falls back to the priority-based logic.
    """
    if not steps:
        return steps

    class_steps = [s for s in steps if s.payment_type.lower() in ("interest", "principal")]
    valid_steps = []
    for s in steps:
        ptype = s.payment_type.lower()
        if ptype in ("interest", "principal"):
            if s.class_name and s.class_name in valid_class_names:
                valid_steps.append(s)
            else:
                logger.warning(
                    f"Waterfall step {s.step} references unknown class '{s.class_name}' "
                    f"(valid: {sorted(valid_class_names)}) — dropping step."
                )
        else:
            valid_steps.append(s)

    # If too few class-based steps survived, signal full fallback
    if class_steps:
        surviving_class_steps = [s for s in valid_steps if s.payment_type.lower() in ("interest", "principal")]
        coverage = len(surviving_class_steps) / len(class_steps)
        if coverage < 0.5:
            logger.warning(
                f"Only {len(surviving_class_steps)}/{len(class_steps)} waterfall steps "
                f"reference valid classes ({coverage:.0%}). Falling back to priority-based waterfall."
            )
            return []

    return valid_steps


_FORMULA_SAFE_BUILTINS = {"min": min, "max": max, "abs": abs, "round": round, "sum": sum}


def _eval_formula(
    formula: str,
    available_funds: float,
    interest_due: Dict[str, float],
    balances: Dict[str, float],
    cap_carryover: Optional[Dict[str, float]] = None,
    fee_amounts: Optional[Dict[str, float]] = None,
    reserve_balance: float = 0.0,
    reserve_target: float = 0.0,
    realized_loss: float = 0.0,
    total_interest_due: float = 0.0,
    reserve_balances: Optional[Dict[str, float]] = None,
    reserve_targets: Optional[Dict[str, float]] = None,
) -> Optional[float]:
    """
    Safely evaluate an LLM-generated amount formula.
    Returns the computed amount, or None if evaluation fails.

    Available variables in formula:
      available_funds, interest_due, balances, cap_carryover,
      fee_amounts, reserve_balance, reserve_target, realized_loss, total_interest_due,
      reserve_balances (per-account), reserve_targets (per-account)
    """
    try:
        ns = {
            **_FORMULA_SAFE_BUILTINS,
            "available_funds": available_funds,
            "interest_due": interest_due,
            "balances": balances,
            "cap_carryover": cap_carryover or {},
            "fee_amounts": fee_amounts or {},
            "reserve_balance": reserve_balance,
            "reserve_target": reserve_target,
            "realized_loss": realized_loss,
            "total_interest_due": total_interest_due,
            "reserve_balances": reserve_balances or {},
            "reserve_targets": reserve_targets or {},
        }
        result = eval(formula, {"__builtins__": {}}, ns)  # noqa: S307
        return float(result)
    except Exception as e:
        logger.warning(f"Formula eval failed '{formula}': {e}")
        return None


# ---------------------------------------------------------------------------
# Reserve account lifecycle
# ---------------------------------------------------------------------------

_RESERVE_FORMULA_VARS = (
    "total_beginning_balance",
    "total_ending_balance",
    "original_pool_balance",
    "current_balance",
    "target",
    "floor",
)


def _eval_reserve_formula(
    formula: str,
    total_beginning_balance: float,
    total_ending_balance: float,
    original_pool_balance: float,
    current_balance: float = 0.0,
    target: float = 0.0,
    floor: float = 0.0,
) -> Optional[float]:
    """Restricted-sandbox eval for target_formula / release_formula."""
    try:
        ns = {
            **_FORMULA_SAFE_BUILTINS,
            "total_beginning_balance": total_beginning_balance,
            "total_ending_balance": total_ending_balance,
            "original_pool_balance": original_pool_balance,
            "current_balance": current_balance,
            "target": target,
            "floor": floor,
        }
        return float(eval(formula, {"__builtins__": {}}, ns))  # noqa: S307
    except Exception as e:
        logger.warning(f"Reserve formula eval failed '{formula}': {e}")
        return None


def compute_reserve_accounts(
    deal_config: DealConfig,
    pool_stats: Dict[str, Any],
    beginning_reserve_balances: Dict[str, float],
    remainder_to_reserve: float,
) -> Tuple[List[AccountEntry], float, float]:
    """
    Execute the per-account reserve lifecycle for one period.

    For each ``ReserveAccount``:
      1. Resolve target (target_formula → target_amount → fully-funded).
      2. deposit_needed = max(0, target - beginning_balance), drawn from
         ``remainder_to_reserve``.
      3. release_amount from release_formula / release_condition.
      4. ending_balance = beginning + deposit - release.

    ``release_amount`` is returned in aggregate when the account is configured
    to release to ``available_funds`` so callers can fold it back into the
    waterfall.

    Returns:
        (entries, total_release_to_available_funds, remainder_left)
    """
    entries: List[AccountEntry] = []
    total_release_to_af = 0.0
    remainder = remainder_to_reserve

    total_beg = float(pool_stats.get("total_beginning_balance") or 0.0)
    total_end = float(pool_stats.get("total_ending_balance") or 0.0)
    original_pool = float(deal_config.original_pool_balance or total_beg)

    for ra in deal_config.reserve_accounts:
        beginning = float(beginning_reserve_balances.get(ra.account_name, ra.initial_balance) or 0.0)

        # ── 1. Target ──────────────────────────────────────────────────────
        target: float
        if ra.target_formula:
            evaluated = _eval_reserve_formula(
                ra.target_formula,
                total_beginning_balance=total_beg,
                total_ending_balance=total_end,
                original_pool_balance=original_pool,
                current_balance=beginning,
                target=ra.target_amount or 0.0,
                floor=ra.floor or 0.0,
            )
            target = max(0.0, evaluated) if evaluated is not None else (ra.target_amount or beginning)
        elif ra.target_amount is not None:
            target = max(0.0, ra.target_amount)
        else:
            # Fully funded: target equals beginning (no top-up needed)
            target = beginning

        # ── 2. Deposit needed ──────────────────────────────────────────────
        deposit_needed = max(0.0, target - beginning)
        # Cap the deposit by available remainder
        deposit = min(deposit_needed, max(0.0, remainder))
        remainder = max(0.0, remainder - deposit)

        current_balance = beginning + deposit
        floor = float(ra.floor or 0.0)

        # ── 3. Release ─────────────────────────────────────────────────────
        release_amount = 0.0
        if ra.release_formula:
            evaluated = _eval_reserve_formula(
                ra.release_formula,
                total_beginning_balance=total_beg,
                total_ending_balance=total_end,
                original_pool_balance=original_pool,
                current_balance=current_balance,
                target=target,
                floor=floor,
            )
            release_amount = max(0.0, evaluated) if evaluated is not None else 0.0
        elif (ra.release_condition or "").lower() == "always":
            release_amount = max(0.0, current_balance - floor)
        elif (ra.release_condition or "").lower() == "trigger_failure":
            release_amount = 0.0
        else:
            # Default: release excess above target+floor
            release_amount = max(0.0, current_balance - target - floor)

        # Cap release by the current balance
        release_amount = min(release_amount, current_balance)

        ending_balance = max(0.0, current_balance - release_amount)

        if (ra.released_to or "available_funds").lower() == "available_funds":
            total_release_to_af += release_amount

        entries.append(AccountEntry(
            account_name=ra.account_name,
            beginning_balance=beginning,
            deposits=deposit,
            withdrawals=release_amount,
            ending_balance_pre_payment=current_balance,
            ending_balance_post_payment=ending_balance,
            required_balance=target if target > 0 else None,
        ))

    return entries, total_release_to_af, remainder


def _execute_waterfall_steps(
    steps: List[WaterfallStep],
    class_interest_due: Dict[str, float],
    class_principal_due: Dict[str, float],
    class_beginning_balances: Dict[str, float],
    available_funds: float,
    payment_type: str = "interest",
    cap_carryover: Optional[Dict[str, float]] = None,
    fee_amounts: Optional[Dict[str, float]] = None,
    reserve_balance: float = 0.0,
    reserve_target: float = 0.0,
    realized_loss: float = 0.0,
    reserve_balances: Optional[Dict[str, float]] = None,
    reserve_targets: Optional[Dict[str, float]] = None,
) -> Tuple[float, Dict[str, float], List[WaterfallTraceStep]]:
    """
    Execute a series of waterfall steps.

    When a step has an ``amount_formula`` (LLM-generated Python expression),
    that formula is evaluated to determine the exact amount owed.
    Otherwise falls back to the rule-based logic keyed on ``payment_type``.

    Returns:
      - remaining_funds after all steps
      - payments made per class {class_name: amount_paid}
      - trace of each step with formulas shown in descriptions
    """
    payments: Dict[str, float] = {}
    trace: List[WaterfallTraceStep] = []
    funds = available_funds
    total_interest_due = sum(class_interest_due.values())

    for step in steps:
        class_name = step.class_name
        ptype = step.payment_type.lower()

        # ── Amount determination ──────────────────────────────────────────
        # Priority: LLM-generated formula  >  rule-based fallback
        amount_owed: float = 0.0
        formula_used = step.amount_formula or ""

        if formula_used:
            evaluated = _eval_formula(
                formula=formula_used,
                available_funds=funds,
                interest_due=class_interest_due,
                balances=class_beginning_balances,
                cap_carryover=cap_carryover,
                fee_amounts=fee_amounts,
                reserve_balance=reserve_balance,
                reserve_target=reserve_target,
                realized_loss=realized_loss,
                total_interest_due=total_interest_due,
                reserve_balances=reserve_balances,
                reserve_targets=reserve_targets,
            )
            if evaluated is not None:
                amount_owed = max(0.0, evaluated)
                # ── Safety check: if formula says 0 but rule-based says > 0,
                # the formula may be wrong (e.g. a reserve formula on a class
                # payment step).  Fall back to rule-based to protect the calc.
                if amount_owed == 0.0 and funds > 0 and class_name:
                    if ptype == "interest":
                        rule_based = class_interest_due.get(class_name, 0.0)
                    elif ptype == "principal":
                        rule_based = class_beginning_balances.get(class_name, 0.0)
                    else:
                        rule_based = 0.0
                    if rule_based > 0:
                        logger.warning(
                            f"Step {step.step} ({class_name} {ptype}): formula '{formula_used}' "
                            f"returned 0 but rule-based gives {rule_based:,.2f}. "
                            f"Using rule-based amount."
                        )
                        amount_owed = rule_based
                        formula_used = ""  # mark as rule-based in trace
            else:
                formula_used = ""  # fall through to rule-based

        if not formula_used:
            # Rule-based fallback
            if ptype == "interest":
                amount_owed = class_interest_due.get(class_name or "", 0.0)
            elif ptype == "principal":
                amount_owed = class_beginning_balances.get(class_name or "", 0.0)
            elif ptype == "reserve":
                acct = getattr(step, "reserve_account", None)
                if acct and reserve_targets is not None and reserve_balances is not None:
                    rt = float(reserve_targets.get(acct, reserve_target) or 0.0)
                    rb = float(reserve_balances.get(acct, reserve_balance) or 0.0)
                    amount_owed = max(0.0, rt - rb)
                else:
                    amount_owed = max(0.0, reserve_target - reserve_balance)
            elif ptype == "loss_reimbursement":
                amount_owed = realized_loss
            elif ptype == "fee":
                amount_owed = (fee_amounts or {}).get(class_name or "", 0.0)
            elif ptype == "excess":
                amount_owed = funds  # pass-through everything remaining
            else:
                amount_owed = class_interest_due.get(class_name or "", 0.0)

        # ── Payment execution ─────────────────────────────────────────────
        if step.concurrent_with:
            # Pro-rata distribution across concurrent classes
            concurrent_classes = [c for c in ([class_name] + (step.concurrent_with or [])) if c]
            total_concurrent_owed = sum(
                class_interest_due.get(c, 0.0) if ptype == "interest"
                else class_beginning_balances.get(c, 0.0)
                for c in concurrent_classes
            )
            if total_concurrent_owed > 0:
                for cls in concurrent_classes:
                    cls_owed = (
                        class_interest_due.get(cls, 0.0) if ptype == "interest"
                        else class_beginning_balances.get(cls, 0.0)
                    )
                    ratio = cls_owed / total_concurrent_owed
                    cls_paid = min(cls_owed, funds * ratio)
                    payments[cls] = payments.get(cls, 0.0) + cls_paid
                amount_paid = min(total_concurrent_owed, funds)
            else:
                amount_paid = 0.0
        else:
            amount_paid = min(amount_owed, funds) if amount_owed > 0 else 0.0
            if class_name:
                payments[class_name] = payments.get(class_name, 0.0) + amount_paid

        funds = max(0.0, funds - amount_paid)

        # Augment description with formula when one was used
        description = step.description or ""
        if formula_used:
            description = f"{description}  [formula: {formula_used} = {amount_paid:,.2f}]"

        trace.append(WaterfallTraceStep(
            step=step.step,
            description=description,
            source_bucket=step.source_bucket,
            funds_available=available_funds if step.step == 1 else funds + amount_paid,
            amount_owed=amount_owed,
            amount_paid=amount_paid,
            funds_remaining=funds,
            class_name=class_name,
            payment_type=ptype,
        ))

    return funds, payments, trace


def _distribute_interest_remittance(
    deal_config: DealConfig,
    class_details: List[ClassPaymentSummary],
    interest_remittance: float,
    days: int,
    sofr_rate: float,
    net_wac: float,
    beginning_balances: Dict[str, float],
) -> Tuple[float, List[WaterfallTraceStep]]:
    """
    Distribute the Interest Remittance Amount according to the deal's interest waterfall.
    Returns (monthly_excess_cashflow, trace_steps).
    Updates class_details in place.
    """
    # Build interest due map
    interest_due: Dict[str, float] = {}
    for cd in class_details:
        interest_due[cd.class_name] = cd.total_interest_due

    funds = interest_remittance
    trace: List[WaterfallTraceStep] = []

    # Validate extracted steps against actual deal classes before using them
    valid_class_names = {cls.class_name for cls in deal_config.classes}
    validated_interest_steps = _validate_waterfall_steps(
        deal_config.interest_waterfall or [], valid_class_names
    )

    # If configured (and validated) waterfall steps exist, use them
    if validated_interest_steps:
        remaining, interest_payments, trace = _execute_waterfall_steps(
            steps=validated_interest_steps,
            class_interest_due=interest_due,
            class_principal_due={},
            class_beginning_balances=beginning_balances,
            available_funds=funds,
            payment_type="interest",
        )
        # Apply payments to class details
        for cd in class_details:
            paid = interest_payments.get(cd.class_name, 0.0)
            # Ensure we don't overpay vs due
            paid = min(paid, cd.total_interest_due)
            cd.interest_paid = paid
            cd.ending_interest_carryforward = max(0.0, cd.total_interest_due - paid)
            cd.total_paid += paid
        return remaining, trace

    # Fallback: sequential interest distribution (sorted by priority)
    sorted_classes = sorted(class_details, key=lambda x: x.__dict__.get("_priority", 99))

    # First sort by interest_priority from deal config
    class_priority = {
        cls.class_name: cls.interest_priority
        for cls in deal_config.classes
    }

    sorted_class_details = sorted(
        class_details,
        key=lambda x: class_priority.get(x.class_name, 999)
    )

    step_num = 1
    for cd in sorted_class_details:
        if cd.class_type.lower() in ("residual",):
            continue
        if cd.total_interest_due <= 0:
            continue

        amount_paid = min(cd.total_interest_due, funds)
        cd.interest_paid = amount_paid
        cd.ending_interest_carryforward = max(0.0, cd.total_interest_due - amount_paid)
        cd.total_paid += amount_paid
        funds = max(0.0, funds - amount_paid)

        trace.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {cd.class_name} Interest",
            source_bucket="interest_remittance",
            funds_available=funds + amount_paid,
            amount_owed=cd.total_interest_due,
            amount_paid=amount_paid,
            funds_remaining=funds,
            class_name=cd.class_name,
            payment_type="interest",
        ))
        step_num += 1

    # Remaining interest goes to excess cashflow
    return funds, trace


def _distribute_principal_remittance(
    deal_config: DealConfig,
    class_details: List[ClassPaymentSummary],
    principal_remittance: float,
    beginning_balances: Dict[str, float],
    unpaid_interest: Dict[str, float],
) -> Tuple[float, List[WaterfallTraceStep]]:
    """
    Distribute the Principal Remittance Amount according to the deal's principal waterfall.
    Returns (remaining_for_excess, trace_steps).
    Updates class_details in place.
    """
    funds = principal_remittance
    trace: List[WaterfallTraceStep] = []

    # Step 1: Cover any unpaid interest from interest remittance (principal priority 1)
    step_num = 1
    for class_name, unpaid in unpaid_interest.items():
        if unpaid > 0 and funds > 0:
            paid = min(unpaid, funds)
            for cd in class_details:
                if cd.class_name == class_name:
                    cd.interest_paid += paid
                    cd.ending_interest_carryforward = max(0.0, cd.total_interest_due - cd.interest_paid)
                    cd.total_paid += paid
                    break
            funds = max(0.0, funds - paid)
            trace.append(WaterfallTraceStep(
                step=step_num,
                description=f"Cover unpaid interest for Class {class_name} from Principal Remittance",
                source_bucket="principal_remittance",
                funds_available=funds + paid,
                amount_owed=unpaid,
                amount_paid=paid,
                funds_remaining=funds,
                class_name=class_name,
                payment_type="interest",
            ))
            step_num += 1

    # Validate extracted steps against actual deal classes
    valid_class_names = {cls.class_name for cls in deal_config.classes}
    validated_principal_steps = _validate_waterfall_steps(
        deal_config.principal_waterfall or [], valid_class_names
    )

    if validated_principal_steps:
        remaining, principal_payments, wf_trace = _execute_waterfall_steps(
            steps=validated_principal_steps,
            class_interest_due={},
            class_principal_due={},
            class_beginning_balances=beginning_balances,
            available_funds=funds,
            payment_type="principal",
        )
        # Apply payments
        for cd in class_details:
            paid = principal_payments.get(cd.class_name, 0.0)
            beginning_bal = beginning_balances.get(cd.class_name, cd.beginning_principal)
            paid = min(paid, beginning_bal)  # Can't pay more than outstanding
            cd.principal_paid = paid
            cd.ending_principal = max(0.0, beginning_bal - paid)
            cd.total_paid += paid
        trace.extend(wf_trace)
        return remaining, trace

    # Fallback: sequential principal distribution
    # Sort by principal_priority (Senior first)
    class_priority = {
        cls.class_name: cls.principal_priority
        for cls in deal_config.classes
    }
    # is_notional lives on CertificateClass, not ClassPaymentSummary — build a lookup
    is_notional_map = {
        cls.class_name: cls.is_notional
        for cls in deal_config.classes
    }
    sorted_classes = sorted(
        class_details,
        key=lambda x: class_priority.get(x.class_name, 999)
    )

    for cd in sorted_classes:
        if cd.class_type.lower() in ("io", "residual", "excess_cashflow"):
            continue
        if is_notional_map.get(cd.class_name, False):
            continue

        beginning_bal = beginning_balances.get(cd.class_name, cd.beginning_principal)
        if beginning_bal <= 0 or funds <= 0:
            continue

        amount_paid = min(beginning_bal, funds)
        cd.principal_paid = amount_paid
        cd.ending_principal = max(0.0, beginning_bal - amount_paid)
        cd.total_paid += amount_paid
        funds = max(0.0, funds - amount_paid)

        trace.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {cd.class_name} Principal",
            source_bucket="principal_remittance",
            funds_available=funds + amount_paid,
            amount_owed=beginning_bal,
            amount_paid=amount_paid,
            funds_remaining=funds,
            class_name=cd.class_name,
            payment_type="principal",
        ))
        step_num += 1

    return funds, trace


def _distribute_excess_cashflow(
    deal_config: DealConfig,
    class_details: List[ClassPaymentSummary],
    excess_cashflow: float,
    beginning_balances: Dict[str, float],
    realized_losses: float,
) -> Tuple[float, List[WaterfallTraceStep]]:
    """
    Distribute Monthly Excess Cashflow.
    Typical structure:
      1. Reimburse realized losses (write-ups on senior bonds)
      2. Pay cap carryover amounts
      3. Excess Servicing Strip (IO class)
      4. XS / X class remainder
      5. Any remainder to Excess Reserve Account
    Updates class_details in place.
    Returns (remainder_to_reserve, trace)
    """
    funds = excess_cashflow
    trace: List[WaterfallTraceStep] = []
    step_num = 1

    # Validate extracted steps against actual deal classes
    valid_class_names = {cls.class_name for cls in deal_config.classes}
    validated_excess_steps = _validate_waterfall_steps(
        deal_config.excess_cashflow_waterfall or [], valid_class_names
    )

    if validated_excess_steps:
        remaining, excess_payments, wf_trace = _execute_waterfall_steps(
            steps=validated_excess_steps,
            class_interest_due={},
            class_principal_due={},
            class_beginning_balances=beginning_balances,
            available_funds=funds,
            payment_type="excess",
        )
        for cd in class_details:
            paid = excess_payments.get(cd.class_name, 0.0)
            if paid > 0:
                cd.total_paid += paid
                if cd.class_type.lower() in ("io", "excess_cashflow") or "io" in cd.class_name.lower() or cd.class_name.upper() in ("X", "XS", "A-IO-S"):
                    cd.interest_paid += paid  # IO/XS gets interest payments from excess
        trace.extend(wf_trace)
        return remaining, trace

    # Fallback: standard excess cashflow distribution
    # Step 1: Reimburse realized losses to bonds (reverse order: senior first)
    if realized_losses > 0:
        senior_classes = [cd for cd in class_details if "senior" in cd.class_type.lower()]
        for cd in senior_classes:
            if funds <= 0:
                break
            paid = min(realized_losses, funds)
            cd.principal_paid = cd.principal_paid + paid  # Writeup
            cd.writeup_amount = paid
            cd.total_paid += paid
            funds = max(0.0, funds - paid)
            trace.append(WaterfallTraceStep(
                step=step_num,
                description=f"Reimburse Realized Losses for {cd.class_name}",
                source_bucket="excess_cashflow",
                funds_available=funds + paid,
                amount_owed=realized_losses,
                amount_paid=paid,
                funds_remaining=funds,
                class_name=cd.class_name,
                payment_type="principal",
            ))
            step_num += 1

    # Step 2: Pay cap carryover amounts for each class
    for cd in class_details:
        if funds <= 0:
            break
        if cd.total_cap_carryover > 0:
            paid = min(cd.total_cap_carryover, funds)
            cd.cap_carryover_paid = paid
            cd.ending_cap_carryover = max(0.0, cd.total_cap_carryover - paid)
            cd.total_paid += paid
            funds = max(0.0, funds - paid)
            trace.append(WaterfallTraceStep(
                step=step_num,
                description=f"Cap Carryover for {cd.class_name}",
                source_bucket="excess_cashflow",
                funds_available=funds + paid,
                amount_owed=cd.total_cap_carryover,
                amount_paid=paid,
                funds_remaining=funds,
                class_name=cd.class_name,
                payment_type="interest",
            ))
            step_num += 1

    # Step 3: IO / Excess Servicing Strip
    io_classes = [cd for cd in class_details
                  if cd.class_name.upper() in ("A-IO-S", "A-IO", "IO", "XS") or
                  cd.class_type.lower() in ("io",)]
    for cd in io_classes:
        if funds <= 0:
            break
        if cd.total_interest_due > 0:
            amount_owed = max(0.0, cd.total_interest_due - cd.interest_paid)
            paid = min(amount_owed, funds)
            cd.interest_paid += paid
            cd.ending_interest_carryforward = max(0.0, cd.total_interest_due - cd.interest_paid)
            cd.total_paid += paid
            funds = max(0.0, funds - paid)
            trace.append(WaterfallTraceStep(
                step=step_num,
                description=f"IO/Excess Servicing Strip - {cd.class_name}",
                source_bucket="excess_cashflow",
                funds_available=funds + paid,
                amount_owed=amount_owed,
                amount_paid=paid,
                funds_remaining=funds,
                class_name=cd.class_name,
                payment_type="interest",
            ))
            step_num += 1

    # Step 4: X class / subordinate residual gets remaining
    x_classes = [cd for cd in class_details
                 if cd.class_name.upper() in ("X", "BX") or
                 cd.class_type.lower() in ("excess_cashflow",)]
    for cd in x_classes:
        if funds <= 0:
            break
        paid = funds  # X gets all remainder
        cd.interest_paid += paid
        cd.total_paid += paid
        funds = 0.0
        trace.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {cd.class_name} Distribution (Monthly Excess Cashflow)",
            source_bucket="excess_cashflow",
            funds_available=paid,
            amount_owed=paid,
            amount_paid=paid,
            funds_remaining=0.0,
            class_name=cd.class_name,
            payment_type="excess",
        ))
        step_num += 1

    return funds, trace


# ---------------------------------------------------------------------------
# Canonical principal-branch builders + trigger evaluation
#
# These are used to render Section 8's full disclosure: both the
# "Trigger Not In Effect" (pro-rata across A-1/M-1/M-2/M-3, then sequential
# B-1..B-4) and "Trigger In Effect" (fully sequential A-1..B-4) branches.
# Execution still flows through _distribute_principal_remittance; the canonical
# branches here are for the report's structural disclosure of both paths.
# ---------------------------------------------------------------------------

_PRO_RATA_CLASSES = ("A-1", "M-1", "M-2", "M-3")
_SEQUENTIAL_TAIL = ("B-1", "B-2", "B-3", "B-4")
_FULLY_SEQUENTIAL_ORDER = ("A-1", "M-1", "M-2", "M-3", "B-1", "B-2", "B-3", "B-4")


def _build_principal_branch_no_trigger(
    classes: List[CertificateClass],
    beginning_balances: Dict[str, float],
    principal_remittance: float,
) -> List[WaterfallTraceStep]:
    """Pro-rata across A-1/M-1/M-2/M-3, then sequential B-1 → B-2 → B-3 → B-4."""
    funds = principal_remittance
    steps: List[WaterfallTraceStep] = []
    names = {c.class_name for c in classes}

    pro_rata = [n for n in _PRO_RATA_CLASSES if n in names]
    pro_rata_bal = {n: beginning_balances.get(n, 0.0) for n in pro_rata}
    total_pro_rata = sum(pro_rata_bal.values())

    funds_before_pro_rata = funds
    pro_rata_total_paid = 0.0
    step_num = 1
    for n in pro_rata:
        bal = pro_rata_bal[n]
        if total_pro_rata > 0 and bal > 0:
            ratio = bal / total_pro_rata
            paid = min(bal, funds_before_pro_rata * ratio)
        else:
            paid = 0.0
        pro_rata_total_paid += paid
        steps.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {n} principal (pro-rata)",
            source_bucket="principal_remittance",
            funds_available=funds_before_pro_rata,
            amount_owed=bal,
            amount_paid=paid,
            funds_remaining=max(0.0, funds_before_pro_rata - pro_rata_total_paid),
            class_name=n,
            payment_type="principal",
        ))
        step_num += 1
    funds = max(0.0, funds - pro_rata_total_paid)

    for n in _SEQUENTIAL_TAIL:
        if n not in names:
            continue
        bal = beginning_balances.get(n, 0.0)
        paid = min(bal, funds)
        steps.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {n} principal (sequential)",
            source_bucket="principal_remittance",
            funds_available=funds,
            amount_owed=bal,
            amount_paid=paid,
            funds_remaining=max(0.0, funds - paid),
            class_name=n,
            payment_type="principal",
        ))
        funds = max(0.0, funds - paid)
        step_num += 1

    return steps


def _build_principal_branch_with_trigger(
    classes: List[CertificateClass],
    beginning_balances: Dict[str, float],
    principal_remittance: float,
) -> List[WaterfallTraceStep]:
    """Fully sequential A-1 → M-1 → M-2 → M-3 → B-1 → B-2 → B-3 → B-4."""
    funds = principal_remittance
    steps: List[WaterfallTraceStep] = []
    names = {c.class_name for c in classes}
    step_num = 1
    for n in _FULLY_SEQUENTIAL_ORDER:
        if n not in names:
            continue
        bal = beginning_balances.get(n, 0.0)
        paid = min(bal, funds)
        steps.append(WaterfallTraceStep(
            step=step_num,
            description=f"Class {n} principal (sequential, trigger in effect)",
            source_bucket="principal_remittance",
            funds_available=funds,
            amount_owed=bal,
            amount_paid=paid,
            funds_remaining=max(0.0, funds - paid),
            class_name=n,
            payment_type="principal",
        ))
        funds = max(0.0, funds - paid)
        step_num += 1
    return steps


def _evaluate_credit_support_trigger(
    classes: List[CertificateClass],
    beginning_balances: Dict[str, float],
) -> Tuple[float, bool]:
    """Trigger fails when aggregate subordinate principal (M-1..B-4) ≤ 0."""
    sub_names = ("M-1", "M-2", "M-3", "B-1", "B-2", "B-3", "B-4")
    sub_bal = sum(beginning_balances.get(n, 0.0) for n in sub_names)
    return sub_bal, sub_bal <= 0


def _evaluate_cumulative_loss_trigger(
    classes: List[CertificateClass],
    cumulative_losses: float,
    threshold: float = 0.05,
) -> Tuple[float, bool, float]:
    """Cumulative loss as % of total cert principal (excluding A-1) > threshold."""
    denom = sum(
        c.initial_principal for c in classes
        if _is_main_capital_class(c) and c.class_name != "A-1"
    )
    if denom <= 0:
        return 0.0, False, threshold
    ratio = cumulative_losses / denom
    return ratio, ratio > threshold, threshold


def _evaluate_delinquency_trigger(
    prior_history: List[Dict],
    pool_stats: Dict,
    beginning_balances: Dict[str, float],
    threshold: float = 0.05,
    window: int = 6,
) -> Tuple[float, bool, float]:
    """6-month rolling avg of 60+ DPD / (pool − A-1) > threshold."""
    current_60plus = (
        pool_stats["bucket_60_89"]["balance"]
        + pool_stats["bucket_90_119"]["balance"]
        + pool_stats["bucket_120_149"]["balance"]
        + pool_stats["bucket_150_179"]["balance"]
        + pool_stats["bucket_180_plus"]["balance"]
    )
    a1 = beginning_balances.get("A-1", 0.0)
    rolling = _rolling_60plus_delinquency_pct(
        prior_history,
        current_60plus,
        pool_stats["total_ending_balance"],
        a1,
        window=window,
    )
    return rolling, rolling > threshold, threshold


def _chip_status(margin_pct: float, fired: bool) -> str:
    """Map a margin (positive = headroom) to a green/amber/red chip status.

    margin_pct = (threshold − current_value) / threshold  for fail-on-exceed tests
    Convention:
      fired         → red
      margin < 25%  → amber (within 25% of breach)
      otherwise     → green
    """
    if fired:
        return "red"
    if margin_pct < 0.25:
        return "amber"
    return "green"


def _evaluate_config_trigger(condition: str, context: Dict[str, Any]) -> bool:
    """Evaluate a Python boolean expression from a deal config trigger condition.

    context must supply the variables referenced in the expression. On any
    failure (NameError from a stale/typo'd variable, SyntaxError from a
    malformed expression, etc.) the function returns False so the waterfall
    keeps running, but the specific failure is logged so the silent-no-fire is
    debuggable from server logs.
    """
    try:
        result = eval(condition, {"__builtins__": {}}, context)  # noqa: S307
        return bool(result)
    except NameError as e:
        logger.warning(
            f"Config trigger condition referenced unknown variable: {e} "
            f"in expression {condition!r}. Trigger treated as not fired."
        )
        return False
    except SyntaxError as e:
        logger.warning(
            f"Config trigger condition is not a valid Python expression "
            f"({e.msg}): {condition!r}. Trigger treated as not fired."
        )
        return False
    except Exception as e:
        logger.warning(
            f"Config trigger condition raised {type(e).__name__} ({e}) "
            f"in expression {condition!r}. Trigger treated as not fired."
        )
        return False


# ---------------------------------------------------------------------------
# Main waterfall computation
# ---------------------------------------------------------------------------

def compute_waterfall(
    deal_config: DealConfig,
    loans: List[Dict],
    payment_date_str: str,
    sofr_rate: Optional[float] = None,
    prior_class_balances: Optional[Dict[str, float]] = None,
    prior_waterfall_result: Optional[Dict] = None,
    prior_history: Optional[List[Dict]] = None,
    prior_reserve_balances: Optional[Dict[str, float]] = None,
    prior_fee_shortfalls: Optional[Dict[str, float]] = None,
) -> WaterfallResult:
    """
    Main entry point for waterfall computation.

    Args:
        deal_config: Full deal configuration from the indenture.
        loans: List of loan records from the loantape for this payment date.
        payment_date_str: Distribution date as "YYYY-MM-DD".
        sofr_rate: 1-month SOFR rate (decimal). If None, uses deal default.
        prior_class_balances: Beginning balances for each class.
            If None, uses initial_principal from deal config.
        prior_waterfall_result: Immediately prior period waterfall result.
            Used to seed beginning carryforwards, beginning fee shortfalls,
            beginning reserve balances, and per-class cumulative life-to-date
            totals.
        prior_history: Full chronological list of all waterfall results
            preceding ``payment_date_str`` (oldest first), as produced by
            ``deal_store.list_prior_waterfall_results``. Used to compute
            rolling CPR/CDR (3-month and inception) and 6-month rolling
            delinquency averages for trigger evaluation.

    Returns:
        WaterfallResult with full computation detail.
    """
    if prior_history is None:
        prior_history = []
    payment_date = datetime.strptime(payment_date_str, "%Y-%m-%d").date()
    computed_at = datetime.utcnow().isoformat()

    # ---- 1. Determine accrual period ----
    accrual_start, accrual_end, days = _compute_accrual_dates(payment_date)
    accrual_start_str = accrual_start.strftime("%Y-%m-%d")
    accrual_end_str = accrual_end.strftime("%Y-%m-%d")

    # Record date is typically 2 business days before payment date
    record_date = payment_date - timedelta(days=deal_config.record_date_offset_days)
    record_date_str = record_date.strftime("%Y-%m-%d")

    # ---- 2. Aggregate loantape ----
    pool_stats = aggregate_loantape(loans)

    # ---- 3. Determine SOFR rate ----
    if sofr_rate is None:
        sofr_rate = deal_config.default_sofr_rate or 0.0530  # Default 5.30%

    # ---- 4. Compute Net WAC ----
    # Get weighted average servicing fee rate from deal config
    svc_fee_rate = 0.0
    if deal_config.servicers:
        total_pct = sum(s.portfolio_pct or 1.0 for s in deal_config.servicers)
        weighted_fee = sum(
            s.servicing_fee_rate * (s.portfolio_pct or 1.0)
            for s in deal_config.servicers
        )
        svc_fee_rate = weighted_fee / total_pct if total_pct > 0 else deal_config.servicers[0].servicing_fee_rate
    else:
        # Infer from fee config
        for fee in deal_config.fees:
            if "servicing" in fee.fee_name.lower() and fee.fee_rate:
                svc_fee_rate = fee.fee_rate
                break

    gross_wac = pool_stats["gross_wac"]
    net_wac = max(0.0, gross_wac - svc_fee_rate)

    # ---- 5. Set beginning balances ----
    beginning_balances: Dict[str, float] = {}
    for cls in deal_config.classes:
        if prior_class_balances and cls.class_name in prior_class_balances:
            beginning_balances[cls.class_name] = prior_class_balances[cls.class_name]
        else:
            beginning_balances[cls.class_name] = cls.initial_principal

    # ---- 6. Compute pass-through rates and interest due for each class ----
    class_details: List[ClassPaymentSummary] = []

    for cls in deal_config.classes:
        rate = _get_class_rate(cls, sofr_rate, net_wac)
        beginning_bal = beginning_balances.get(cls.class_name, cls.initial_principal)

        # Per-class day count: 30/360 → 30 fixed days regardless of calendar,
        # actual/360 → the period's actual day count. The pool-level `days`
        # value (computed from accrual dates above) is used for actual/* and
        # also feeds cash-collection math; only the class-level interest
        # accrual day count switches.
        class_days = _class_day_count(cls.accrual_convention, days)

        # Compute cap carryover for floating rate classes
        # Cap carryover = max(0, rate_if_no_cap - actual_rate) * balance
        cap_carryover_this_period = 0.0
        if cls.interest_rate_type == "floating" and cls.margin and cls.rate_cap:
            uncapped_rate = sofr_rate + (cls.margin or 0.0)
            if uncapped_rate > rate:
                cap_carryover_this_period = _compute_interest(
                    beginning_bal, uncapped_rate - rate, class_days, cls.accrual_convention
                )

        # Interest accrual
        interest_accrued = _compute_interest(beginning_bal, rate, class_days, cls.accrual_convention)

        # Beginning carryforward (from prior period)
        beginning_carryforward = 0.0
        if prior_waterfall_result:
            for prior_cd in prior_waterfall_result.get("class_details", []):
                if prior_cd.get("class_name") == cls.class_name:
                    beginning_carryforward = prior_cd.get("ending_interest_carryforward", 0.0)
                    break

        total_due = beginning_carryforward + interest_accrued

        # Beginning cap carryover
        beginning_cap_carryover = 0.0
        if prior_waterfall_result:
            for prior_cd in prior_waterfall_result.get("class_details", []):
                if prior_cd.get("class_name") == cls.class_name:
                    beginning_cap_carryover = prior_cd.get("ending_cap_carryover", 0.0)
                    break

        total_cap_carryover = beginning_cap_carryover + cap_carryover_this_period

        # Factor computation (per $1000 face value)
        original_principal = cls.initial_principal
        factor_denom = original_principal / 1000.0 if original_principal > 0 else 1.0
        factor_beginning = beginning_bal / original_principal if original_principal > 0 else 0.0

        cd = ClassPaymentSummary(
            class_name=cls.class_name,
            cusip=cls.cusip,
            class_type=cls.type,
            original_principal=original_principal,
            beginning_principal=beginning_bal,
            interest_rate=rate,
            benchmark_rate=sofr_rate if cls.interest_rate_type == "floating" else 0.0,
            accrual_start=accrual_start_str,
            accrual_end=accrual_end_str,
            days_accrued=class_days,
            beginning_interest_carryforward=beginning_carryforward,
            interest_accrued=interest_accrued,
            total_interest_due=total_due,
            interest_paid=0.0,  # Will be filled by waterfall
            ending_interest_carryforward=total_due,  # Will be updated
            beginning_cap_carryover=beginning_cap_carryover,
            current_cap_carryover=cap_carryover_this_period,
            total_cap_carryover=total_cap_carryover,
            cap_carryover_paid=0.0,
            ending_cap_carryover=total_cap_carryover,
            principal_paid=0.0,
            ending_principal=beginning_bal,  # Will be updated
            total_paid=0.0,
            factor_beginning=factor_beginning * 1000.0,
            factor_ending=factor_beginning * 1000.0,
            record_date=record_date_str,
        )
        # Mark notional classes
        if cls.is_notional or cls.interest_rate_type in ("excess_cashflow", "io"):
            cd.class_type = cls.type
        class_details.append(cd)

    # ---- 7. Compute Available Funds ----
    # Interest Remittance Amount
    gross_interest = pool_stats["total_gross_interest"] or pool_stats["total_interest_collected"]
    servicing_fees_collected = pool_stats["total_servicing_fees"]

    # Compute deal fees AND expenses (custodian, SA, trustee, indemnification, etc.)
    pool_balance = pool_stats["total_beginning_balance"] or pool_stats["total_ending_balance"]
    deal_fees_amount, fee_entries, deal_expenses_amount, computed_expense_entries = compute_deal_fees(
        deal_config,
        pool_balance,
        days,
        prior_result=prior_waterfall_result,
        prior_shortfalls=prior_fee_shortfalls,
    )

    # Other amounts (advances, adjustments)
    other_amounts = 0.0  # Could be overridden from misc items

    # ── Split fees/expenses by their paid_from bucket ───────────────────────
    fee_entry_by_name = {e.fee_name: e for e in fee_entries}
    exp_entry_by_name = {e.fee_name: e for e in computed_expense_entries}

    def _bucket_for(fee: FeeConfig) -> str:
        b = (getattr(fee, "paid_from", None) or "interest_remittance").lower()
        # Map "available_funds" deductions to interest_remittance for the
        # interest-cashflow side; principal_remittance / excess_cashflow
        # deductions handled separately.
        if b not in ("interest_remittance", "principal_remittance", "excess_cashflow", "available_funds"):
            return "interest_remittance"
        return b

    deductions_from_interest = 0.0
    deductions_from_principal = 0.0
    deductions_from_excess = 0.0

    for fee in deal_config.fees:
        if fee.fee_name.lower() in ("servicing fee", "servicer fee"):
            continue
        cat = (getattr(fee, "category", "fee") or "fee").lower()
        if cat == "fee":
            ent = fee_entry_by_name.get(fee.fee_name)
        else:
            ent = exp_entry_by_name.get(fee.fee_name)
        if ent is None:
            continue
        amt = float(ent.amount_paid or 0.0)
        bucket = _bucket_for(fee)
        if bucket == "principal_remittance":
            deductions_from_principal += amt
        elif bucket == "excess_cashflow":
            deductions_from_excess += amt
        else:
            # interest_remittance and available_funds default to interest side
            deductions_from_interest += amt

    interest_remittance = max(
        0.0,
        gross_interest - servicing_fees_collected - deductions_from_interest + other_amounts,
    )

    # Principal Remittance Amount
    sched_principal = pool_stats["total_scheduled_principal"]
    curtailments = pool_stats["total_curtailments"]
    prepayments_full = pool_stats["total_pif_principal"]
    liquidations = pool_stats["total_liquidations"]
    repurchases = pool_stats["total_repurchases"]

    # If scheduled + curtailments + PIF don't add up to total principal, reconcile
    total_principal_breakdown = sched_principal + curtailments + prepayments_full + liquidations + repurchases
    total_principal_collected = pool_stats["total_principal_collected"]
    if total_principal_collected > 0 and abs(total_principal_breakdown - total_principal_collected) > 1.0:
        # Allocate remainder to curtailments
        curtailments += total_principal_collected - total_principal_breakdown
        curtailments = max(0.0, curtailments)

    principal_remittance = sched_principal + curtailments + prepayments_full + liquidations + repurchases
    principal_remittance = max(0.0, principal_remittance - deductions_from_principal)

    available_funds = interest_remittance + principal_remittance

    # ---- 8. Execute Interest Waterfall ----
    monthly_excess_from_interest, interest_trace = _distribute_interest_remittance(
        deal_config=deal_config,
        class_details=class_details,
        interest_remittance=interest_remittance,
        days=days,
        sofr_rate=sofr_rate,
        net_wac=net_wac,
        beginning_balances=beginning_balances,
    )

    # ---- 9. Execute Principal Waterfall ----
    unpaid_interest = {
        cd.class_name: cd.ending_interest_carryforward
        for cd in class_details
        if "senior" in cd.class_type.lower()
    }
    monthly_excess_from_principal, principal_trace = _distribute_principal_remittance(
        deal_config=deal_config,
        class_details=class_details,
        principal_remittance=principal_remittance,
        beginning_balances=beginning_balances,
        unpaid_interest=unpaid_interest,
    )

    # ---- 10. Monthly Excess Cashflow ----
    monthly_excess_cashflow = max(
        0.0,
        monthly_excess_from_interest + monthly_excess_from_principal - deductions_from_excess,
    )
    realized_losses = pool_stats["total_charge_offs"]
    remainder_to_reserve, excess_trace = _distribute_excess_cashflow(
        deal_config=deal_config,
        class_details=class_details,
        excess_cashflow=monthly_excess_cashflow,
        beginning_balances=beginning_balances,
        realized_losses=realized_losses,
    )

    # ---- 11. Update remaining ending principal and factors ----
    for cd in class_details:
        if cd.ending_principal == cd.beginning_principal:
            # Not yet updated (notional / IO classes or classes with no principal paid)
            cd.ending_principal = max(0.0, cd.beginning_principal - cd.principal_paid)

        orig = cd.original_principal
        if orig > 0:
            cd.factor_beginning = (cd.beginning_principal / orig) * 1000.0
            cd.factor_ending = (cd.ending_principal / orig) * 1000.0
            cd.factor_interest = (cd.interest_paid / orig) * 1000.0
            cd.factor_principal = (cd.principal_paid / orig) * 1000.0
            cd.factor_total = cd.factor_interest + cd.factor_principal

    # ---- 11b. Per-class life-to-date cumulative totals ----
    # Roll forward Section 1(f) Cumulative Payment Detail. First period (no
    # prior result) leaves all values equal to the current-period numbers,
    # matching the spec's "if no prior state exists, equal current-period values."
    for cd in class_details:
        cd.cumulative_interest_paid = (
            _prior_class_field(prior_waterfall_result, cd.class_name, "cumulative_interest_paid")
            + cd.interest_paid
        )
        cd.cumulative_principal_paid = (
            _prior_class_field(prior_waterfall_result, cd.class_name, "cumulative_principal_paid")
            + cd.principal_paid
        )
        cd.cumulative_total_distribution = (
            _prior_class_field(prior_waterfall_result, cd.class_name, "cumulative_total_distribution")
            + cd.total_paid
        )
        cd.cumulative_realized_loss = (
            _prior_class_field(prior_waterfall_result, cd.class_name, "cumulative_realized_loss")
            + cd.realized_loss
        )
        cd.cumulative_writedown = (
            _prior_class_field(prior_waterfall_result, cd.class_name, "cumulative_writedown")
            + cd.writedown_amount
        )
        # Deferred interest reported as the running outstanding balance —
        # equals this period's ending carryforward (which already aggregates
        # prior unpaid + this period's unpaid).
        cd.cumulative_deferred_interest = cd.ending_interest_carryforward

    # ---- 12. Compute CDR / CPR ----
    new_defaults = pool_stats["bucket_120_149"]["balance"]  # Standard MBA convention
    unscheduled_principal = curtailments + prepayments_full
    smm_default, cdr_1m, smm_prepay, cpr_1m = compute_cdr_cpr(
        beginning_balance=pool_stats["total_beginning_balance"],
        new_defaults=new_defaults,
        scheduled_principal=sched_principal,
        unscheduled_principal=unscheduled_principal,
    )

    # ---- 12b. Rolling CPR / CDR from prior-period history ----
    # 3-month: trailing 3-period average. Inception: average across all
    # available periods. Both fall back to the current 1-month value when
    # there is no prior history (single-period deals).
    cpr_series, cdr_series = _build_rate_series_from_history(prior_history, cpr_1m, cdr_1m)
    cpr_3m = _rolling_average(cpr_series, 3)
    cdr_3m = _rolling_average(cdr_series, 3)
    cpr_inception = _rolling_average(cpr_series, len(cpr_series))
    cdr_inception = _rolling_average(cdr_series, len(cdr_series))

    # ---- 13. Collateral performance buckets (1-D) ----
    total_balance = pool_stats["total_ending_balance"]
    total_count = pool_stats["loan_count"]
    performance_buckets = []
    bucket_defs = [
        ("Current", "bucket_current"),
        ("1-29 Days", "bucket_1_29"),
        ("30-59 Days", "bucket_30_59"),
        ("60-89 Days", "bucket_60_89"),
        ("90-119 Days", "bucket_90_119"),
        ("120-149 Days", "bucket_120_149"),
        ("150-179 Days", "bucket_150_179"),
        ("180+ Days", "bucket_180_plus"),
        ("Foreclosure", "bucket_foreclosure"),
        ("Bankruptcy", "bucket_bankruptcy"),
        ("REO", "bucket_reo"),
        ("Forbearance", "bucket_forbearance"),
    ]
    for bucket_name, key in bucket_defs:
        bal = pool_stats[key]["balance"]
        cnt = pool_stats[key]["count"]
        performance_buckets.append(CollateralBucket(
            bucket=bucket_name,
            amount=bal,
            count=cnt,
            pct_amount=(bal / total_balance * 100.0) if total_balance > 0 else 0.0,
            pct_count=(cnt / total_count * 100.0) if total_count > 0 else 0.0,
        ))

    # ---- 13b. 2-D delinquency matrix (Section 2(b)) ----
    matrix_rows = ["Current", "1-29", "30-59", "60-89", "90-119", "120-149", "150-179", "180+"]
    matrix_cols = ["Delinquent", "Foreclosure", "Bankruptcy", "REO", "Forbearance"]
    matrix_cells: List[DelinquencyMatrixCell] = []
    row_totals: Dict[str, float] = {r: 0.0 for r in matrix_rows}
    col_totals: Dict[str, float] = {c: 0.0 for c in matrix_cols}
    for r in matrix_rows:
        for c in matrix_cols:
            cell_data = pool_stats["delinq_matrix"].get((r, c), {"balance": 0.0, "count": 0})
            matrix_cells.append(DelinquencyMatrixCell(
                dpd_bucket=r,
                disposition=c,
                amount=cell_data["balance"],
                count=cell_data["count"],
            ))
            row_totals[r] += cell_data["balance"]
            col_totals[c] += cell_data["balance"]
    delinquency_matrix = DelinquencyMatrix(
        rows=matrix_rows,
        columns=matrix_cols + ["Total"],
        cells=matrix_cells,
        row_totals=row_totals,
        col_totals=col_totals,
    )

    # ---- 14. Servicer balances ----
    servicer_balances = []
    for svc_name, svc_data in pool_stats["servicer_balances"].items():
        servicer_balances.append(ServicerBalance(
            servicer_name=svc_name,
            beginning_upb=svc_data["beginning_upb"],
            ending_upb=svc_data["ending_upb"],
            servicing_fee=svc_data["servicing_fee"],
            loan_count=svc_data["loan_count"],
        ))

    # ---- 14b. Collateral Realized Loss (Section 2(d)) ----
    realized_loss_current = pool_stats["total_charge_offs"]
    liquidated_current = int(pool_stats["total_liquidations_count"])
    net_liq_current = pool_stats["total_net_liquidation_proceeds"]
    collateral_realized_loss = CollateralRealizedLossEntry(
        realized_loss_current=realized_loss_current,
        realized_loss_cumulative=(
            _prior_top_field(prior_waterfall_result, "cumulative_realized_losses")
            + realized_loss_current
        ),
        loans_liquidated_current=liquidated_current,
        loans_liquidated_cumulative=(
            int(_prior_top_field(prior_waterfall_result, "cumulative_loans_liquidated"))
            + liquidated_current
        ),
        net_liquidation_proceeds_current=net_liq_current,
        net_liquidation_proceeds_cumulative=(
            _prior_top_field(prior_waterfall_result, "cumulative_net_liquidation_proceeds")
            + net_liq_current
        ),
    )

    # ---- 14c. Structural Features (Section 2(e)) ----
    # Original credit support uses each class's INITIAL principal as denominator
    # and the initial subordinate principal as numerator (a structural feature
    # set at deal issuance). Current credit support uses the same formula on
    # this period's BEGINNING balances (per market convention — supports the
    # period being reported).
    initial_balances = {c.class_name: c.initial_principal for c in deal_config.classes}
    cs_target_classes = ["M-1", "M-2", "M-3"]
    original_ce = {
        cn: _credit_support_pct(cn, deal_config.classes, initial_balances)
        for cn in cs_target_classes
    }
    current_ce = {
        cn: _credit_support_pct(cn, deal_config.classes, beginning_balances)
        for cn in cs_target_classes
    }

    severely_delinquent = (
        pool_stats["bucket_90_119"]["balance"]
        + pool_stats["bucket_120_149"]["balance"]
        + pool_stats["bucket_150_179"]["balance"]
        + pool_stats["bucket_180_plus"]["balance"]
    )
    npl_60plus = severely_delinquent + pool_stats["bucket_60_89"]["balance"]
    npl_pct = (npl_60plus / pool_stats["total_ending_balance"]) if pool_stats["total_ending_balance"] > 0 else 0.0

    # Charge-off % uses original pool balance (lifetime denominator) — matches
    # the standard ABS market convention for cumulative loss ratios.
    orig_pool_for_charge_off = (
        deal_config.original_pool_balance or pool_stats["total_beginning_balance"]
    )
    cum_realized = collateral_realized_loss.realized_loss_cumulative
    charged_off_pct = (
        cum_realized / orig_pool_for_charge_off if orig_pool_for_charge_off > 0 else 0.0
    )

    # Expected interest: pool balance × WAC × days/360 (Act/360 convention at
    # pool level matches the deal's interest_day_count = 'actual/360').
    pool_for_expected = pool_stats["total_beginning_balance"] or pool_stats["total_ending_balance"]
    gross_expected_int = pool_for_expected * gross_wac * days / 360.0 if pool_for_expected > 0 and gross_wac > 0 else 0.0
    net_expected_int = pool_for_expected * net_wac * days / 360.0 if pool_for_expected > 0 and net_wac > 0 else 0.0

    structural_features = StructuralFeatures(
        gross_wac=gross_wac,
        net_wac=net_wac,
        wac_cap=net_wac,  # WAC Cap = period Net WAC (indenture-defined ceiling)
        original_credit_support=original_ce,
        current_credit_support=current_ce,
        non_performing_loan_pct=npl_pct,
        charged_off_loan_pct=charged_off_pct,
        beginning_upb_by_servicer={
            k: v["beginning_upb"] for k, v in pool_stats["servicer_balances"].items()
        },
        ending_upb_by_servicer={
            k: v["ending_upb"] for k, v in pool_stats["servicer_balances"].items()
        },
        sofr_fixing=sofr_rate,
        severely_delinquent_balance=severely_delinquent,
        gross_expected_interest=gross_expected_int,
        net_expected_interest=net_expected_int,
    )

    # ---- 15. Evaluate trigger tests / events ----
    events = []
    trigger_chips: List[TriggerChip] = []
    original_pool_balance = deal_config.original_pool_balance or pool_stats["total_beginning_balance"]
    cleanup_call_threshold = original_pool_balance * deal_config.cleanup_call_pct

    events.append(EventTest(
        test_name="Optional Clean-Up Call",
        current_value=pool_stats["total_beginning_balance"],
        operator="greater_than",
        threshold=cleanup_call_threshold,
        status="Pass" if pool_stats["total_beginning_balance"] > cleanup_call_threshold else "Eligible",
        description=f"Pool balance > {deal_config.cleanup_call_pct*100:.0f}% of original balance",
    ))

    # Real evaluation of the three indenture triggers. ``fired`` = True means
    # the trigger condition is satisfied (the trigger event is in effect) and
    # the principal waterfall flips to fully sequential.
    cum_realized = collateral_realized_loss.realized_loss_cumulative

    cs_value, cs_fired = _evaluate_credit_support_trigger(deal_config.classes, beginning_balances)
    cl_value, cl_fired, cl_threshold = _evaluate_cumulative_loss_trigger(
        deal_config.classes, cum_realized
    )
    dq_value, dq_fired, dq_threshold = _evaluate_delinquency_trigger(
        prior_history, pool_stats, beginning_balances
    )

    # Headroom: how far from breach (positive = headroom). For
    # Credit Support Depletion, breach is at 0 and current_value is a
    # principal amount, so margin is measured against the original sub balance.
    sub_orig = sum(
        c.initial_principal for c in deal_config.classes
        if c.class_name in ("M-1", "M-2", "M-3", "B-1", "B-2", "B-3", "B-4")
    )
    cs_margin = (cs_value / sub_orig) if sub_orig > 0 else 0.0
    cl_margin = max(0.0, (cl_threshold - cl_value) / cl_threshold) if cl_threshold > 0 else 0.0
    dq_margin = max(0.0, (dq_threshold - dq_value) / dq_threshold) if dq_threshold > 0 else 0.0

    # Shared eval context for config-based trigger conditions.
    # Allows extracted Python expressions to reference these named variables.
    trigger_eval_context: Dict[str, Any] = {
        "subordinate_balance": cs_value,
        "cumulative_loss_pct": cl_value,
        "cumulative_losses": cum_realized,
        "delinquency_60plus_pct": dq_value,
        "pool_balance": pool_stats["total_beginning_balance"],
    }

    # Evaluate config triggers that have an extracted trigger_condition.
    # The trigger_action name maps the result back to a known baseline flag or
    # introduces a new named flag.
    config_triggers_with_condition = [
        t for t in (deal_config.triggers or []) if t.trigger_condition
    ]

    config_trigger_results: List[tuple] = []  # (trigger, fired)
    for ct in config_triggers_with_condition:
        fired = _evaluate_config_trigger(ct.trigger_condition, trigger_eval_context)
        config_trigger_results.append((ct, fired))

    # Build a dict from action-name → fired so config results can override the
    # three hardcoded baseline evaluations when the action name matches.
    _KNOWN_ACTIONS = {"CREDIT_SUPPORT_DEPLETION", "CUMULATIVE_LOSS_TRIGGER", "DELINQUENCY_TRIGGER"}
    config_fired_by_action: Dict[str, bool] = {
        ct.trigger_action: fired
        for ct, fired in config_trigger_results
        if ct.trigger_action
    }

    # Override baseline fired status with config-condition results when available.
    cs_fired_final = config_fired_by_action.get("CREDIT_SUPPORT_DEPLETION", cs_fired)
    cl_fired_final = config_fired_by_action.get("CUMULATIVE_LOSS_TRIGGER", cl_fired)
    dq_fired_final = config_fired_by_action.get("DELINQUENCY_TRIGGER", dq_fired)

    # Any config trigger whose action is not one of the three baseline names.
    other_config_fired = any(
        fired
        for ct, fired in config_trigger_results
        if ct.trigger_action not in _KNOWN_ACTIONS
    )

    # EventTest entries — use final (possibly overridden) fired status.
    events.extend([
        EventTest(
            test_name="Credit Support Depletion Event",
            current_value=cs_value,
            operator="equals",
            threshold=0.0,
            status="Fail" if cs_fired_final else "Pass",
            description="Aggregate subordinate principal (M-1..B-4) > 0",
        ),
        EventTest(
            test_name="Cumulative Loss Trigger",
            current_value=cl_value,
            operator="greater_than",
            threshold=cl_threshold,
            status="Fail" if cl_fired_final else "Pass",
            description="Cumulative realized losses / cert principal (excl A-1) ≤ 5%",
        ),
        EventTest(
            test_name="Delinquency Trigger",
            current_value=dq_value,
            operator="greater_than",
            threshold=dq_threshold,
            status="Fail" if dq_fired_final else "Pass",
            description="6-mo rolling avg of 60+ DPD / (pool − A-1) ≤ 5%",
        ),
    ])

    # EventTest rows for non-baseline config triggers.
    _baseline_event_names = {
        "Credit Support Depletion Event",
        "Cumulative Loss Trigger",
        "Delinquency Trigger",
    }
    for ct, ct_fired in config_trigger_results:
        if ct.test_name not in _baseline_event_names:
            events.append(EventTest(
                test_name=ct.test_name,
                current_value=0.0,
                operator=ct.operator,
                threshold=ct.threshold or 0.0,
                status="Fail" if ct_fired else "Pass",
                description=ct.description or f"if {ct.trigger_condition}: {ct.trigger_action} = True",
            ))

    # Dashboard chips — baseline 3 use final fired status.
    trigger_chips = [
        TriggerChip(
            name="Credit Support Depletion",
            status=_chip_status(cs_margin, cs_fired_final),
            current_value=cs_value,
            threshold=0.0,
            margin_pct=cs_margin,
            description="Aggregate subordinate principal headroom",
        ),
        TriggerChip(
            name="Cumulative Loss",
            status=_chip_status(cl_margin, cl_fired_final),
            current_value=cl_value,
            threshold=cl_threshold,
            margin_pct=cl_margin,
            description="Cumulative losses vs 5% cap",
        ),
        TriggerChip(
            name="Delinquency",
            status=_chip_status(dq_margin, dq_fired_final),
            current_value=dq_value,
            threshold=dq_threshold,
            margin_pct=dq_margin,
            description="6-month rolling 60+ DPD vs 5% cap",
        ),
    ]

    # Extra chips for non-baseline config triggers.
    _baseline_chip_names = {"Credit Support Depletion", "Cumulative Loss", "Delinquency"}
    for ct, ct_fired in config_trigger_results:
        display_name = ct.test_name.replace(" Event", "").replace(" Trigger", "")
        if display_name not in _baseline_chip_names:
            trigger_chips.append(TriggerChip(
                name=display_name,
                status="red" if ct_fired else "green",
                current_value=0.0,
                threshold=ct.threshold or 0.0,
                margin_pct=0.0,
                description=ct.description or f"if {ct.trigger_condition}: {ct.trigger_action} = True",
            ))

    # Active principal-waterfall branch: any trigger firing flips to sequential.
    any_trigger_fired = cs_fired_final or cl_fired_final or dq_fired_final or other_config_fired
    active_trigger_branch = "Trigger In Effect" if any_trigger_fired else "Trigger Not In Effect"

    # Canonical branch traces (both rendered for disclosure)
    principal_trace_no_trigger = _build_principal_branch_no_trigger(
        deal_config.classes, beginning_balances, principal_remittance
    )
    principal_trace_with_trigger = _build_principal_branch_with_trigger(
        deal_config.classes, beginning_balances, principal_remittance
    )

    # ---- 16. Reserve accounts ----
    # Beginning balances: prefer explicit ``prior_reserve_balances`` arg, else
    # pull from prior waterfall result, else fall back to initial_balance.
    beginning_reserve_balances: Dict[str, float] = {}
    for ra in deal_config.reserve_accounts:
        if prior_reserve_balances is not None and ra.account_name in prior_reserve_balances:
            beginning_reserve_balances[ra.account_name] = float(
                prior_reserve_balances.get(ra.account_name) or 0.0
            )
        elif prior_waterfall_result is not None:
            beginning_reserve_balances[ra.account_name] = _prior_account_balance(
                prior_waterfall_result, ra.account_name
            )
        else:
            beginning_reserve_balances[ra.account_name] = float(ra.initial_balance or 0.0)

    reserve_accounts, _released_to_af, remainder_after_reserves = compute_reserve_accounts(
        deal_config=deal_config,
        pool_stats=pool_stats,
        beginning_reserve_balances=beginning_reserve_balances,
        remainder_to_reserve=remainder_to_reserve,
    )

    # Excess Reserve Account holds whatever remainder is left after the
    # configured reserve accounts have been topped up.
    excess_reserve_seen = any(
        e.account_name == "Excess Reserve Account" for e in reserve_accounts
    )
    if not excess_reserve_seen:
        if prior_reserve_balances is not None and "Excess Reserve Account" in prior_reserve_balances:
            prior_excess = float(prior_reserve_balances.get("Excess Reserve Account") or 0.0)
        else:
            prior_excess = _prior_account_balance(prior_waterfall_result, "Excess Reserve Account")
        reserve_accounts.append(AccountEntry(
            account_name="Excess Reserve Account",
            beginning_balance=prior_excess,
            deposits=remainder_after_reserves,
            withdrawals=0.0,
            ending_balance_pre_payment=prior_excess,
            ending_balance_post_payment=prior_excess + remainder_after_reserves,
        ))

    # ---- 16b. Expenses (Section 5 — separate from Fees) ----
    # Two sources fold together:
    #   1. config-driven expenses (category="expense" in deal_config.fees) —
    #      already computed by compute_deal_fees() above.
    #   2. canonical "Capped Trust Expenses" rows from the indenture, kept as
    #      placeholders when no itemized current_due exists (zeros).
    expenses_detail: List[ExpenseEntry] = []
    config_expense_names: set = set()
    for e in computed_expense_entries:
        config_expense_names.add(e.fee_name)
        expenses_detail.append(ExpenseEntry(
            expense_name=e.fee_name,
            beginning_shortfall=e.beginning_shortfall,
            current_due=e.current_due,
            total_due=e.total_due,
            amount_paid=e.amount_paid,
            ending_shortfall=e.ending_shortfall,
            remaining_cap=None,
        ))

    _expense_row_names = [
        "Indemnification Amount",
        "Unpaid Trustee Expenses",
        "Unpaid Custodian Expenses",
        "Unpaid Securities Administrator Expenses",
        "Other Expenses",
    ]
    for name in _expense_row_names:
        if name in config_expense_names:
            continue
        beg = _prior_fee_shortfall(prior_waterfall_result, name)
        current = 0.0
        total = beg + current
        paid = total
        ending = max(0.0, total - paid)
        expenses_detail.append(ExpenseEntry(
            expense_name=name,
            beginning_shortfall=beg,
            current_due=current,
            total_due=total,
            amount_paid=paid,
            ending_shortfall=ending,
            remaining_cap=None,
        ))

    # ---- 17. Build loan detail lists ----
    def _to_loan_details(raw_list: List[Dict]) -> List[LoanDetail]:
        return [
            LoanDetail(
                loan_id=r.get("loan_id", ""),
                beginning_principal=r.get("beginning_principal", 0.0),
                ending_principal=r.get("ending_principal", 0.0),
                interest_paid=r.get("interest_paid"),
                principal_paid=r.get("principal_paid"),
                status=r.get("status"),
                days_delinquent=r.get("days_delinquent"),
                interest_rate=r.get("interest_rate"),
                deferred_amount=r.get("deferred_amount"),
                realized_loss=r.get("realized_loss"),
            )
            for r in raw_list
        ]

    # ---- 18. Build totals ----
    total_interest_paid = sum(cd.interest_paid for cd in class_details)
    total_principal_paid = sum(cd.principal_paid for cd in class_details)
    total_ending_principal = sum(
        cd.ending_principal for cd in class_details if not cd.class_type.lower().startswith("io")
    )

    # ---- 18b. Dashboard KPIs + distribution allocation buckets ----
    def _classify_class_for_chart(name: str) -> str:
        """Map a class name to one of the allocation buckets used by Section 8 chart."""
        if name == "A-1":
            return "Senior"
        if name in ("M-1", "M-2", "M-3"):
            return "Mezz"
        if name in ("B-1", "B-2", "B-3", "B-4", "BX"):
            return "Sub"
        return "Other"

    senior_int = sum(cd.interest_paid for cd in class_details if _classify_class_for_chart(cd.class_name) == "Senior")
    senior_prin = sum(cd.principal_paid for cd in class_details if _classify_class_for_chart(cd.class_name) == "Senior")
    mezz_int = sum(cd.interest_paid for cd in class_details if _classify_class_for_chart(cd.class_name) == "Mezz")
    sub_int = sum(cd.interest_paid for cd in class_details if _classify_class_for_chart(cd.class_name) == "Sub")
    class_x_excess = sum(
        cd.total_paid for cd in class_details
        if cd.class_name.upper() in ("X", "BX", "A-IO-S")
        or (cd.class_type or "").lower() in ("excess_cashflow", "io")
    )
    fees_and_expenses = (
        sum(f.amount_paid for f in fee_entries) + servicing_fees_collected
    )

    distribution_allocation_buckets = [
        DistributionAllocation(bucket="Senior Principal", amount=senior_prin),
        DistributionAllocation(bucket="Senior Interest", amount=senior_int),
        DistributionAllocation(bucket="Mezz Interest", amount=mezz_int),
        DistributionAllocation(bucket="Sub Interest", amount=sub_int),
        DistributionAllocation(bucket="Class X / Excess", amount=class_x_excess),
        DistributionAllocation(bucket="Fees & Expenses", amount=fees_and_expenses),
    ]
    distribution_allocation_buckets.sort(key=lambda b: b.amount, reverse=True)

    # KPI values for the dashboard strip (6 tiles per spec D15)
    def _fmt_money_compact(v: float) -> str:
        if v >= 1_000_000:
            return f"${v / 1_000_000:.2f}M"
        return f"${v:,.0f}"

    sixty_plus_pct = (
        (pool_stats["bucket_60_89"]["balance"]
         + pool_stats["bucket_90_119"]["balance"]
         + pool_stats["bucket_120_149"]["balance"]
         + pool_stats["bucket_150_179"]["balance"]
         + pool_stats["bucket_180_plus"]["balance"])
        / pool_stats["total_ending_balance"]
        if pool_stats["total_ending_balance"] > 0 else 0.0
    )

    dashboard_kpis = [
        DashboardKPI(label="Pool Balance",
                     value=_fmt_money_compact(pool_stats["total_ending_balance"]),
                     raw=pool_stats["total_ending_balance"]),
        DashboardKPI(label="Total Distribution",
                     value=_fmt_money_compact(total_interest_paid + total_principal_paid),
                     raw=total_interest_paid + total_principal_paid),
        DashboardKPI(label="Net WAC",
                     value=f"{net_wac*100:.4f}%",
                     raw=net_wac),
        DashboardKPI(label="CPR (1M)",
                     value=f"{cpr_1m*100:.2f}%",
                     raw=cpr_1m),
        DashboardKPI(label="60+ Day Delq %",
                     value=f"{sixty_plus_pct*100:.2f}%",
                     raw=sixty_plus_pct),
        DashboardKPI(label="Active Loan Count",
                     value=f"{pool_stats['loan_count']:,}",
                     raw=float(pool_stats["loan_count"])),
    ]

    # ---- 19. Build fees entries including servicing ----
    servicing_fee_entry = FeeEntry(
        fee_name="Servicing Fee",
        beginning_shortfall=0.0,
        current_due=servicing_fees_collected,
        total_due=servicing_fees_collected,
        amount_paid=servicing_fees_collected,
        ending_shortfall=0.0,
    )
    all_fee_entries = [servicing_fee_entry] + fee_entries

    # ---- 20. Assemble result ----
    return WaterfallResult(
        deal_id=deal_config.deal_id,
        deal_name=deal_config.deal_name,
        payment_date=payment_date_str,
        distribution_date=payment_date_str,
        record_date=record_date_str,
        accrual_start_date=accrual_start_str,
        accrual_end_date=accrual_end_str,
        days_accrued=days,
        report_type="Monthly Report",
        asset_class=deal_config.asset_class,
        benchmark=deal_config.benchmark,
        benchmark_rate=sofr_rate,
        net_wac=net_wac,
        gross_wac=gross_wac,
        # Class payments
        class_details=class_details,
        total_original_principal=sum(cls.initial_principal for cls in deal_config.classes if not cls.is_notional),
        total_beginning_principal=sum(beginning_balances.values()),
        total_interest_paid=total_interest_paid,
        total_principal_paid=total_principal_paid,
        total_paid=total_interest_paid + total_principal_paid,
        total_ending_principal=total_ending_principal,
        # Collateral
        original_pool_balance=original_pool_balance,
        prior_pool_balance=pool_stats["total_beginning_balance"],
        purchases=0.0,
        funded_draws=pool_stats["total_funded_draws"],
        capitalized_amounts=pool_stats["total_capitalized"],
        scheduled_principal_collateral=sched_principal,
        curtailments=curtailments,
        prepayments_in_full=prepayments_full,
        repurchases=repurchases,
        charge_offs=pool_stats["total_charge_offs"],
        sales=0.0,
        liquidations=liquidations,
        realized_losses_collateral=realized_losses,
        other_collateral=0.0,
        current_pool_balance=pool_stats["total_ending_balance"],
        current_loan_count=pool_stats["loan_count"],
        performance_buckets=performance_buckets,
        collateral_rates=CollateralRates(
            cdr_1m=cdr_1m,
            cdr_3m=cdr_3m,
            cdr_inception=cdr_inception,
            cpr_1m=cpr_1m,
            cpr_3m=cpr_3m,
            cpr_inception=cpr_inception,
            smm_prepay=smm_prepay,
            smm_default=smm_default,
        ),
        # Collections
        gross_interest_collected=gross_interest,
        servicing_fees_paid=servicing_fees_collected,
        deal_fees_paid=deal_fees_amount,
        deal_expenses_paid=deal_expenses_amount,
        other_collections=other_amounts,
        interest_remittance_amount=interest_remittance,
        principal_scheduled=sched_principal,
        principal_curtailments=curtailments,
        principal_prepayments_full=prepayments_full,
        principal_sales=0.0,
        principal_liquidations=liquidations,
        principal_repurchases=repurchases,
        principal_recoveries=pool_stats["total_recoveries"],
        principal_other=0.0,
        principal_remittance_amount=principal_remittance,
        available_funds=available_funds,
        monthly_excess_cashflow=monthly_excess_cashflow,
        reserve_accounts=reserve_accounts,
        # Fees
        fees_detail=all_fee_entries,
        total_fees=sum(f.amount_paid for f in all_fee_entries),
        # Expenses (Section 5 — distinct from recurring Fees in Section 4)
        expenses_detail=expenses_detail,
        total_expenses=sum(e.amount_paid for e in expenses_detail),
        # Events
        events=events,
        # Servicer balances
        servicer_balances=servicer_balances,
        misc_items=[
            {"name": "Net WAC", "value": f"{net_wac*100:.5f}%", "category": "WAC"},
            {"name": "Gross WAC", "value": f"{gross_wac*100:.5f}%", "category": "WAC"},
        ],
        # Waterfall trace
        waterfall_trace_interest=interest_trace,
        waterfall_trace_principal=principal_trace,
        waterfall_trace_excess=excess_trace,
        # Canonical branches for Section 8 disclosure
        waterfall_trace_principal_no_trigger=principal_trace_no_trigger,
        waterfall_trace_principal_with_trigger=principal_trace_with_trigger,
        active_trigger_branch=active_trigger_branch,
        # Loan details
        loans_paid_in_full=_to_loan_details(pool_stats["loans_pif"]),
        loans_reo=_to_loan_details(pool_stats["loans_reo"]),
        loans_foreclosure=_to_loan_details(pool_stats["loans_foreclosure"]),
        loans_bankruptcy=_to_loan_details(pool_stats["loans_bankruptcy"]),
        loans_forbearance=_to_loan_details(pool_stats["loans_forbearance"]),
        loans_realized_loss=_to_loan_details(pool_stats["loans_realized_loss"]),
        loans_modified=_to_loan_details(pool_stats["loans_modified"]),
        # Deal-level cumulative life-to-date trackers (rolled forward from prior period)
        cumulative_interest_paid=(
            _prior_top_field(prior_waterfall_result, "cumulative_interest_paid") + total_interest_paid
        ),
        cumulative_principal_paid=(
            _prior_top_field(prior_waterfall_result, "cumulative_principal_paid") + total_principal_paid
        ),
        cumulative_realized_losses=(
            _prior_top_field(prior_waterfall_result, "cumulative_realized_losses") + realized_losses
        ),
        cumulative_prepayments=(
            _prior_top_field(prior_waterfall_result, "cumulative_prepayments")
            + prepayments_full
            + curtailments
        ),
        cumulative_defaults=(
            _prior_top_field(prior_waterfall_result, "cumulative_defaults") + new_defaults
        ),
        cumulative_loans_liquidated=collateral_realized_loss.loans_liquidated_cumulative,
        cumulative_net_liquidation_proceeds=collateral_realized_loss.net_liquidation_proceeds_cumulative,
        # New report sections
        structural_features=structural_features,
        collateral_realized_loss=collateral_realized_loss,
        delinquency_matrix=delinquency_matrix,
        # Dashboard data
        dashboard_kpis=dashboard_kpis,
        trigger_chips=trigger_chips,
        distribution_allocation=distribution_allocation_buckets,
        # Metadata
        computed_at=computed_at,
    )
