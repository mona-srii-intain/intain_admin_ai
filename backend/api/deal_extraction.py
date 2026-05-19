"""
API Endpoints for Deal Indenture PDF Extraction.

Endpoints:
  POST /api/deals/extract          - Upload PDF and extract deal config via LLM
  POST /api/deals/review           - Submit maker-checker reviewed config
  GET  /api/deals/extract/status   - Check extraction progress (if using async)
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from models.deal import DealConfig, DealExtractionRequest, MakerCheckerReview
from services.deal_store import deal_exists, load_deal_config, save_deal_config, load_audit, append_audit_entry
from services.llm_agent import extract_deal_config_from_pdf

router = APIRouter(prefix="/api/deals", tags=["Deal Extraction"])

# In-memory extraction status store (for async progress tracking)
_extraction_status: Dict[str, Dict] = {}


# PDF storage — overridable via env var. Defaults to <backend>/data/pdfs so it
# works on Windows without root access (matches the existing data/ layout).
_DEFAULT_PDF_DIR = Path(__file__).resolve().parents[1] / "data" / "pdfs"
PDF_STORAGE_DIR = Path(os.getenv("PDF_STORAGE_DIR") or _DEFAULT_PDF_DIR)
PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

_SAFE_DEAL_ID = re.compile(r"^[A-Za-z0-9_\-]+$")


def _pdf_path_for(deal_id: str) -> Path:
    """Storage path for the indenture PDF of a deal. Guards against path traversal."""
    if not _SAFE_DEAL_ID.match(deal_id):
        raise HTTPException(status_code=400, detail="Invalid deal_id")
    return PDF_STORAGE_DIR / f"{deal_id}.pdf"


@router.post("/extract", summary="Upload deal indenture PDF and extract configuration via LLM")
async def extract_deal_config(
    file: UploadFile = File(..., description="Deal indenture PDF file"),
    deal_id: str = Form(..., description="Deal ID to assign to this deal"),
    overwrite: bool = Form(False, description="Overwrite if deal_id already exists"),
):
    """
    Upload a deal indenture PDF and extract the deal configuration using LLM.
    
    The LLM agent will:
    1. Read the entire PDF document
    2. Identify and extract certificate classes, rates, fees, waterfall rules
    3. Return structured deal configuration for maker-checker review
    
    The extraction may take 1-3 minutes for large documents (350+ pages).
    """
    if not overwrite and deal_exists(deal_id):
        raise HTTPException(
            status_code=409,
            detail=f"Deal '{deal_id}' already exists. Use overwrite=true to replace it."
        )

    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF document")

    # Save uploaded PDF to a temp file
    content = await file.read()
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="Uploaded file is empty")

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        # Extract deal config using LLM agent
        deal_config = await extract_deal_config_from_pdf(
            pdf_path=tmp_path,
            deal_id=deal_id,
        )

        # Persist the source PDF so the frontend verification panel can render it.
        # Done after successful extraction so we don't keep PDFs from failed runs.
        try:
            shutil.copy2(tmp_path, _pdf_path_for(deal_id))
        except Exception as copy_err:
            # Non-fatal: extraction succeeded; just log and continue without PDF panel.
            print(f"[deal_extraction] Failed to persist source PDF for {deal_id}: {copy_err}")

        # Auto-save as draft immediately — preserves data even if user closes tab
        # manually_verified stays False until the user clicks "Save & Verify"
        save_deal_config(deal_config)

        return {
            "success": True,
            "message": "Extraction complete. Draft saved — please review and confirm the extracted fields.",
            "deal_id": deal_id,
            "deal_name": deal_config.deal_name,
            "extraction_summary": {
                "classes_found": len(deal_config.classes),
                "fees_found": len(deal_config.fees),
                "interest_waterfall_steps": len(deal_config.interest_waterfall),
                "principal_waterfall_steps": len(deal_config.principal_waterfall),
                "excess_cashflow_steps": len(deal_config.excess_cashflow_waterfall),
            },
            "deal_config": deal_config.model_dump(),
            "section_page_map": deal_config.section_page_map or {},
            "requires_review": True,
            "auto_saved_as_draft": True,
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Extraction failed: {str(e)}")
    finally:
        # Clean up temp file
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


@router.post("/review", summary="Submit maker-checker reviewed deal configuration")
async def submit_reviewed_config(review: MakerCheckerReview):
    """
    Submit the reviewed and corrected deal configuration after maker-checker review.
    
    This endpoint:
    1. Accepts the reviewed/corrected config
    2. Marks it as manually_verified
    3. Saves it to storage
    """
    config = review.reviewed_config
    config.deal_id = review.deal_id
    config.manually_verified = True
    config.verified_by = review.reviewer_name
    
    import datetime
    config.verified_at = datetime.datetime.utcnow().isoformat()
    config.updated_at = config.verified_at

    deal_id = save_deal_config(config)

    return {
        "success": True,
        "message": f"Deal '{deal_id}' configuration saved and verified.",
        "deal_id": deal_id,
        "class_count": len(config.classes),
        "manually_verified": True,
        "corrections_applied": len(review.corrections),
    }


@router.get("/{deal_id}/audit", summary="Get audit change-log annotations for a deal")
async def get_audit_annotations(deal_id: str):
    """
    Returns all logged email-change annotations for a deal, keyed by row_key.
    Row keys follow the pattern  'section:identifier'  e.g. 'classes:A-1'.
    """
    entries = load_audit(deal_id)
    return {"deal_id": deal_id, "entries": entries}


@router.post("/{deal_id}/audit/entry", summary="Append a single audit annotation entry")
async def add_audit_entry(deal_id: str, body: dict):
    """
    Log an email-instructed change against a specific table row.
    Body: { row_key: str, sender: str, content: str }
    """
    row_key = body.get("row_key", "")
    sender  = body.get("sender", "").strip()
    content = body.get("content", "").strip()

    if not row_key or not sender or not content:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="row_key, sender and content are required.")

    entry = append_audit_entry(deal_id, row_key, sender, content)
    return {"success": True, "deal_id": deal_id, "row_key": row_key, "entry": entry}


@router.get("/{deal_id}/pdf", summary="Serve the stored indenture PDF for a deal")
async def get_deal_pdf(deal_id: str):
    """Return the stored source PDF used during extraction (application/pdf)."""
    path = _pdf_path_for(deal_id)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No source PDF stored for deal '{deal_id}'. Re-upload to enable verification.",
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        filename=f"{deal_id}.pdf",
        headers={"Cache-Control": "private, max-age=300"},
    )


@router.get("/{deal_id}/section-pages", summary="Get the section -> PDF page mapping for a deal")
async def get_section_pages(deal_id: str):
    """
    Return the precomputed `section_page_map` for a deal (UI section -> ranked 1-indexed
    PDF page numbers). Used by the frontend PDF verification panel.
    """
    config = load_deal_config(deal_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    return {
        "deal_id": deal_id,
        "section_page_map": config.section_page_map or {},
        "has_pdf": _pdf_path_for(deal_id).exists(),
    }


@router.post("/save-draft", summary="Save extracted config as draft (without full verification)")
async def save_draft_config(request: DealExtractionRequest):
    """
    Save deal configuration as a draft (extraction_source=llm_extracted, not yet verified).
    Useful for saving mid-review.
    """
    config = request.deal_config
    config.deal_id = request.deal_id
    config.manually_verified = False

    deal_id = save_deal_config(config)

    return {
        "success": True,
        "message": f"Draft configuration saved for deal '{deal_id}'.",
        "deal_id": deal_id,
    }
