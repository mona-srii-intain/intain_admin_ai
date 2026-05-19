"""
Audit log API endpoints.

POST /api/audit/{deal_id}/entry   — save (create or replace) annotations for a row
GET  /api/audit/{deal_id}         — retrieve the full audit log for a deal
GET  /api/audit/{deal_id}/entry   — retrieve one row's annotations (?section=…&row_id=…)
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from models.audit import AuditLog, AuditEntry, SaveAuditRequest
from services.audit_store import upsert_audit_entry, load_audit_log, get_audit_entry

router = APIRouter(prefix="/api/audit", tags=["Audit"])


@router.post("/{deal_id}/entry", response_model=AuditEntry)
async def save_audit_entry(deal_id: str, request: SaveAuditRequest):
    """Save (create or replace) all annotations for one table row."""
    if not request.annotations:
        raise HTTPException(status_code=400, detail="At least one annotation is required.")
    entry = upsert_audit_entry(
        deal_id=deal_id,
        section=request.section,
        row_id=request.row_id,
        annotations=request.annotations,
        reviewer=request.reviewer,
    )
    return entry


@router.get("/{deal_id}", response_model=AuditLog)
async def get_audit_log(deal_id: str):
    """Return the complete audit log for a deal."""
    return load_audit_log(deal_id)


@router.get("/{deal_id}/entry", response_model=AuditEntry)
async def get_row_audit(
    deal_id: str,
    section: str = Query(...),
    row_id: str = Query(...),
):
    """Return audit annotations for a specific row."""
    entry = get_audit_entry(deal_id, section, row_id)
    if not entry:
        raise HTTPException(status_code=404, detail="No audit entry found for this row.")
    return entry
