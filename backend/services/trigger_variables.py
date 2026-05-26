"""
Curated catalog of variables available inside the waterfall trigger evaluation
context.

Source of truth: ``backend/services/waterfall_engine.py``. When the engine
evaluates a trigger via ``_evaluate_config_trigger``, the only names that the
Python expression can reference are the keys of ``trigger_eval_context``
assembled around line 2266. Keep this list in sync with that dict — anything
not listed here will evaluate to ``NameError`` at compute time.

This catalog is static (not derived at runtime) for two reasons:
  1. Latency — no need to walk the engine on every prompt.
  2. Accuracy — the LLM gets curated descriptions and units, not raw variable
     names, so it can map plain-English phrases like "60+ day delinquency"
     to ``delinquency_60plus_pct`` reliably.
"""
from __future__ import annotations

from typing import Dict, List


class TriggerVariable(dict):
    """Plain dict subclass so it serializes cleanly through FastAPI."""


TRIGGER_VARIABLES: List[Dict[str, str]] = [
    {
        "name": "subordinate_balance",
        "type": "float",
        "units": "dollars",
        "description": (
            "Aggregate principal balance of all subordinate certificate classes "
            "(M-1 through B-4), at the start of the current period."
        ),
        "example_phrases": [
            "subordinate principal is fully depleted",
            "credit support has been wiped out",
            "no subordinate balance remaining",
        ],
        "example_expression": "subordinate_balance == 0",
    },
    {
        "name": "cumulative_loss_pct",
        "type": "float",
        "units": "decimal fraction (0.05 = 5%)",
        "description": (
            "Cumulative realized losses divided by the original aggregate "
            "principal balance of all main certificate classes excluding A-1. "
            "Use this for any threshold expressed as a percentage of cert "
            "principal."
        ),
        "example_phrases": [
            "cumulative losses exceed 5%",
            "lifetime loss rate above 4%",
            "realized losses over 7.5% of original certificate balance",
        ],
        "example_expression": "cumulative_loss_pct > 0.05",
    },
    {
        "name": "cumulative_losses",
        "type": "float",
        "units": "dollars",
        "description": (
            "Absolute dollar amount of cumulative realized losses on the "
            "collateral pool from closing through the current period."
        ),
        "example_phrases": [
            "cumulative losses exceed $10 million",
            "total realized loss above 5MM",
        ],
        "example_expression": "cumulative_losses > 10_000_000",
    },
    {
        "name": "delinquency_60plus_pct",
        "type": "float",
        "units": "decimal fraction (0.05 = 5%)",
        "description": (
            "6-month rolling average of loans 60+ days delinquent, expressed "
            "as a fraction of (pool balance − Class A-1 principal). Matches "
            "the standard indenture Delinquency Trigger definition."
        ),
        "example_phrases": [
            "6-month rolling 60+ day delinquency rate exceeds 5%",
            "average 60+ DPD over the last six months above 4.5%",
            "trailing 6 month 60+ delinquency above 5 percent",
        ],
        "example_expression": "delinquency_60plus_pct > 0.05",
    },
    {
        "name": "pool_balance",
        "type": "float",
        "units": "dollars",
        "description": (
            "Total beginning collateral pool balance for the current period "
            "(sum of all loan-level beginning principal balances)."
        ),
        "example_phrases": [
            "pool balance falls below 10% of original",
            "remaining pool below $50 million",
        ],
        "example_expression": "pool_balance < 50_000_000",
    },
]


KNOWN_ACTION_FLAGS: List[Dict[str, str]] = [
    {
        "name": "CREDIT_SUPPORT_DEPLETION",
        "description": (
            "Use when the trigger fires because subordinate principal is "
            "exhausted. Causes the principal waterfall to flip fully sequential."
        ),
    },
    {
        "name": "CUMULATIVE_LOSS_TRIGGER",
        "description": (
            "Use when the trigger is based on a cumulative loss percentage or "
            "absolute loss threshold."
        ),
    },
    {
        "name": "DELINQUENCY_TRIGGER",
        "description": (
            "Use when the trigger is based on a delinquency rate (60+ DPD, "
            "rolling average, or similar)."
        ),
    },
]


ALLOWED_VARIABLE_NAMES = {v["name"] for v in TRIGGER_VARIABLES}


def variable_catalog_for_prompt() -> str:
    """Render the catalog as compact text for inclusion in an LLM prompt."""
    lines: List[str] = []
    for v in TRIGGER_VARIABLES:
        phrases = " / ".join(f'"{p}"' for p in v["example_phrases"])
        lines.append(
            f"- {v['name']} ({v['type']}, {v['units']})\n"
            f"    {v['description']}\n"
            f"    Common phrasings: {phrases}\n"
            f"    Example: {v['example_expression']}"
        )
    return "\n".join(lines)


def action_catalog_for_prompt() -> str:
    lines = [f"- {a['name']}: {a['description']}" for a in KNOWN_ACTION_FLAGS]
    return "\n".join(lines)
