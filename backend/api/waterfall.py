"""
API Endpoints for Waterfall Computation.

Endpoints:
  POST /api/waterfall/compute         - Run waterfall computation for deal + payment date
  GET  /api/waterfall/{deal_id}       - List all computed waterfalls for a deal
  GET  /api/waterfall/{deal_id}/{date} - Get a specific waterfall result
  DELETE /api/waterfall/{deal_id}/{date} - Delete a waterfall result
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from config.database import executeQuery
from models.deal import DealConfig
from models.waterfall import WaterfallComputeRequest, WaterfallResult, WaterfallSummary
from services.deal_store import (
    get_prior_waterfall,
    list_prior_waterfall_results,
    list_waterfall_results,
    load_deal_config,
    load_latest_prior_fee_shortfalls,
    load_latest_prior_reserve_balances,
    load_waterfall_result,
    save_fee_shortfalls,
    save_reserve_balances,
    save_waterfall_result,
)
from services.waterfall_engine import compute_waterfall

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/waterfall", tags=["Waterfall Computation"])

# Key columns to fetch from loantape
LOANTAPE_COLS = [
    "LOAN_ID", "BEGINNING_LOAN_BALANCE", "CURRENT_PRINCIPAL_BALANCE",
    "PRIOR_PRINCIPAL_BALANCES", "ORIGINAL_PRINCIPAL_BALANCE",
    "INTEREST_PAYMENT", "GROSS_INTEREST",
    "PRINCIPAL_PAYMENT", "PRINCIPAL_PAYMENT_SCHEDULED",
    "PRINCIPAL_PAYMENT_CURTAILMENTS", "PRINCIPAL_PAYMENT_PIF",
    "PRINCIPAL_LIQUIDATED", "PRINCIPAL_PAYMENT_REPURCHASE",
    "SERVICING_FEES", "SERVICING_FEE_RATE",
    "CURRENT_INTEREST_RATE",
    "LOAN_STATUS", "ACCOUNT_STATUS", "NUMBER_OF_DAYS_IN_ARREARS",
    "SERVICER_NAME", "SERVICER",
    "FUNDED", "FUNDED_REMIT_BAL", "CAPITALIZED_AMOUNTS",
    "CHARGE_OFFS", "ALLOCATED_LOSSES", "CUMULATIVE_RECOVERIES", "RECOVERIES",
    "BANKRUPTCY_FLAG", "DEFAULTED_FLAG", "MODIFICATION_FLAG", "LOAN_MODIFICATION_TYPE",
    "DEFERRED_BEGINNING_BALANCE", "DEFERRED_ENDING_BALANCE",
]


async def _fetch_loantape(deal_id: str, payment_date: str) -> List[Dict[str, Any]]:
    """Fetch loantape from Snowflake for a deal and payment date."""
    col_list = ", ".join(LOANTAPE_COLS)
    query = f"""
        SELECT {col_list}
        FROM IA_DEMO.PUBLIC.LOAN_TAPE
        WHERE DEAL_ID = '{deal_id}'
          AND PAYMENT_DATE = '{payment_date}'
    """
    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Failed to fetch loantape from Snowflake: {err}")

    if not result or len(result) < 2:
        raise HTTPException(
            status_code=404,
            detail=f"No loantape data found for deal '{deal_id}' on {payment_date}. "
                   "Check that the payment date is valid."
        )

    headers = result[0]
    return [dict(zip(headers, row)) for row in result[1:]]


def _extract_beginning_balances(prior_result: Optional[Dict]) -> Optional[Dict[str, float]]:
    """Extract ending class balances from prior period result to use as beginning balances."""
    if not prior_result:
        return None
    class_details = prior_result.get("class_details", [])
    balances = {}
    for cd in class_details:
        class_name = cd.get("class_name")
        ending_principal = cd.get("ending_principal", 0.0)
        if class_name:
            balances[class_name] = ending_principal
    return balances if balances else None


@router.post("/compute", summary="Compute payment waterfall for a deal and payment date")
async def compute_payment_waterfall(request: WaterfallComputeRequest):
    """
    Execute the payment waterfall computation for a deal on a specific payment date.
    
    This will:
    1. Load the deal configuration from saved JSON
    2. Fetch the loantape from Snowflake for the given payment date
    3. Run the waterfall computation engine
    4. Save and return the result
    
    The result includes:
    - Complete class-level payment details (interest, principal, factors)
    - Collateral performance (delinquency buckets, CDR/CPR)
    - Fee and expense payments
    - Priority of payments trace
    - Loan-level details (PIF, bankruptcy, etc.)
    """
    deal_id = request.deal_id
    payment_date = request.payment_date

    # Validate deal config exists
    deal_config = load_deal_config(deal_id)
    if not deal_config:
        raise HTTPException(
            status_code=404,
            detail=f"Deal configuration not found for '{deal_id}'. "
                   "Please upload and save the deal indenture first."
        )

    # Fetch loantape from Snowflake
    logger.info(f"Fetching loantape for {deal_id} / {payment_date}")
    loans = await _fetch_loantape(deal_id, payment_date)
    logger.info(f"Fetched {len(loans)} loans for {deal_id} / {payment_date}")

    # Get prior period result for beginning balances + full chronological history
    # for rolling averages (3M/inception CPR/CDR, 6M delinquency trigger, etc.).
    prior_result = get_prior_waterfall(deal_id, payment_date)
    prior_history = list_prior_waterfall_results(deal_id, payment_date)
    beginning_balances = _extract_beginning_balances(prior_result)
    prior_reserve_balances = load_latest_prior_reserve_balances(deal_id, payment_date)
    prior_fee_shortfalls = load_latest_prior_fee_shortfalls(deal_id, payment_date)

    # Override with user-provided balances if any
    if request.override_beginning_balances:
        if not beginning_balances:
            beginning_balances = {}
        beginning_balances.update(request.override_beginning_balances)

    # Determine SOFR rate
    sofr_rate = request.sofr_rate
    if sofr_rate is None:
        sofr_rate = deal_config.default_sofr_rate or 0.0530  # 5.30% default

    # Run waterfall computation
    logger.info(f"Computing waterfall for {deal_id} / {payment_date}, SOFR={sofr_rate:.4f}")
    try:
        result = compute_waterfall(
            deal_config=deal_config,
            loans=loans,
            payment_date_str=payment_date,
            sofr_rate=sofr_rate,
            prior_class_balances=beginning_balances,
            prior_waterfall_result=prior_result,
            prior_history=prior_history,
            prior_reserve_balances=prior_reserve_balances or None,
            prior_fee_shortfalls=prior_fee_shortfalls or None,
        )
    except Exception as e:
        logger.error(f"Waterfall computation failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Waterfall computation failed: {str(e)}")

    # Save result + per-period side-state for next period.
    save_waterfall_result(result)
    ending_reserves = {
        a.account_name: float(a.ending_balance_post_payment or 0.0)
        for a in (result.reserve_accounts or [])
    }
    save_reserve_balances(deal_id, payment_date, ending_reserves)
    ending_shortfalls: Dict[str, float] = {}
    for f in (result.fees_detail or []):
        if float(f.ending_shortfall or 0.0) > 0:
            ending_shortfalls[f.fee_name] = float(f.ending_shortfall)
    for e in (result.expenses_detail or []):
        if float(e.ending_shortfall or 0.0) > 0:
            ending_shortfalls[e.expense_name] = float(e.ending_shortfall)
    save_fee_shortfalls(deal_id, payment_date, ending_shortfalls)
    logger.info(f"Waterfall result saved for {deal_id} / {payment_date}")

    return result


@router.get("/{deal_id}", summary="List all computed waterfalls for a deal")
async def list_waterfalls(deal_id: str) -> List[WaterfallSummary]:
    """List all computed and saved waterfall results for a deal, most recent first."""
    return list_waterfall_results(deal_id)


@router.get("/{deal_id}/{payment_date}", summary="Get a specific waterfall computation result")
async def get_waterfall_result(deal_id: str, payment_date: str) -> WaterfallResult:
    """
    Retrieve a previously computed waterfall result.
    Payment date format: YYYY-MM-DD
    """
    result = load_waterfall_result(deal_id, payment_date)
    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No waterfall result found for deal '{deal_id}' on {payment_date}. "
                   "Run /compute first."
        )
    return result


@router.get("/{deal_id}/{payment_date}/class-summary", summary="Get class payment summary for a waterfall")
async def get_class_summary(deal_id: str, payment_date: str):
    """Get just the class payment summary table (Section 1a of the report)."""
    result = load_waterfall_result(deal_id, payment_date)
    if not result:
        raise HTTPException(status_code=404, detail=f"No waterfall result found for {deal_id}/{payment_date}")

    return {
        "deal_id": deal_id,
        "payment_date": payment_date,
        "distribution_date": result.distribution_date,
        "classes": [
            {
                "class_name": cd.class_name,
                "cusip": cd.cusip,
                "type": cd.class_type,
                "original_principal": cd.original_principal,
                "beginning_principal": cd.beginning_principal,
                "interest_paid": cd.interest_paid,
                "principal_paid": cd.principal_paid,
                "total_paid": cd.total_paid,
                "ending_principal": cd.ending_principal,
                "interest_rate": cd.interest_rate,
                "factor_beginning": cd.factor_beginning,
                "factor_ending": cd.factor_ending,
            }
            for cd in result.class_details
        ],
        "totals": {
            "total_interest_paid": result.total_interest_paid,
            "total_principal_paid": result.total_principal_paid,
            "total_paid": result.total_paid,
            "total_ending_principal": result.total_ending_principal,
        }
    }


@router.get("/{deal_id}/{payment_date}/collateral", summary="Get collateral performance for a waterfall")
async def get_collateral_performance(deal_id: str, payment_date: str):
    """Get collateral performance section (delinquency, CDR/CPR)."""
    result = load_waterfall_result(deal_id, payment_date)
    if not result:
        raise HTTPException(status_code=404, detail=f"No waterfall result found for {deal_id}/{payment_date}")

    return {
        "deal_id": deal_id,
        "payment_date": payment_date,
        "pool_summary": {
            "prior_balance": result.prior_pool_balance,
            "current_balance": result.current_pool_balance,
            "loan_count": result.current_loan_count,
            "prepayments_in_full": result.prepayments_in_full,
            "scheduled_principal": result.scheduled_principal_collateral,
            "curtailments": result.curtailments,
        },
        "delinquency_buckets": [
            {
                "bucket": b.bucket,
                "amount": b.amount,
                "count": b.count,
                "pct_amount": b.pct_amount,
                "pct_count": b.pct_count,
            }
            for b in result.performance_buckets
        ],
        "rates": {
            "cdr_1m": result.collateral_rates.cdr_1m,
            "cpr_1m": result.collateral_rates.cpr_1m,
            "smm_default": result.collateral_rates.smm_default,
            "smm_prepay": result.collateral_rates.smm_prepay,
        },
        "net_wac": result.net_wac,
        "gross_wac": result.gross_wac,
        "benchmark_rate": result.benchmark_rate,
    }


@router.delete("/{deal_id}/{payment_date}", summary="Delete a waterfall computation result")
async def delete_waterfall_result(deal_id: str, payment_date: str):
    """Delete a specific waterfall computation result."""
    from pathlib import Path
    from services.deal_store import REPORTS_DIR

    safe_date = payment_date.replace("-", "")
    path = REPORTS_DIR / deal_id / f"waterfall_{safe_date}.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"No waterfall result found for {deal_id}/{payment_date}")

    path.unlink()
    return {"success": True, "message": f"Waterfall result deleted for {deal_id}/{payment_date}"}
