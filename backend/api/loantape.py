"""
API Endpoints for Loantape Data (fetched from Snowflake).

Endpoints:
  GET /api/loantape/deals                    - List available deals in Snowflake
  GET /api/loantape/{deal_id}/payment-dates  - Get payment dates for a deal
  GET /api/loantape/{deal_id}/data           - Fetch full loantape for deal + payment date
  GET /api/loantape/{deal_id}/summary        - Get aggregated summary for a payment date
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from config.database import check_and_reconnect_db, executeQuery

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/loantape", tags=["Loantape"])

# Key loantape columns needed for waterfall computation
WATERFALL_COLUMNS = [
    "LOAN_ID",
    "DEAL_ID",
    "PAYMENT_DATE",
    "BEGINNING_LOAN_BALANCE",
    "CURRENT_PRINCIPAL_BALANCE",
    "PRIOR_PRINCIPAL_BALANCES",
    "ORIGINAL_PRINCIPAL_BALANCE",
    "INTEREST_PAYMENT",
    "GROSS_INTEREST",
    "PRINCIPAL_PAYMENT",
    "PRINCIPAL_PAYMENT_SCHEDULED",
    "PRINCIPAL_PAYMENT_CURTAILMENTS",
    "PRINCIPAL_PAYMENT_PIF",
    "PRINCIPAL_LIQUIDATED",
    "PRINCIPAL_PAYMENT_REPURCHASE",
    "SERVICING_FEES",
    "SERVICING_FEE_RATE",
    "CURRENT_INTEREST_RATE",
    "LOAN_STATUS",
    "ACCOUNT_STATUS",
    "NUMBER_OF_DAYS_IN_ARREARS",
    "ORIGINATION_DATE",
    "MATURITY_DATE",
    "SERVICER_NAME",
    "SERVICER",
    "FUNDED",
    "FUNDED_REMIT_BAL",
    "CAPITALIZED_AMOUNTS",
    "CHARGE_OFFS",
    "ALLOCATED_LOSSES",
    "CUMULATIVE_RECOVERIES",
    "RECOVERIES",
    "BANKRUPTCY_FLAG",
    "DEFAULTED_FLAG",
    "MODIFICATION_FLAG",
    "LOAN_MODIFICATION_TYPE",
    "DEFERRED_BEGINNING_BALANCE",
    "DEFERRED_ENDING_BALANCE",
    "BORROWER_STATE",
    "PROPERTY_STATE",
    "BORROWER_FICO",
    "CURRENT_LOAN_TO_VALUE",
    "ORIGINAL_LOAN_TO_VALUE",
    "LIEN",
    "PROPERTY_TYPE",
]


def _rows_to_dicts(result) -> List[Dict[str, Any]]:
    """Convert executeQuery result (headers + rows) to list of dicts."""
    if not result or len(result) < 2:
        return []
    headers = result[0]
    return [dict(zip(headers, row)) for row in result[1:]]


@router.get("/deals", summary="List all available deals in Snowflake")
async def list_snowflake_deals():
    """Get list of distinct deal IDs from the Snowflake deal_info table."""
    query = "SELECT DISTINCT DEAL_ID FROM IA_DEMO.PUBLIC.deal_info ORDER BY DEAL_ID"
    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Snowflake query failed: {err}")

    deals = [row[0] for row in result[1:]] if result else []
    return {"deals": deals, "total": len(deals)}


@router.get("/{deal_id}/payment-dates", summary="Get payment dates for a deal")
async def get_payment_dates(deal_id: str):
    """
    Get all available payment dates for a specific deal from Snowflake.
    Returns dates in descending order (most recent first).
    """
    query = f"""
        SELECT DISTINCT payment_date 
        FROM IA_DEMO.PUBLIC.LOAN_TAPE 
        WHERE deal_id = '{deal_id}' 
        ORDER BY payment_date DESC
    """
    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Snowflake query failed: {err}")

    if not result or len(result) < 2:
        raise HTTPException(status_code=404, detail=f"No payment dates found for deal '{deal_id}'")

    dates = [str(row[0]) for row in result[1:]]
    return {
        "deal_id": deal_id,
        "payment_dates": dates,
        "total": len(dates),
        "latest": dates[0] if dates else None,
        "earliest": dates[-1] if dates else None,
    }


@router.get("/{deal_id}/data", summary="Fetch loantape data for a deal and payment date")
async def get_loantape_data(
    deal_id: str,
    payment_date: str = Query(..., description="Payment date in YYYY-MM-DD format"),
    limit: Optional[int] = Query(None, description="Limit number of loans returned (for preview)"),
):
    """
    Fetch the loantape (loan-level data) from Snowflake for a specific deal and payment date.
    
    This data is used as input to the waterfall computation engine.
    """
    # Build column list for query
    col_list = ", ".join(WATERFALL_COLUMNS)
    limit_clause = f"LIMIT {limit}" if limit else ""

    query = f"""
        SELECT {col_list}
        FROM IA_DEMO.PUBLIC.LOAN_TAPE
        WHERE DEAL_ID = '{deal_id}'
          AND PAYMENT_DATE = '{payment_date}'
        {limit_clause}
    """

    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Snowflake query failed: {err}")

    if not result:
        raise HTTPException(
            status_code=404,
            detail=f"No loantape data found for deal '{deal_id}' on {payment_date}"
        )

    loans = _rows_to_dicts(result)
    return {
        "deal_id": deal_id,
        "payment_date": payment_date,
        "loan_count": len(loans),
        "loans": loans,
    }


@router.get("/{deal_id}/summary", summary="Get aggregated loantape summary for a payment date")
async def get_loantape_summary(
    deal_id: str,
    payment_date: str = Query(..., description="Payment date in YYYY-MM-DD format"),
):
    """
    Get an aggregated summary of loantape metrics for a deal and payment date.
    Includes: pool balance, interest/principal collections, delinquency breakdown.
    """
    query = f"""
        SELECT 
            COUNT(*) AS LOAN_COUNT,
            SUM(BEGINNING_LOAN_BALANCE) AS TOTAL_BEG_BALANCE,
            SUM(CURRENT_PRINCIPAL_BALANCE) AS TOTAL_END_BALANCE,
            SUM(INTEREST_PAYMENT) AS TOTAL_INTEREST,
            SUM(GROSS_INTEREST) AS TOTAL_GROSS_INTEREST,
            SUM(PRINCIPAL_PAYMENT) AS TOTAL_PRINCIPAL,
            SUM(PRINCIPAL_PAYMENT_SCHEDULED) AS TOTAL_SCHED_PRINCIPAL,
            SUM(PRINCIPAL_PAYMENT_CURTAILMENTS) AS TOTAL_CURTAILMENTS,
            SUM(PRINCIPAL_PAYMENT_PIF) AS TOTAL_PIF,
            SUM(SERVICING_FEES) AS TOTAL_SVC_FEES,
            AVG(CURRENT_INTEREST_RATE) AS AVG_RATE,
            SUM(CURRENT_PRINCIPAL_BALANCE * CURRENT_INTEREST_RATE) / NULLIF(SUM(CURRENT_PRINCIPAL_BALANCE), 0) AS WAC,
            SUM(CHARGE_OFFS) AS TOTAL_CHARGE_OFFS,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS = 0 OR NUMBER_OF_DAYS_IN_ARREARS IS NULL THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS CURRENT_BALANCE,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 1 AND 29 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_1_29,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 30 AND 59 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_30_59,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 60 AND 89 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_60_89,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 90 AND 119 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_90_119,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 120 AND 149 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_120_149,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS >= 150 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS DLQ_150_PLUS
        FROM IA_DEMO.PUBLIC.LOAN_TAPE
        WHERE DEAL_ID = '{deal_id}'
          AND PAYMENT_DATE = '{payment_date}'
    """

    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Snowflake query failed: {err}")

    if not result or len(result) < 2:
        raise HTTPException(status_code=404, detail=f"No data found for deal '{deal_id}' on {payment_date}")

    headers = result[0]
    row = result[1]
    summary = dict(zip(headers, row))

    # Safe float helper
    def _sf(v):
        try:
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    total_bal = _sf(summary.get("TOTAL_END_BALANCE"))

    return {
        "deal_id": deal_id,
        "payment_date": payment_date,
        "loan_count": int(_sf(summary.get("LOAN_COUNT"))),
        "pool_balance": {
            "beginning": _sf(summary.get("TOTAL_BEG_BALANCE")),
            "ending": total_bal,
        },
        "collections": {
            "gross_interest": _sf(summary.get("TOTAL_GROSS_INTEREST")) or _sf(summary.get("TOTAL_INTEREST")),
            "interest": _sf(summary.get("TOTAL_INTEREST")),
            "total_principal": _sf(summary.get("TOTAL_PRINCIPAL")),
            "scheduled_principal": _sf(summary.get("TOTAL_SCHED_PRINCIPAL")),
            "curtailments": _sf(summary.get("TOTAL_CURTAILMENTS")),
            "pif": _sf(summary.get("TOTAL_PIF")),
            "servicing_fees": _sf(summary.get("TOTAL_SVC_FEES")),
        },
        "rates": {
            "wac": _sf(summary.get("WAC")),
            "avg_rate": _sf(summary.get("AVG_RATE")),
        },
        "delinquency": {
            "current": _sf(summary.get("CURRENT_BALANCE")),
            "1_29_days": _sf(summary.get("DLQ_1_29")),
            "30_59_days": _sf(summary.get("DLQ_30_59")),
            "60_89_days": _sf(summary.get("DLQ_60_89")),
            "90_119_days": _sf(summary.get("DLQ_90_119")),
            "120_149_days": _sf(summary.get("DLQ_120_149")),
            "150_plus_days": _sf(summary.get("DLQ_150_PLUS")),
        },
        "charge_offs": _sf(summary.get("TOTAL_CHARGE_OFFS")),
    }


@router.get("/{deal_id}/delinquency-history", summary="Get delinquency trend history across payment dates")
async def get_delinquency_history(
    deal_id: str,
    months: int = Query(12, description="Number of recent months to fetch"),
):
    """
    Get historical delinquency data across multiple payment dates.
    Useful for CDR/CPR trend charts in the investor report.
    """
    query = f"""
        SELECT 
            PAYMENT_DATE,
            COUNT(*) AS LOAN_COUNT,
            SUM(BEGINNING_LOAN_BALANCE) AS BEG_BALANCE,
            SUM(CURRENT_PRINCIPAL_BALANCE) AS END_BALANCE,
            SUM(INTEREST_PAYMENT) AS INTEREST,
            SUM(PRINCIPAL_PAYMENT) AS PRINCIPAL,
            SUM(PRINCIPAL_PAYMENT_SCHEDULED) AS SCHED_PRINCIPAL,
            SUM(PRINCIPAL_PAYMENT_CURTAILMENTS) + SUM(PRINCIPAL_PAYMENT_PIF) AS UNSCHEDULED_PRINCIPAL,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 120 AND 149 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS NEW_DEFAULTS
        FROM IA_DEMO.PUBLIC.LOAN_TAPE
        WHERE DEAL_ID = '{deal_id}'
        GROUP BY PAYMENT_DATE
        ORDER BY PAYMENT_DATE DESC
        LIMIT {months}
    """

    result, err, _ = await executeQuery(query)
    if err != 0:
        raise HTTPException(status_code=500, detail=f"Snowflake query failed: {err}")

    if not result or len(result) < 2:
        return {"deal_id": deal_id, "history": []}

    def _sf(v):
        try:
            return float(v) if v is not None else 0.0
        except Exception:
            return 0.0

    history = []
    for row in result[1:]:
        headers = result[0]
        r = dict(zip(headers, row))
        beg = _sf(r.get("BEG_BALANCE"))
        sched = _sf(r.get("SCHED_PRINCIPAL"))
        unscheduled = _sf(r.get("UNSCHEDULED_PRINCIPAL"))
        new_defaults = _sf(r.get("NEW_DEFAULTS"))

        smm_default = new_defaults / beg if beg > 0 else 0.0
        cdr = 1.0 - (1.0 - smm_default) ** 12 if 0 < smm_default < 1 else 0.0

        denom = beg - sched
        smm_prepay = unscheduled / denom if denom > 0 else 0.0
        cpr = 1.0 - (1.0 - smm_prepay) ** 12 if 0 < smm_prepay < 1 else 0.0

        history.append({
            "payment_date": str(r.get("PAYMENT_DATE", "")),
            "loan_count": int(_sf(r.get("LOAN_COUNT"))),
            "beginning_balance": beg,
            "ending_balance": _sf(r.get("END_BALANCE")),
            "interest_collected": _sf(r.get("INTEREST")),
            "principal_collected": _sf(r.get("PRINCIPAL")),
            "scheduled_principal": sched,
            "unscheduled_principal": unscheduled,
            "new_defaults": new_defaults,
            "smm_prepay": smm_prepay,
            "cpr_1m": cpr,
            "smm_default": smm_default,
            "cdr_1m": cdr,
        })

    return {
        "deal_id": deal_id,
        "months_returned": len(history),
        "history": history,
    }
