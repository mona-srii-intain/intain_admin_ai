"""
API Endpoints for Deal Configuration CRUD.

Endpoints:
  GET    /api/deals                 - List all saved deals
  GET    /api/deals/{deal_id}       - Get deal configuration
  POST   /api/deals                 - Create new deal configuration (manual entry)
  PUT    /api/deals/{deal_id}       - Update deal configuration
  DELETE /api/deals/{deal_id}       - Delete deal configuration
  POST   /api/deals/{deal_id}/verify - Mark as manually verified
"""
from __future__ import annotations

import datetime
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from models.deal import DealConfig, DealSummary, MakerCheckerReview
from services.deal_store import (
    deal_exists,
    delete_deal_config,
    list_deals,
    load_deal_config,
    save_deal_config,
)

router = APIRouter(prefix="/api/deals", tags=["Deal Configuration"])


@router.get("", summary="List all saved deal configurations")
async def list_deal_configs() -> List[DealSummary]:
    """Return a list of all saved deal configurations with key summary fields."""
    return list_deals()


@router.get("/{deal_id}", summary="Get full deal configuration")
async def get_deal_config(deal_id: str) -> DealConfig:
    """Retrieve the full deal configuration for a given deal ID."""
    config = load_deal_config(deal_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    return config


@router.post("", summary="Create a new deal configuration manually")
async def create_deal_config(deal_config: DealConfig):
    """
    Create a new deal configuration via manual entry.
    
    Use this for deals that don't have a digital indenture, or to manually
    create a configuration from scratch.
    """
    if deal_exists(deal_config.deal_id):
        raise HTTPException(
            status_code=409,
            detail=f"Deal '{deal_config.deal_id}' already exists. Use PUT to update."
        )

    now = datetime.datetime.utcnow().isoformat()
    deal_config.created_at = now
    deal_config.updated_at = now
    deal_config.extraction_source = "manual"

    deal_id = save_deal_config(deal_config)
    return {"success": True, "message": f"Deal '{deal_id}' created.", "deal_id": deal_id}


@router.put("/{deal_id}", summary="Update an existing deal configuration")
async def update_deal_config(deal_id: str, deal_config: DealConfig):
    """Update an existing deal configuration. Preserves created_at timestamp."""
    existing = load_deal_config(deal_id)
    if not existing:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")

    deal_config.deal_id = deal_id
    deal_config.created_at = existing.created_at
    deal_config.updated_at = datetime.datetime.utcnow().isoformat()

    save_deal_config(deal_config)
    return {"success": True, "message": f"Deal '{deal_id}' updated.", "deal_id": deal_id}


@router.delete("/{deal_id}", summary="Delete a deal configuration")
async def delete_deal(deal_id: str):
    """Delete a deal configuration. This does not delete computed waterfall results."""
    if not deal_exists(deal_id):
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    delete_deal_config(deal_id)
    return {"success": True, "message": f"Deal '{deal_id}' deleted."}


@router.post("/{deal_id}/verify", summary="Mark a deal configuration as manually verified")
async def verify_deal_config(deal_id: str, reviewer_name: Optional[str] = None):
    """Mark the deal configuration as manually verified (maker-checker approval)."""
    config = load_deal_config(deal_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")

    now = datetime.datetime.utcnow().isoformat()
    config.manually_verified = True
    config.verified_by = reviewer_name
    config.verified_at = now
    config.updated_at = now

    save_deal_config(config)
    return {
        "success": True,
        "message": f"Deal '{deal_id}' marked as verified.",
        "deal_id": deal_id,
        "verified_by": reviewer_name,
        "verified_at": now,
    }


@router.get("/{deal_id}/classes", summary="Get certificate classes for a deal")
async def get_deal_classes(deal_id: str):
    """Get the list of certificate classes for a deal."""
    config = load_deal_config(deal_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    return {
        "deal_id": deal_id,
        "deal_name": config.deal_name,
        "classes": [cls.model_dump() for cls in config.classes],
        "total_classes": len(config.classes),
    }


@router.get("/{deal_id}/waterfall-rules", summary="Get waterfall rules for a deal")
async def get_deal_waterfall_rules(deal_id: str):
    """Get the priority of payments waterfall rules for a deal."""
    config = load_deal_config(deal_id)
    if not config:
        raise HTTPException(status_code=404, detail=f"Deal '{deal_id}' not found")
    return {
        "deal_id": deal_id,
        "deal_name": config.deal_name,
        "interest_waterfall": [s.model_dump() for s in config.interest_waterfall],
        "principal_waterfall": [s.model_dump() for s in config.principal_waterfall],
        "excess_cashflow_waterfall": [s.model_dump() for s in config.excess_cashflow_waterfall],
        "interest_steps": len(config.interest_waterfall),
        "principal_steps": len(config.principal_waterfall),
        "excess_steps": len(config.excess_cashflow_waterfall),
    }
