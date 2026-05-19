"""
API Endpoints for Investor Report Generation.

Endpoints:
  POST /api/reports/generate         - Generate report (JSON + optional PDF)
  GET  /api/reports/{deal_id}        - List all generated reports for a deal
  GET  /api/reports/{deal_id}/{date} - Get full JSON report
  GET  /api/reports/{deal_id}/{date}/download - Download PDF report
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, Response

from models.waterfall import ReportGenerateRequest, WaterfallResult
from services.deal_store import (
    REPORTS_DIR,
    list_deals,
    list_waterfall_results,
    load_deal_config,
    load_waterfall_result,
    save_waterfall_result,
)
from services.report_generator import build_json_report, generate_pdf_report
from config.database import executeQuery

logger = logging.getLogger("uvicorn.error")
router = APIRouter(prefix="/api/reports", tags=["Reports"])


@router.post("/generate", summary="Generate investor report for a deal and payment date")
async def generate_report(request: ReportGenerateRequest):
    """
    Generate a comprehensive investor report.
    
    If a waterfall computation doesn't exist yet for the given deal + payment date,
    this endpoint will also trigger the computation automatically.
    
    Returns the full JSON report. If format='pdf', also generates a PDF file
    that can be downloaded via the /download endpoint.
    """
    deal_id = request.deal_id
    payment_date = request.payment_date

    # Check if waterfall result exists
    waterfall_result = load_waterfall_result(deal_id, payment_date)

    if not waterfall_result:
        # Auto-trigger waterfall computation
        deal_config = load_deal_config(deal_id)
        if not deal_config:
            raise HTTPException(
                status_code=404,
                detail=f"Deal '{deal_id}' not found. Please save deal configuration first."
            )

        # Fetch loantape
        from api.waterfall import LOANTAPE_COLS, _fetch_loantape, _extract_beginning_balances
        from services.deal_store import get_prior_waterfall, list_prior_waterfall_results
        from services.waterfall_engine import compute_waterfall

        loans = await _fetch_loantape(deal_id, payment_date)
        prior_result = get_prior_waterfall(deal_id, payment_date)
        prior_history = list_prior_waterfall_results(deal_id, payment_date)
        beginning_balances = _extract_beginning_balances(prior_result)

        sofr_rate = deal_config.default_sofr_rate or 0.0530
        try:
            waterfall_result = compute_waterfall(
                deal_config=deal_config,
                loans=loans,
                payment_date_str=payment_date,
                sofr_rate=sofr_rate,
                prior_class_balances=beginning_balances,
                prior_waterfall_result=prior_result,
                prior_history=prior_history,
            )
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Waterfall computation failed: {str(e)}")

        save_waterfall_result(waterfall_result)

    # Enrich with CDR/CPR history if requested
    if request.include_performance_history and waterfall_result:
        history = await _fetch_performance_history(deal_id)
        waterfall_result.performance_history = history

    # Build JSON report
    json_report = build_json_report(waterfall_result)

    # Generate PDF if requested
    pdf_generated = False
    if request.format == "pdf":
        try:
            safe_date = payment_date.replace("-", "")
            pdf_path = str(REPORTS_DIR / deal_id / f"report_{safe_date}.pdf")
            generate_pdf_report(waterfall_result, output_path=pdf_path)
            pdf_generated = True
        except Exception as e:
            logger.warning(f"PDF generation failed: {e}")

    return {
        "success": True,
        "deal_id": deal_id,
        "payment_date": payment_date,
        "pdf_generated": pdf_generated,
        "pdf_download_url": f"/api/reports/{deal_id}/{payment_date}/download" if pdf_generated else None,
        # Return the flat WaterfallResult so the frontend can render it directly.
        # The nested build_json_report is only used internally for PDF generation.
        "report": waterfall_result.model_dump(),
    }


@router.get("", summary="List all generated reports across all deals")
async def list_all_reports():
    """Return a flat list of all waterfall/report summaries across every saved deal."""
    all_reports = []
    for deal_summary in list_deals():
        for r in list_waterfall_results(deal_summary.deal_id):
            entry = r.model_dump()
            entry.setdefault("deal_name", deal_summary.deal_name)
            entry.setdefault("asset_class", deal_summary.asset_class)
            all_reports.append(entry)
    # Sort newest first by payment_date
    all_reports.sort(key=lambda x: x.get("payment_date", ""), reverse=True)
    return {"reports": all_reports, "total": len(all_reports)}


@router.get("/{deal_id}", summary="List all generated reports for a deal")
async def list_reports(deal_id: str):
    """List all computed waterfall results (reports) for a deal."""
    results = list_waterfall_results(deal_id)
    return {
        "deal_id": deal_id,
        "reports": [r.model_dump() for r in results],
        "total": len(results),
    }


@router.get("/{deal_id}/{payment_date}", summary="Get full JSON investor report")
async def get_report(deal_id: str, payment_date: str):
    """
    Get the waterfall result for a deal and payment date.
    Returns the flat WaterfallResult so the frontend can render it directly.
    """
    waterfall_result = load_waterfall_result(deal_id, payment_date)
    if not waterfall_result:
        raise HTTPException(
            status_code=404,
            detail=f"No report found for {deal_id}/{payment_date}. Run /generate first."
        )

    return waterfall_result.model_dump()


@router.get("/{deal_id}/{payment_date}/download", summary="Download PDF investor report")
async def download_report_pdf(
    deal_id: str,
    payment_date: str,
    regenerate: bool = Query(False, description="Force regenerate PDF even if exists"),
):
    """
    Download the PDF investor report for a deal and payment date.
    If the PDF doesn't exist, it will be generated on the fly.
    """
    safe_date = payment_date.replace("-", "")
    pdf_path = REPORTS_DIR / deal_id / f"report_{safe_date}.pdf"

    if not pdf_path.exists() or regenerate:
        # Generate PDF
        waterfall_result = load_waterfall_result(deal_id, payment_date)
        if not waterfall_result:
            raise HTTPException(
                status_code=404,
                detail=f"No waterfall result found for {deal_id}/{payment_date}. Run /generate first."
            )

        # Add history if available
        history = await _fetch_performance_history(deal_id)
        waterfall_result.performance_history = history

        try:
            generate_pdf_report(waterfall_result, output_path=str(pdf_path))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"PDF generation failed: {str(e)}")

    return FileResponse(
        path=str(pdf_path),
        media_type="application/pdf",
        filename=f"{deal_id}_investor_report_{payment_date}.pdf",
    )


@router.get("/{deal_id}/{payment_date}/waterfall-raw", summary="Get raw waterfall computation result")
async def get_waterfall_raw(deal_id: str, payment_date: str) -> WaterfallResult:
    """Get the raw WaterfallResult model (all computed fields, not formatted as report)."""
    result = load_waterfall_result(deal_id, payment_date)
    if not result:
        raise HTTPException(status_code=404, detail=f"No result found for {deal_id}/{payment_date}")
    return result


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _fetch_performance_history(deal_id: str):
    """Fetch historical CDR/CPR data from Snowflake for trend charts."""
    from models.waterfall import CollateralPerformanceHistory

    query = f"""
        SELECT 
            PAYMENT_DATE,
            SUM(BEGINNING_LOAN_BALANCE) AS BEG_BAL,
            SUM(CURRENT_PRINCIPAL_BALANCE) AS END_BAL,
            SUM(PRINCIPAL_PAYMENT_SCHEDULED) AS SCHED,
            SUM(PRINCIPAL_PAYMENT_CURTAILMENTS) + SUM(PRINCIPAL_PAYMENT_PIF) AS UNSCHED,
            SUM(CASE WHEN NUMBER_OF_DAYS_IN_ARREARS BETWEEN 120 AND 149 THEN CURRENT_PRINCIPAL_BALANCE ELSE 0 END) AS NEW_DEF
        FROM IA_DEMO.PUBLIC.LOAN_TAPE
        WHERE DEAL_ID = '{deal_id}'
        GROUP BY PAYMENT_DATE
        ORDER BY PAYMENT_DATE DESC
        LIMIT 24
    """
    try:
        result, err, _ = await executeQuery(query)
        if err != 0 or not result or len(result) < 2:
            return []

        history = []
        prev_cpr_history = []
        prev_cdr_history = []

        for row in reversed(result[1:]):  # oldest first for rolling calc
            headers = result[0]
            r = dict(zip(headers, row))

            def sf(v):
                try:
                    return float(v) if v is not None else 0.0
                except Exception:
                    return 0.0

            beg = sf(r.get("BEG_BAL"))
            sched = sf(r.get("SCHED"))
            unsched = sf(r.get("UNSCHED"))
            new_def = sf(r.get("NEW_DEF"))

            smm_def = new_def / beg if beg > 0 else 0.0
            cdr_1m = 1.0 - (1.0 - smm_def) ** 12 if 0 < smm_def < 1 else 0.0

            denom = beg - sched
            smm_pre = unsched / denom if denom > 0 else 0.0
            cpr_1m = 1.0 - (1.0 - smm_pre) ** 12 if 0 < smm_pre < 1 else 0.0

            prev_cdr_history.append(cdr_1m)
            prev_cpr_history.append(cpr_1m)

            # Rolling 3-month averages
            cdr_3m = sum(prev_cdr_history[-3:]) / len(prev_cdr_history[-3:]) if prev_cdr_history else 0.0
            cpr_3m = sum(prev_cpr_history[-3:]) / len(prev_cpr_history[-3:]) if prev_cpr_history else 0.0
            cdr_inc = sum(prev_cdr_history) / len(prev_cdr_history) if prev_cdr_history else 0.0
            cpr_inc = sum(prev_cpr_history) / len(prev_cpr_history) if prev_cpr_history else 0.0

            history.append(CollateralPerformanceHistory(
                date=str(r.get("PAYMENT_DATE", "")),
                beginning_balance=beg,
                new_defaults=new_def,
                smm_prepay=smm_pre,
                cpr_1m=cpr_1m,
                cpr_3m=cpr_3m,
                cpr_6m=0.0,
                cpr_12m=0.0,
                cpr_inception=cpr_inc,
                smm_default=smm_def,
                cdr_1m=cdr_1m,
                cdr_3m=cdr_3m,
                cdr_6m=0.0,
                cdr_12m=0.0,
                cdr_inception=cdr_inc,
                scheduled_principal=sched,
                unscheduled_principal=unsched,
            ))

        return list(reversed(history))  # most recent first

    except Exception as e:
        logger.warning(f"Could not fetch performance history: {e}")
        return []
