"""
Audit log models for deal configuration changes.
Each row-level change can reference one or more client emails that triggered it.
"""
from __future__ import annotations
from typing import List, Optional
from pydantic import BaseModel, Field
from datetime import datetime
import uuid


class AuditAnnotation(BaseModel):
    """A single email-based annotation for a row change."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str = Field(..., description="Name / email of the person who sent the instruction")
    content: str = Field(..., description="Relevant excerpt from the email")
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat(),
        description="When this annotation was recorded (UTC ISO-8601)",
    )
    reviewer: Optional[str] = Field(None, description="Reviewer who logged this annotation")


class AuditEntry(BaseModel):
    """All annotations attached to one table row in one section."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    deal_id: str
    section: str = Field(..., description="e.g. classes, fees, interest_waterfall, …")
    row_id: str = Field(..., description="Natural key for the row, e.g. class_name or fee_name")
    annotations: List[AuditAnnotation] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())


class AuditLog(BaseModel):
    """The full audit log for a deal."""
    deal_id: str
    entries: List[AuditEntry] = Field(default_factory=list)


class SaveAuditRequest(BaseModel):
    """Request body for POST /api/audit/{deal_id}/entry."""
    section: str
    row_id: str
    annotations: List[AuditAnnotation]
    reviewer: Optional[str] = "Admin"
