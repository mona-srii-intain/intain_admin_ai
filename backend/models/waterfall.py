"""
Pydantic models for Waterfall Computation Results and Reports.
"""
from __future__ import annotations
from typing import Optional, List, Dict, Any
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Input models
# ---------------------------------------------------------------------------

class WaterfallComputeRequest(BaseModel):
    """Request body to trigger waterfall computation."""
    deal_id: str = Field(..., description="Deal ID (must have saved config)")
    payment_date: str = Field(..., description="Payment date YYYY-MM-DD")
    sofr_rate: Optional[float] = Field(None, description="1-Month SOFR rate (decimal). Uses deal default if None.")
    override_beginning_balances: Optional[Dict[str, float]] = Field(
        None, description="Optional override for beginning class balances {class_name: balance}"
    )
    accrual_start_date: Optional[str] = Field(None, description="Override accrual start date YYYY-MM-DD")
    accrual_end_date: Optional[str] = Field(None, description="Override accrual end date YYYY-MM-DD")
    notes: Optional[str] = Field(None)


# ---------------------------------------------------------------------------
# Result sub-models
# ---------------------------------------------------------------------------

class ClassPaymentSummary(BaseModel):
    """Payment summary for one certificate class."""
    class_name: str
    cusip: Optional[str] = None
    class_type: str
    original_principal: float = 0.0
    beginning_principal: float = 0.0

    # Interest
    interest_rate: float = 0.0             # effective pass-through rate used this period
    benchmark_rate: float = 0.0            # SOFR/LIBOR used
    accrual_start: str = ""
    accrual_end: str = ""
    days_accrued: int = 30
    beginning_interest_carryforward: float = 0.0
    interest_accrued: float = 0.0          # interest due this period
    total_interest_due: float = 0.0        # carryforward + accrued
    interest_paid: float = 0.0
    ending_interest_carryforward: float = 0.0

    # Cap carryover (for floating rate classes)
    beginning_cap_carryover: float = 0.0
    current_cap_carryover: float = 0.0
    total_cap_carryover: float = 0.0
    cap_carryover_paid: float = 0.0
    ending_cap_carryover: float = 0.0

    # Principal
    principal_paid: float = 0.0
    writedown_amount: float = 0.0
    writeup_amount: float = 0.0
    cumulative_writedown: float = 0.0
    realized_loss: float = 0.0
    cumulative_realized_loss: float = 0.0
    ending_principal: float = 0.0

    # Life-to-date cumulative totals (Section 1(f) Cumulative Payment Detail).
    # Each = prior-period value + this-period addition; first period equals current.
    cumulative_interest_paid: float = 0.0
    cumulative_principal_paid: float = 0.0
    cumulative_total_distribution: float = 0.0
    cumulative_deferred_interest: float = 0.0

    # Totals
    total_paid: float = 0.0               # interest + principal paid

    # Factors (per $1000 original denomination)
    factor_beginning: float = 0.0
    factor_interest: float = 0.0
    factor_principal: float = 0.0
    factor_total: float = 0.0
    factor_ending: float = 0.0
    record_date: str = ""


class CollateralBucket(BaseModel):
    """Delinquency or status bucket for collateral performance."""
    bucket: str                        # "Current", "30-59 Days", etc.
    amount: float = 0.0
    count: int = 0
    pct_amount: float = 0.0
    pct_count: float = 0.0


class CollateralPerformanceHistory(BaseModel):
    """CDR/CPR history row."""
    date: str
    beginning_balance: float = 0.0
    new_defaults: float = 0.0
    smm_prepay: float = 0.0
    cpr_1m: float = 0.0
    cpr_3m: float = 0.0
    cpr_6m: float = 0.0
    cpr_12m: float = 0.0
    cpr_inception: float = 0.0
    smm_default: float = 0.0
    cdr_1m: float = 0.0
    cdr_3m: float = 0.0
    cdr_6m: float = 0.0
    cdr_12m: float = 0.0
    cdr_inception: float = 0.0
    scheduled_principal: float = 0.0
    unscheduled_principal: float = 0.0


class CollateralRates(BaseModel):
    """Collateral default and prepayment rates."""
    cdr_1m: float = 0.0
    cdr_3m: float = 0.0
    cdr_inception: float = 0.0
    cpr_1m: float = 0.0
    cpr_3m: float = 0.0
    cpr_inception: float = 0.0
    smm_prepay: float = 0.0
    smm_default: float = 0.0


class AccountEntry(BaseModel):
    """Account balance entry."""
    account_name: str
    beginning_balance: float = 0.0
    deposits: float = 0.0
    withdrawals: float = 0.0
    ending_balance_pre_payment: float = 0.0
    ending_balance_post_payment: float = 0.0
    required_balance: Optional[float] = None  # target funding level (Section 3(b))


class FeeEntry(BaseModel):
    """Fee payment detail."""
    fee_name: str
    beginning_shortfall: float = 0.0
    current_due: float = 0.0
    total_due: float = 0.0
    amount_paid: float = 0.0
    ending_shortfall: float = 0.0


class ExpenseEntry(BaseModel):
    """Expense detail."""
    expense_name: str
    beginning_shortfall: float = 0.0
    current_due: float = 0.0
    total_due: float = 0.0
    amount_paid: float = 0.0
    ending_shortfall: float = 0.0
    remaining_cap: Optional[float] = None


class WaterfallTraceStep(BaseModel):
    """A single traced step in the waterfall execution."""
    step: int
    description: str
    source_bucket: str
    funds_available: float
    amount_owed: float
    amount_paid: float
    funds_remaining: float
    class_name: Optional[str] = None
    payment_type: str = ""


class EventTest(BaseModel):
    """Trigger/test event result."""
    test_name: str
    current_value: float
    operator: str
    threshold: float
    status: str             # "Pass" or "Fail"
    description: str = ""


class ServicerBalance(BaseModel):
    """Per-servicer balance tracking."""
    servicer_name: str
    beginning_upb: float = 0.0
    ending_upb: float = 0.0
    servicing_fee: float = 0.0
    loan_count: int = 0


class LoanDetail(BaseModel):
    """Individual loan detail for report sections."""
    loan_id: str
    beginning_principal: float = 0.0
    ending_principal: float = 0.0
    interest_paid: Optional[float] = None
    principal_paid: Optional[float] = None
    status: Optional[str] = None
    days_delinquent: Optional[int] = None
    interest_rate: Optional[float] = None
    deferred_amount: Optional[float] = None
    cumulative_deferred: Optional[float] = None
    realized_loss: Optional[float] = None
    subsequent_recovery: Optional[float] = None
    cumulative_loss: Optional[float] = None
    modification_type: Optional[str] = None


class StructuralFeatures(BaseModel):
    """Section 2(e) — Structural Features.

    Fields sourced from the indenture + this period's pool computation.
    Per-class credit support is keyed by class_name (M-1, M-2, M-3 minimum).
    """
    gross_wac: float = 0.0
    net_wac: float = 0.0
    wac_cap: float = 0.0
    original_credit_support: Dict[str, float] = Field(default_factory=dict)
    current_credit_support: Dict[str, float] = Field(default_factory=dict)
    non_performing_loan_pct: float = 0.0       # 60+ DPD balance / pool
    charged_off_loan_pct: float = 0.0          # cum charge-offs / original pool
    beginning_upb_by_servicer: Dict[str, float] = Field(default_factory=dict)
    ending_upb_by_servicer: Dict[str, float] = Field(default_factory=dict)
    sofr_fixing: float = 0.0
    severely_delinquent_balance: float = 0.0   # 90+ DPD balance
    gross_expected_interest: float = 0.0
    net_expected_interest: float = 0.0


class CollateralRealizedLossEntry(BaseModel):
    """Section 2(d) — Collateral Realized Loss (Current / Cumulative)."""
    realized_loss_current: float = 0.0
    realized_loss_cumulative: float = 0.0
    loans_liquidated_current: int = 0
    loans_liquidated_cumulative: int = 0
    net_liquidation_proceeds_current: float = 0.0
    net_liquidation_proceeds_cumulative: float = 0.0


class DelinquencyMatrixCell(BaseModel):
    """One (DPD bucket × disposition) cell in the 2D matrix."""
    dpd_bucket: str
    disposition: str
    amount: float = 0.0
    count: int = 0


class DelinquencyMatrix(BaseModel):
    """Section 2(b) — 2D matrix of DPD bucket × loan disposition.

    Rows: Current, 1-29, 30-59, 60-89, 90-119, 120-149, 150-179, 180+
    Columns: Delinquent, Foreclosure, Bankruptcy, REO, Forbearance, Total
    """
    rows: List[str] = Field(default_factory=list)
    columns: List[str] = Field(default_factory=list)
    cells: List[DelinquencyMatrixCell] = Field(default_factory=list)
    row_totals: Dict[str, float] = Field(default_factory=dict)
    col_totals: Dict[str, float] = Field(default_factory=dict)


class TriggerChip(BaseModel):
    """Dashboard status chip for one indenture trigger."""
    name: str                       # display name
    status: str                     # "green" | "amber" | "red"
    current_value: float = 0.0
    threshold: float = 0.0
    margin_pct: float = 0.0         # how far below the limit (positive = headroom)
    description: str = ""


class DashboardKPI(BaseModel):
    """One tile on the dashboard KPI strip."""
    label: str
    value: str                      # pre-formatted display string
    raw: float = 0.0                # numeric, for sorting/charts


class DistributionAllocation(BaseModel):
    """Bucketed distribution amount for Section 8 chart."""
    bucket: str                     # "Senior Principal", "Senior Interest", etc.
    amount: float = 0.0


# ---------------------------------------------------------------------------
# Main waterfall result model
# ---------------------------------------------------------------------------

class WaterfallResult(BaseModel):
    """
    Complete waterfall computation result for one payment date.
    This is both stored as JSON and used to generate investor reports.
    """
    # Identity
    deal_id: str
    deal_name: str
    payment_date: str
    distribution_date: str
    record_date: str
    accrual_start_date: str
    accrual_end_date: str
    days_accrued: int = 30
    report_type: str = "Monthly Report"
    asset_class: str = ""

    # Benchmark / Rates
    benchmark: str = "SOFR"
    benchmark_rate: float = 0.0          # SOFR rate used
    net_wac: float = 0.0                 # Weighted average coupon net of servicing
    gross_wac: float = 0.0               # Weighted average coupon gross

    # ------ SECTION 1: PAYMENTS SUMMARY ------
    class_details: List[ClassPaymentSummary] = Field(default_factory=list)
    total_original_principal: float = 0.0
    total_beginning_principal: float = 0.0
    total_interest_paid: float = 0.0
    total_principal_paid: float = 0.0
    total_paid: float = 0.0
    total_ending_principal: float = 0.0

    # ------ SECTION 2: COLLATERAL SUMMARY ------
    original_pool_balance: float = 0.0
    prior_pool_balance: float = 0.0
    purchases: float = 0.0
    funded_draws: float = 0.0
    capitalized_amounts: float = 0.0
    scheduled_principal_collateral: float = 0.0
    curtailments: float = 0.0
    prepayments_in_full: float = 0.0
    repurchases: float = 0.0
    charge_offs: float = 0.0
    sales: float = 0.0
    liquidations: float = 0.0
    realized_losses_collateral: float = 0.0
    other_collateral: float = 0.0
    current_pool_balance: float = 0.0
    current_loan_count: int = 0

    # Collateral performance (delinquency)
    performance_buckets: List[CollateralBucket] = Field(default_factory=list)

    # CDR/CPR rates
    collateral_rates: CollateralRates = Field(default_factory=CollateralRates)
    performance_history: List[CollateralPerformanceHistory] = Field(default_factory=list)

    # ------ SECTION 3: ACCOUNTS ------
    # Collections
    gross_interest_collected: float = 0.0
    servicing_fees_paid: float = 0.0
    deal_fees_paid: float = 0.0
    deal_expenses_paid: float = 0.0
    other_collections: float = 0.0
    interest_remittance_amount: float = 0.0

    principal_scheduled: float = 0.0
    principal_curtailments: float = 0.0
    principal_prepayments_full: float = 0.0
    principal_sales: float = 0.0
    principal_liquidations: float = 0.0
    principal_repurchases: float = 0.0
    principal_recoveries: float = 0.0
    principal_other: float = 0.0
    principal_remittance_amount: float = 0.0
    available_funds: float = 0.0
    monthly_excess_cashflow: float = 0.0

    reserve_accounts: List[AccountEntry] = Field(default_factory=list)

    # ------ SECTION 4: FEES ------
    fees_detail: List[FeeEntry] = Field(default_factory=list)
    total_fees: float = 0.0

    # ------ SECTION 5: EXPENSES ------
    expenses_detail: List[ExpenseEntry] = Field(default_factory=list)
    total_expenses: float = 0.0

    # ------ SECTION 6: EVENTS / TRIGGERS ------
    events: List[EventTest] = Field(default_factory=list)

    # ------ SECTION 7: REPORTING MISC ------
    servicer_balances: List[ServicerBalance] = Field(default_factory=list)
    misc_items: List[Dict[str, Any]] = Field(default_factory=list)

    # ------ SECTION 8: PRIORITY OF PAYMENTS TRACE ------
    waterfall_trace_interest: List[WaterfallTraceStep] = Field(default_factory=list)
    waterfall_trace_principal: List[WaterfallTraceStep] = Field(default_factory=list)
    waterfall_trace_excess: List[WaterfallTraceStep] = Field(default_factory=list)
    # Both principal branches rendered for disclosure. The active branch's
    # steps mirror `waterfall_trace_principal`; the inactive branch shows
    # zeros for `amount_paid` (structure for reference only).
    waterfall_trace_principal_no_trigger: List[WaterfallTraceStep] = Field(default_factory=list)
    waterfall_trace_principal_with_trigger: List[WaterfallTraceStep] = Field(default_factory=list)
    active_trigger_branch: str = "Trigger Not In Effect"  # or "Trigger In Effect"

    # ------ SECTION 9: LOAN DETAILS ------
    loans_paid_in_full: List[LoanDetail] = Field(default_factory=list)
    loans_reo: List[LoanDetail] = Field(default_factory=list)
    loans_foreclosure: List[LoanDetail] = Field(default_factory=list)
    loans_bankruptcy: List[LoanDetail] = Field(default_factory=list)
    loans_forbearance: List[LoanDetail] = Field(default_factory=list)
    loans_realized_loss: List[LoanDetail] = Field(default_factory=list)
    loans_modified: List[LoanDetail] = Field(default_factory=list)

    # Cumulative trackers
    cumulative_interest_paid: float = 0.0
    cumulative_principal_paid: float = 0.0
    cumulative_realized_losses: float = 0.0
    cumulative_prepayments: float = 0.0
    cumulative_defaults: float = 0.0
    cumulative_loans_liquidated: int = 0
    cumulative_net_liquidation_proceeds: float = 0.0

    # ------ NEW SECTIONS (Part A & B of the upgraded report) ------
    structural_features: Optional[StructuralFeatures] = None       # Section 2(e)
    collateral_realized_loss: Optional[CollateralRealizedLossEntry] = None  # Section 2(d)
    delinquency_matrix: Optional[DelinquencyMatrix] = None         # Section 2(b) 2D

    # Dashboard data (consumed by frontend + PDF page 1)
    dashboard_kpis: List[DashboardKPI] = Field(default_factory=list)
    trigger_chips: List[TriggerChip] = Field(default_factory=list)
    # Distribution allocation buckets for Section 8 chart
    distribution_allocation: List[DistributionAllocation] = Field(default_factory=list)

    # Metadata
    computed_at: str = ""
    computation_notes: Optional[str] = None


class WaterfallSummary(BaseModel):
    """Lightweight summary for listing all waterfall computations."""
    deal_id: str
    payment_date: str
    deal_name: str
    current_pool_balance: float
    total_interest_paid: float
    total_principal_paid: float
    current_loan_count: int
    cpr_1m: float
    cdr_1m: float
    computed_at: str


class ReportGenerateRequest(BaseModel):
    """Request to generate investor report."""
    deal_id: str
    payment_date: str
    include_loan_details: bool = True
    include_performance_history: bool = True
    format: str = Field("json", description="json or pdf")
