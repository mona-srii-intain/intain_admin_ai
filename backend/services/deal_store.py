"""
Deal Configuration Storage Service.
Handles saving and retrieving deal configs as JSON files.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from models.deal import DealConfig, DealSummary
from models.waterfall import WaterfallResult, WaterfallSummary

BASE_DIR = Path(__file__).resolve().parents[1] / "data"
DEALS_DIR = BASE_DIR / "deals"
REPORTS_DIR = BASE_DIR / "reports"

DEALS_DIR.mkdir(parents=True, exist_ok=True)
REPORTS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Deal Config persistence
# ---------------------------------------------------------------------------

def _deal_path(deal_id: str) -> Path:
    """Path to the deal config JSON file."""
    return DEALS_DIR / f"{deal_id}.json"


def save_deal_config(deal_config: DealConfig) -> str:
    """Save deal config to JSON file. Returns deal_id."""
    deal_config.updated_at = datetime.utcnow().isoformat()
    if not deal_config.created_at:
        deal_config.created_at = deal_config.updated_at

    path = _deal_path(deal_config.deal_id)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(deal_config.model_dump(), f, indent=2, default=str)

    return deal_config.deal_id


def load_deal_config(deal_id: str) -> Optional[DealConfig]:
    """Load deal config from JSON file. Returns None if not found."""
    path = _deal_path(deal_id)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return DealConfig(**data)


def delete_deal_config(deal_id: str) -> bool:
    """Delete deal config. Returns True if deleted."""
    path = _deal_path(deal_id)
    if path.exists():
        path.unlink()
        return True
    return False


def list_deals() -> List[DealSummary]:
    """List all saved deals as summaries."""
    summaries = []
    for path in sorted(DEALS_DIR.glob("*.json")):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Build summary
            classes = data.get("classes", [])
            summaries.append(DealSummary(
                deal_id=data["deal_id"],
                deal_name=data.get("deal_name", ""),
                asset_class=data.get("asset_class", ""),
                asset_type=data.get("asset_type", ""),
                original_pool_balance=data.get("original_pool_balance", 0.0),
                closing_date=data.get("closing_date"),
                first_payment_date=data.get("first_payment_date"),
                class_count=len(classes),
                manually_verified=data.get("manually_verified", False),
                created_at=data.get("created_at"),
            ))
        except Exception:
            continue
    return summaries


def deal_exists(deal_id: str) -> bool:
    """Check if a deal config exists."""
    return _deal_path(deal_id).exists()


# ---------------------------------------------------------------------------
# Audit / change-log persistence
# ---------------------------------------------------------------------------

def _audit_path(deal_id: str) -> Path:
    return DEALS_DIR / f"{deal_id}_audit.json"


def load_audit(deal_id: str) -> Dict[str, List[Dict]]:
    """Load audit annotations for a deal. Returns dict keyed by row_key."""
    path = _audit_path(deal_id)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("entries", {})


def append_audit_entry(deal_id: str, row_key: str, sender: str, content: str) -> Dict:
    """Append a single audit entry for a row and persist it. Returns the new entry."""
    path = _audit_path(deal_id)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"deal_id": deal_id, "entries": {}}

    entry = {
        "sender": sender,
        "content": content,
        "created_at": datetime.utcnow().isoformat() + "Z",
    }
    data["entries"].setdefault(row_key, []).append(entry)

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return entry


# ---------------------------------------------------------------------------
# Waterfall result persistence
# ---------------------------------------------------------------------------

def _waterfall_dir(deal_id: str) -> Path:
    """Directory for storing waterfall results for a deal."""
    d = REPORTS_DIR / deal_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _waterfall_path(deal_id: str, payment_date: str) -> Path:
    """Path to waterfall result JSON."""
    safe_date = payment_date.replace("-", "")
    return _waterfall_dir(deal_id) / f"waterfall_{safe_date}.json"


def save_waterfall_result(result: WaterfallResult) -> str:
    """Save waterfall computation result as JSON."""
    path = _waterfall_path(result.deal_id, result.payment_date)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result.model_dump(), f, indent=2, default=str)
    return str(path)


def load_waterfall_result(deal_id: str, payment_date: str) -> Optional[WaterfallResult]:
    """Load a saved waterfall result."""
    path = _waterfall_path(deal_id, payment_date)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return WaterfallResult(**data)


def load_waterfall_result_raw(deal_id: str, payment_date: str) -> Optional[Dict]:
    """Load a saved waterfall result as raw dict (for prior period reference)."""
    path = _waterfall_path(deal_id, payment_date)
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def list_waterfall_results(deal_id: str) -> List[WaterfallSummary]:
    """List all computed waterfall results for a deal."""
    summaries = []
    d = REPORTS_DIR / deal_id
    if not d.exists():
        return summaries

    for path in sorted(d.glob("waterfall_*.json"), reverse=True):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            rates = data.get("collateral_rates", {})
            summaries.append(WaterfallSummary(
                deal_id=data["deal_id"],
                payment_date=data["payment_date"],
                deal_name=data.get("deal_name", ""),
                current_pool_balance=data.get("current_pool_balance", 0.0),
                total_interest_paid=data.get("total_interest_paid", 0.0),
                total_principal_paid=data.get("total_principal_paid", 0.0),
                current_loan_count=data.get("current_loan_count", 0),
                cpr_1m=rates.get("cpr_1m", 0.0),
                cdr_1m=rates.get("cdr_1m", 0.0),
                computed_at=data.get("computed_at", ""),
            ))
        except Exception:
            continue
    return summaries


def get_prior_waterfall(deal_id: str, payment_date: str) -> Optional[Dict]:
    """
    Get the most recent waterfall result prior to a given payment date.
    Used to seed beginning balances for computation.
    """
    d = REPORTS_DIR / deal_id
    if not d.exists():
        return None

    results = []
    for path in d.glob("waterfall_*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pd = data.get("payment_date", "")
            if pd < payment_date:
                results.append((pd, data))
        except Exception:
            continue

    if not results:
        return None

    results.sort(key=lambda x: x[0], reverse=True)
    return results[0][1]


def _reserve_balances_path(deal_id: str, payment_date: str) -> Path:
    """Path to the per-period saved reserve balances dict."""
    safe_date = payment_date.replace("-", "")
    return _waterfall_dir(deal_id) / f"reserve_balances_{safe_date}.json"


def save_reserve_balances(deal_id: str, payment_date: str, balances: Dict[str, float]) -> str:
    """Persist this period's ending reserve account balances."""
    path = _reserve_balances_path(deal_id, payment_date)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"deal_id": deal_id, "payment_date": payment_date, "balances": balances}, f, indent=2)
    return str(path)


def load_reserve_balances(deal_id: str, payment_date: str) -> Dict[str, float]:
    """Load reserve balances saved at a specific payment date. {} if not found."""
    path = _reserve_balances_path(deal_id, payment_date)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in (data.get("balances") or {}).items()}


def load_latest_prior_reserve_balances(deal_id: str, payment_date: str) -> Dict[str, float]:
    """Load the most recent reserve balances file for ``deal_id`` whose
    payment_date is strictly earlier than ``payment_date``. Empty dict if none."""
    d = REPORTS_DIR / deal_id
    if not d.exists():
        return {}
    candidates: List[Tuple[str, Path]] = []
    for path in d.glob("reserve_balances_*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pd = data.get("payment_date", "")
            if pd and pd < payment_date:
                candidates.append((pd, path))
        except Exception:
            continue
    if not candidates:
        return {}
    candidates.sort(key=lambda x: x[0], reverse=True)
    with open(candidates[0][1], "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in (data.get("balances") or {}).items()}


def _fee_shortfalls_path(deal_id: str, payment_date: str) -> Path:
    """Path to per-period saved fee/expense ending shortfalls."""
    safe_date = payment_date.replace("-", "")
    return _waterfall_dir(deal_id) / f"fee_shortfalls_{safe_date}.json"


def save_fee_shortfalls(deal_id: str, payment_date: str, shortfalls: Dict[str, float]) -> str:
    """Persist this period's ending fee/expense shortfalls."""
    path = _fee_shortfalls_path(deal_id, payment_date)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"deal_id": deal_id, "payment_date": payment_date, "shortfalls": shortfalls}, f, indent=2)
    return str(path)


def load_fee_shortfalls(deal_id: str, payment_date: str) -> Dict[str, float]:
    """Load fee shortfalls saved at a specific payment date. {} if not found."""
    path = _fee_shortfalls_path(deal_id, payment_date)
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in (data.get("shortfalls") or {}).items()}


def load_latest_prior_fee_shortfalls(deal_id: str, payment_date: str) -> Dict[str, float]:
    """Load the most recent fee_shortfalls file strictly prior to ``payment_date``."""
    d = REPORTS_DIR / deal_id
    if not d.exists():
        return {}
    candidates: List[Tuple[str, Path]] = []
    for path in d.glob("fee_shortfalls_*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pd = data.get("payment_date", "")
            if pd and pd < payment_date:
                candidates.append((pd, path))
        except Exception:
            continue
    if not candidates:
        return {}
    candidates.sort(key=lambda x: x[0], reverse=True)
    with open(candidates[0][1], "r", encoding="utf-8") as f:
        data = json.load(f)
    return {k: float(v) for k, v in (data.get("shortfalls") or {}).items()}


def list_prior_waterfall_results(
    deal_id: str,
    payment_date: str,
    limit: Optional[int] = None,
) -> List[Dict]:
    """
    Return all waterfall results for a deal prior to ``payment_date``,
    in chronological order (oldest first).

    Used to roll forward cumulative trackers and compute rolling averages
    (3-month / inception CPR & CDR, 6-month delinquency trigger, etc.).

    Args:
        deal_id: Deal identifier.
        payment_date: Cutoff date (YYYY-MM-DD). Only results with
            ``payment_date`` strictly less than this are included.
        limit: If set and > 0, return only the most recent N prior periods.
            The returned list remains chronological (oldest of those N first).

    Returns:
        List of raw dicts (one per period), oldest → newest. Empty list if none.
    """
    d = REPORTS_DIR / deal_id
    if not d.exists():
        return []

    found: List[Tuple[str, Dict]] = []
    for path in d.glob("waterfall_*.json"):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            pd_str = data.get("payment_date", "")
            if pd_str and pd_str < payment_date:
                found.append((pd_str, data))
        except Exception:
            continue

    found.sort(key=lambda x: x[0])  # chronological, oldest first
    if limit is not None and limit > 0 and len(found) > limit:
        found = found[-limit:]
    return [entry[1] for entry in found]
