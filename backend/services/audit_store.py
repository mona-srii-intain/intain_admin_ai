"""
Persistence layer for deal audit logs.
Each deal gets its own JSON file: backend/data/audit/{deal_id}.json
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from models.audit import AuditAnnotation, AuditEntry, AuditLog

AUDIT_DIR = Path(__file__).resolve().parents[1] / "data" / "audit"
AUDIT_DIR.mkdir(parents=True, exist_ok=True)


def _audit_path(deal_id: str) -> Path:
    return AUDIT_DIR / f"{deal_id}.json"


def load_audit_log(deal_id: str) -> AuditLog:
    path = _audit_path(deal_id)
    if not path.exists():
        return AuditLog(deal_id=deal_id)
    with open(path) as f:
        return AuditLog(**json.load(f))


def _save_audit_log(log: AuditLog) -> None:
    with open(_audit_path(log.deal_id), "w") as f:
        json.dump(log.model_dump(), f, indent=2)


def upsert_audit_entry(
    deal_id: str,
    section: str,
    row_id: str,
    annotations: List[AuditAnnotation],
    reviewer: Optional[str] = "Admin",
) -> AuditEntry:
    """
    Create or replace the audit entry for (deal_id, section, row_id).
    Stamps a reviewer name onto each annotation and persists.
    """
    log = load_audit_log(deal_id)

    # Stamp reviewer name
    for ann in annotations:
        ann.reviewer = reviewer

    # Find existing entry for this row
    existing = next(
        (e for e in log.entries if e.section == section and e.row_id == row_id),
        None,
    )

    if existing:
        existing.annotations = annotations
        existing.updated_at = datetime.utcnow().isoformat()
        entry = existing
    else:
        entry = AuditEntry(
            deal_id=deal_id,
            section=section,
            row_id=row_id,
            annotations=annotations,
        )
        log.entries.append(entry)

    _save_audit_log(log)
    return entry


def get_audit_entry(deal_id: str, section: str, row_id: str) -> Optional[AuditEntry]:
    log = load_audit_log(deal_id)
    return next(
        (e for e in log.entries if e.section == section and e.row_id == row_id),
        None,
    )
