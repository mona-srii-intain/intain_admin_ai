"""
Pydantic models for Deal Configuration.
Captures all fields needed for ABS/MBS waterfall computation from the deal indenture.
"""
from __future__ import annotations
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field, field_validator
from datetime import date, datetime


class CertificateClass(BaseModel):
    """Represents a single class/tranche of certificates."""
    class_name: str = Field(..., description="Class name (e.g., A-1, M-1, B-3)")
    cusip: Optional[str] = Field(None, description="CUSIP identifier")
    isin: Optional[str] = Field(None, description="ISIN identifier")

    # Type classification
    type: str = Field(..., description="Class type: Senior, Mezzanine, Subordinate, IO, Exchangeable, Residual")
    sub_type: Optional[str] = Field(None, description="Sub-type: Sequential, Pro Rata, Floater, Fixed, Principal Only, Excess Cashflow")
    is_notional: bool = Field(False, description="Whether this is a notional (IO) class")
    is_exchangeable: bool = Field(False, description="Whether this class can be exchanged")
    exchange_group: Optional[str] = Field(None, description="Exchange group name (e.g., BX)")
    is_residual: bool = Field(False, description="REMIC residual class")

    # Principal amounts
    initial_principal: float = Field(..., description="Initial class principal or notional amount")
    current_principal: Optional[float] = Field(None, description="Current outstanding balance (populated at runtime)")

    # Interest rate config
    interest_rate_type: str = Field("fixed", description="fixed, floating, principal_only, excess_cashflow, io")
    fixed_rate: Optional[float] = Field(None, description="Fixed annual pass-through rate (decimal, e.g. 0.0681)")
    margin: Optional[float] = Field(None, description="Spread over benchmark for floating rate (decimal, e.g. 0.0175)")
    benchmark: Optional[str] = Field(None, description="Rate benchmark: SOFR, LIBOR, Term_SOFR")
    benchmark_tenor: Optional[str] = Field(None, description="Benchmark tenor: 1M, 3M")
    rate_cap: Optional[float] = Field(None, description="Maximum pass-through rate (Net WAC cap)")
    rate_floor: float = Field(0.0, description="Minimum pass-through rate (usually 0%)")

    # Interest accrual
    accrual_convention: str = Field("30/360", description="Interest accrual: actual/360, 30/360, actual/365")
    accrual_start_day: int = Field(20, description="Day of month accrual starts (20th or 1st)")

    # Dates
    expected_final_date: Optional[str] = Field(None, description="Expected final distribution date (YYYY-MM)")
    final_scheduled_date: Optional[str] = Field(None, description="Legal final distribution date (YYYY-MM)")

    # Payment priority
    interest_priority: int = Field(0, description="Order in interest waterfall (1=highest)")
    principal_priority: int = Field(0, description="Order in principal waterfall (1=highest)")

    # Principal distribution method
    principal_method: str = Field("sequential", description="sequential or pro_rata")

    # Ratings
    fitch_rating: Optional[str] = Field(None)
    moodys_rating: Optional[str] = Field(None)
    sp_rating: Optional[str] = Field(None)
    kbra_rating: Optional[str] = Field(None)

    # Minimum denominations
    min_denomination_144a: Optional[float] = Field(None)
    min_denomination_reg_s: Optional[float] = Field(None)


class FeeConfig(BaseModel):
    """Represents a transaction fee or expense."""
    fee_name: str = Field(..., description="Name of the fee (Servicing Fee, Custodian Fee, etc.)")
    fee_rate: Optional[float] = Field(None, description="Annual rate as decimal (e.g. 0.0025 for 25bps)")
    fixed_amount: Optional[float] = Field(None, description="Fixed dollar amount per period")
    fee_type: str = Field("percentage", description="percentage or fixed")
    priority: int = Field(1, description="Payment priority (1=first)")
    fee_cap: Optional[float] = Field(None, description="Annual cap on fee")
    applies_to: str = Field("pool_balance", description="pool_balance, class_balance, or specific class name")
    servicer_name: Optional[str] = Field(None, description="Servicer name if servicer-specific fee")
    category: str = Field("fee", description="'fee' or 'expense'")
    paid_from: str = Field("interest_remittance", description="Which bucket pays this: 'interest_remittance', 'principal_remittance', 'available_funds', 'excess_cashflow'")
    payee: Optional[str] = Field(None, description="Who receives this payment: e.g. 'U.S. Bank', 'KBRA'")
    accrues: bool = Field(True, description="Whether this accrues monthly or is paid on demand")
    shortfall_carried: bool = Field(True, description="Whether unpaid amounts carry forward to next period")


class WaterfallStep(BaseModel):
    """A single step in the priority of payments waterfall."""
    step: int = Field(..., description="Step number (1=first priority)")
    description: str = Field(..., description="Description of this waterfall step")
    class_name: Optional[str] = Field(None, description="Target certificate class")
    payment_type: str = Field(..., description="interest, principal, reserve, excess, fee, expense")
    source_bucket: str = Field("available_funds", description="Source: available_funds, interest_remittance, principal_remittance, excess_cashflow, reserve")
    condition: Optional[str] = Field(None, description="Condition for this step (e.g., trigger_failure, always)")
    amount_formula: Optional[str] = Field(None, description="Formula for computing amount")
    concurrent_with: Optional[List[str]] = Field(None, description="Steps that run concurrently (pro-rata)")
    reserve_account: Optional[str] = Field(None, description="Reserve account name if depositing")


class TriggerTest(BaseModel):
    """Credit enhancement trigger tests."""
    test_name: str = Field(..., description="Name of the test")
    test_type: str = Field(..., description="oc, ce, cleanup_call, other")
    description: str = Field("")
    threshold: Optional[float] = Field(None, description="Threshold value (legacy; prefer trigger_condition)")
    operator: str = Field("greater_than", description="greater_than, less_than, equal")
    numerator_components: List[str] = Field(default_factory=list)
    denominator_components: List[str] = Field(default_factory=list)
    trigger_condition: Optional[str] = Field(
        None,
        description=(
            "Python boolean expression (condition of the if-statement). "
            "Available variables: subordinate_balance, cumulative_loss_pct, cumulative_losses, "
            "delinquency_60plus_pct, pool_balance. "
            "Example: subordinate_balance == 0"
        ),
    )
    trigger_action: Optional[str] = Field(
        None,
        description=(
            "Action flag name set to True when condition fires. "
            "Known names: CREDIT_SUPPORT_DEPLETION, CUMULATIVE_LOSS_TRIGGER, DELINQUENCY_TRIGGER. "
            "Example: CREDIT_SUPPORT_DEPLETION"
        ),
    )


class ReserveAccount(BaseModel):
    """Reserve or cash account in the deal structure."""
    account_name: str = Field(..., description="e.g. 'Reserve Fund', 'Pre-Funding Account', 'Capitalized Interest Account'")
    account_type: str = Field("reserve", description="'reserve', 'prefunding', 'capitalized_interest', 'liquidity', 'spread', 'collection'")
    initial_balance: float = Field(0.0, description="Cash deposited at closing")
    target_amount: Optional[float] = Field(None, description="Required funded amount (floor). If None = fully funded.")
    target_formula: Optional[str] = Field(None, description="Python expression for dynamic target: e.g. 'total_beginning_balance * 0.01'")
    funded_from: str = Field("excess_cashflow", description="'excess_cashflow', 'available_funds', 'principal_remittance'")
    released_to: str = Field("available_funds", description="Where released funds go when trigger is met or account is released")
    release_condition: Optional[str] = Field(None, description="When to release: e.g. 'cleanup_call', 'always', 'trigger_failure'")
    release_formula: Optional[str] = Field(None, description="Python expression for release amount: e.g. 'max(0, current_balance - target)'")
    floor: float = Field(0.0, description="Minimum balance that cannot be released")
    draws_allowed: bool = Field(True, description="Whether the account can be drawn on to cover shortfalls")
    draw_priority: int = Field(99, description="Priority at which draws occur in the waterfall (lower = earlier)")


class ServicerConfig(BaseModel):
    """Servicer details."""
    servicer_name: str
    servicing_fee_rate: float = Field(0.0025, description="Annual servicing fee rate")
    advance_obligation: bool = Field(False, description="Whether servicer makes P&I advances")
    portfolio_pct: Optional[float] = Field(None, description="Percentage of pool serviced")


class DealConfig(BaseModel):
    """
    Complete deal configuration extracted from the deal indenture.
    This is the master config used to drive waterfall computations.
    """
    # --- Identification ---
    deal_id: str = Field(..., description="Unique deal identifier")
    deal_name: str = Field(..., description="Full deal name")
    issuing_entity: str = Field(..., description="Legal name of the trust/issuing entity")
    cusip_base: Optional[str] = Field(None, description="Base CUSIP for the deal")
    series: Optional[str] = Field(None, description="Series (e.g., 2023-HE1)")

    # --- Parties ---
    depositor: Optional[str] = Field(None)
    sponsors: Optional[List[str]] = Field(default_factory=list)
    servicers: List[ServicerConfig] = Field(default_factory=list)
    originators: Optional[List[str]] = Field(default_factory=list)
    custodian: Optional[str] = Field(None)
    securities_administrator: Optional[str] = Field(None)
    owner_trustee: Optional[str] = Field(None)
    underwriters: Optional[List[str]] = Field(default_factory=list)
    rating_agencies: Optional[List[str]] = Field(default_factory=list)

    # --- Key Dates ---
    closing_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    cut_off_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    first_payment_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    legal_maturity_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    pricing_date: Optional[str] = Field(None, description="YYYY-MM-DD")
    record_date_offset_days: int = Field(2, description="Business days before payment date")
    determination_date_offset_days: int = Field(5, description="Business days before payment date")

    # --- Collateral Characteristics ---
    asset_class: str = Field("Residential Real Estate", description="Asset class")
    asset_type: str = Field("Mortgage", description="HELOC, Mortgage, Auto, Student, CLO, etc.")
    payment_frequency: str = Field("Monthly", description="Monthly, Quarterly, Semi-Annual")
    original_pool_balance: float = Field(0.0, description="Original aggregate pool balance")
    lien_position: Optional[str] = Field(None, description="First Lien, Second Lien")
    revolving_period: bool = Field(False, description="Does the deal have a revolving period?")
    revolving_period_end_date: Optional[str] = Field(None, description="End of revolving period")

    # --- Interest Rate Config ---
    benchmark: Optional[str] = Field("SOFR", description="Rate benchmark for floating rate classes")
    benchmark_tenor: Optional[str] = Field("1M", description="1M, 3M")
    default_sofr_rate: Optional[float] = Field(None, description="Default SOFR rate if not provided at runtime")
    interest_day_count: Optional[str] = Field("actual/360", description="Pool-level day count convention")

    @field_validator("benchmark", mode="before")
    @classmethod
    def default_benchmark(cls, v: Optional[str]) -> str:
        return v if v is not None else "SOFR"

    @field_validator("benchmark_tenor", mode="before")
    @classmethod
    def default_benchmark_tenor(cls, v: Optional[str]) -> str:
        return v if v is not None else "1M"

    @field_validator("interest_day_count", mode="before")
    @classmethod
    def default_interest_day_count(cls, v: Optional[str]) -> str:
        return v if v is not None else "actual/360"

    # --- Certificate Classes ---
    classes: List[CertificateClass] = Field(default_factory=list, description="All certificate classes")

    # --- Fees and Expenses ---
    fees: List[FeeConfig] = Field(default_factory=list, description="Transaction fees")
    annual_expense_cap: Optional[float] = Field(None, description="Cap on annual deal expenses")

    # --- Waterfall Rules ---
    interest_waterfall: List[WaterfallStep] = Field(
        default_factory=list,
        description="Ordered steps for distributing interest remittance"
    )
    principal_waterfall: List[WaterfallStep] = Field(
        default_factory=list,
        description="Ordered steps for distributing principal remittance"
    )
    excess_cashflow_waterfall: List[WaterfallStep] = Field(
        default_factory=list,
        description="Ordered steps for monthly excess cashflow"
    )

    # --- Reserve Accounts ---
    reserve_accounts: List[ReserveAccount] = Field(default_factory=list)

    # --- Triggers / Tests ---
    triggers: List[TriggerTest] = Field(default_factory=list)
    cleanup_call_pct: float = Field(0.10, description="Optional redemption trigger (% of original balance)")

    # --- Loss Allocation ---
    loss_allocation_order: List[str] = Field(
        default_factory=list,
        description="Order of classes that absorb losses (first to last, most subordinate first)"
    )

    # --- Additional Metadata ---
    notes: Optional[str] = Field(None, description="Additional notes or rules")
    extraction_source: str = Field("manual", description="manual or llm_extracted")
    extraction_confidence: Optional[float] = Field(None, description="LLM confidence score 0-1")
    raw_extraction: Optional[Dict[str, Any]] = Field(None, description="Raw LLM extraction output")
    # Maps section keys (deal_info, certificate_classes, fees, waterfall, triggers, etc.)
    # to ranked 1-indexed PDF page numbers, used by the frontend PDF verification panel
    # to jump to the relevant pages when a section is being edited.
    section_page_map: Optional[Dict[str, List[int]]] = Field(
        default_factory=dict,
        description="Top relevant PDF pages per UI section (1-indexed)",
    )
    manually_verified: bool = Field(False, description="Whether maker-checker has verified")
    verified_by: Optional[str] = Field(None)
    verified_at: Optional[str] = Field(None)
    created_at: Optional[str] = Field(None)
    updated_at: Optional[str] = Field(None)


class DealSummary(BaseModel):
    """Lightweight deal summary for listing."""
    deal_id: str
    deal_name: str
    asset_class: str
    asset_type: str
    original_pool_balance: float
    closing_date: Optional[str]
    first_payment_date: Optional[str]
    class_count: int
    manually_verified: bool
    created_at: Optional[str]


class DealExtractionRequest(BaseModel):
    """Request to save/confirm extracted deal fields."""
    deal_id: str = Field(..., description="Deal ID to assign")
    deal_config: DealConfig = Field(..., description="Extracted/confirmed deal configuration")


class FieldCorrection(BaseModel):
    """A single field correction made during maker-checker review."""
    field_path: str = Field(..., description="JSON path to the field (e.g., classes[0].margin)")
    original_value: Any = Field(None, description="Original extracted value")
    corrected_value: Any = Field(None, description="Corrected value")
    reason: Optional[str] = Field(None, description="Reason for correction")


class MakerCheckerReview(BaseModel):
    """Maker-checker review submission."""
    deal_id: str
    reviewed_config: DealConfig
    corrections: List[FieldCorrection] = Field(default_factory=list)
    reviewer_name: Optional[str] = None
    notes: Optional[str] = None
