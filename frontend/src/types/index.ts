export interface CertificateClass {
  class_name: string;
  cusip?: string;
  type: string;
  sub_type?: string;
  is_notional: boolean;
  is_exchangeable: boolean;
  is_residual: boolean;
  initial_principal: number;
  interest_rate_type: string;
  fixed_rate?: number;
  margin?: number;
  benchmark?: string;
  benchmark_tenor?: string;
  rate_cap?: number;
  rate_floor: number;
  accrual_convention: string;
  interest_priority: number;
  principal_priority: number;
  principal_method: string;
  expected_final_date?: string;
  final_scheduled_date?: string;
  fitch_rating?: string;
  kbra_rating?: string;
  moodys_rating?: string;
  sp_rating?: string;
}

export interface FeeConfig {
  fee_name: string;
  fee_rate?: number | null;
  fixed_amount?: number | null;
  fee_type: "percentage" | "fixed" | string;
  priority: number;
  fee_cap?: number | null;
  applies_to: string;
  servicer_name?: string | null;
  category?: "fee" | "expense";
  paid_from?: string;
  payee?: string | null;
  accrues?: boolean;
  shortfall_carried?: boolean;
}

export interface ReserveAccount {
  account_name: string;
  account_type?: string;
  initial_balance: number;
  target_amount?: number | null;
  target_formula?: string | null;
  funded_from?: string;
  released_to?: string;
  release_condition?: string | null;
  release_formula?: string | null;
  floor?: number;
  draws_allowed?: boolean;
  draw_priority?: number;
}

export interface WaterfallStep {
  step: number;
  description: string;
  class_name?: string;
  payment_type: string;
  source_bucket: string;
  condition?: string;
  amount_formula?: string;
}

export interface ServicerConfig {
  servicer_name: string;
  servicing_fee_rate: number;
  advance_obligation: boolean;
  portfolio_pct?: number;
}

export interface DealConfig {
  deal_id: string;
  deal_name: string;
  issuing_entity: string;
  series?: string;
  depositor?: string;
  sponsors?: string[];
  servicers: ServicerConfig[];
  closing_date?: string;
  cut_off_date?: string;
  first_payment_date?: string;
  legal_maturity_date?: string;
  pricing_date?: string;
  asset_class: string;
  asset_type: string;
  payment_frequency: string;
  original_pool_balance: number;
  lien_position?: string;
  benchmark?: string;
  benchmark_tenor?: string;
  interest_day_count?: string;
  default_sofr_rate?: number;
  cleanup_call_pct: number;
  classes: CertificateClass[];
  fees: FeeConfig[];
  reserve_accounts?: ReserveAccount[];
  interest_waterfall: WaterfallStep[];
  principal_waterfall: WaterfallStep[];
  excess_cashflow_waterfall: WaterfallStep[];
  triggers?: TriggerTest[];
  loss_allocation_order?: string[];
  rating_agencies?: string[];
  revolving_period?: boolean;
  revolving_period_end_date?: string;
  notes?: string;
  custodian?: string;
  securities_administrator?: string;
  owner_trustee?: string;
  manually_verified: boolean;
  extraction_source: string;
  extraction_confidence?: number;
  created_at?: string;
  updated_at?: string;
  verified_by?: string;
  verified_at?: string;
  section_page_map?: Record<string, number[]>;
}

export interface TriggerTest {
  test_name: string;
  test_type: string;
  description: string;
  trigger_condition?: string;
  trigger_action?: string;
}

// Matches actual backend ClassPaymentSummary fields
export interface ClassPaymentSummary {
  class_name: string;
  cusip?: string;
  class_type: string;
  original_principal: number;
  beginning_principal: number;
  interest_rate: number;
  benchmark_rate: number;
  accrual_start?: string;
  accrual_end?: string;
  days_accrued?: number;
  interest_accrued: number;
  total_interest_due: number;
  interest_paid: number;
  ending_interest_carryforward: number;   // shortfall
  beginning_interest_carryforward: number;
  // Cap carryover
  beginning_cap_carryover?: number;
  current_cap_carryover?: number;
  total_cap_carryover?: number;
  cap_carryover_paid?: number;
  ending_cap_carryover?: number;
  principal_paid: number;
  writedown_amount?: number;
  writeup_amount?: number;
  cumulative_writedown?: number;
  realized_loss: number;
  cumulative_realized_loss: number;
  ending_principal: number;
  factor_beginning: number;
  factor_interest?: number;
  factor_principal?: number;
  factor_total?: number;
  factor_ending: number;
  total_paid: number;
  // Life-to-date cumulative totals (Section 1(f))
  cumulative_interest_paid?: number;
  cumulative_principal_paid?: number;
  cumulative_total_distribution?: number;
  cumulative_deferred_interest?: number;
  record_date?: string;
}

// Matches actual backend WaterfallTraceStep
export interface WaterfallTraceStep {
  step: number;
  description: string;
  source_bucket?: string;
  funds_available: number;
  amount_owed: number;
  amount_paid: number;
  funds_remaining: number;
  class_name?: string;
  payment_type?: string;
}

// Matches backend StructuralFeatures
export interface StructuralFeatures {
  gross_wac: number;
  net_wac: number;
  wac_cap: number;
  original_credit_support: Record<string, number>;
  current_credit_support: Record<string, number>;
  non_performing_loan_pct: number;
  charged_off_loan_pct: number;
  beginning_upb_by_servicer: Record<string, number>;
  ending_upb_by_servicer: Record<string, number>;
  sofr_fixing: number;
  severely_delinquent_balance: number;
  gross_expected_interest: number;
  net_expected_interest: number;
}

export interface CollateralRealizedLossEntry {
  realized_loss_current: number;
  realized_loss_cumulative: number;
  loans_liquidated_current: number;
  loans_liquidated_cumulative: number;
  net_liquidation_proceeds_current: number;
  net_liquidation_proceeds_cumulative: number;
}

export interface DelinquencyMatrixCell {
  dpd_bucket: string;
  disposition: string;
  amount: number;
  count: number;
}

export interface DelinquencyMatrix {
  rows: string[];
  columns: string[];
  cells: DelinquencyMatrixCell[];
  row_totals: Record<string, number>;
  col_totals: Record<string, number>;
}

export interface TriggerChip {
  name: string;
  status: "green" | "amber" | "red" | string;
  current_value: number;
  threshold: number;
  margin_pct: number;
  description: string;
}

export interface DashboardKPI {
  label: string;
  value: string;
  raw: number;
}

export interface DistributionAllocation {
  bucket: string;
  amount: number;
}

export interface AccountEntry {
  account_name: string;
  beginning_balance: number;
  deposits: number;
  withdrawals: number;
  ending_balance_pre_payment?: number;
  ending_balance_post_payment: number;
  required_balance?: number | null;
}

export interface ExpenseEntry {
  expense_name: string;
  beginning_shortfall: number;
  current_due: number;
  total_due: number;
  amount_paid: number;
  ending_shortfall: number;
  remaining_cap?: number | null;
}

export interface FeeEntry {
  fee_name: string;
  beginning_shortfall: number;
  current_due: number;
  total_due: number;
  amount_paid: number;
  ending_shortfall: number;
}

export interface CollateralRates {
  cdr_1m: number;
  cdr_3m: number;
  cdr_inception: number;
  cpr_1m: number;
  cpr_3m: number;
  cpr_inception: number;
  smm_prepay: number;
  smm_default: number;
}

export interface CollateralPerformanceHistory {
  date: string;
  beginning_balance: number;
  new_defaults: number;
  smm_prepay: number;
  cpr_1m: number;
  cpr_3m: number;
  cpr_inception: number;
  smm_default: number;
  cdr_1m: number;
  cdr_3m: number;
  cdr_inception: number;
  scheduled_principal: number;
  unscheduled_principal: number;
}

export interface LoanDetailRow {
  loan_id: string;
  beginning_principal?: number;
  ending_principal?: number;
  status?: string;
  days_delinquent?: number;
  deferred_amount?: number;
  cumulative_deferred?: number;
  realized_loss?: number;
}

export interface EventTest {
  test_name: string;
  current_value: number;
  operator: string;
  threshold: number;
  status: string;
  description?: string;
}

export interface ServicerBalance {
  servicer_name: string;
  beginning_upb: number;
  ending_upb: number;
  servicing_fee: number;
  loan_count: number;
}

// Matches actual backend WaterfallResult fields
export interface WaterfallResult {
  deal_id: string;
  deal_name: string;
  payment_date: string;
  distribution_date?: string;
  record_date?: string;
  accrual_start_date: string;
  accrual_end_date: string;
  days_accrued: number;
  net_wac: number;
  gross_wac: number;
  benchmark: string;
  benchmark_rate: number;

  // Pool / collateral
  current_pool_balance: number;
  prior_pool_balance: number;
  original_pool_balance: number;
  current_loan_count: number;
  purchases?: number;
  funded_draws?: number;
  capitalized_amounts?: number;
  scheduled_principal_collateral?: number;
  repurchases?: number;
  sales?: number;
  liquidations?: number;
  realized_losses_collateral?: number;
  other_collateral?: number;
  cdr?: number | null;
  cpr?: number | null;

  // Collections
  gross_interest_collected: number;
  servicing_fees_paid: number;
  deal_fees_paid?: number;
  deal_expenses_paid?: number;
  other_collections?: number;
  interest_remittance_amount: number;
  principal_scheduled?: number;
  principal_curtailments?: number;
  principal_prepayments_full?: number;
  principal_sales?: number;
  principal_liquidations?: number;
  principal_repurchases?: number;
  principal_recoveries?: number;
  principal_other?: number;
  principal_remittance_amount: number;
  available_funds: number;
  monthly_excess_cashflow: number;
  total_fees: number;
  total_expenses?: number;
  curtailments: number;
  prepayments_in_full: number;
  charge_offs: number;

  // Totals
  total_interest_paid: number;
  total_principal_paid: number;
  total_beginning_principal: number;
  total_ending_principal: number;
  total_original_principal?: number;
  total_paid?: number;

  // Classes
  class_details: ClassPaymentSummary[];

  // Waterfall traces
  waterfall_trace_interest?: WaterfallTraceStep[] | null;
  waterfall_trace_principal?: WaterfallTraceStep[] | null;
  waterfall_trace_excess?: WaterfallTraceStep[] | null;
  waterfall_trace_principal_no_trigger?: WaterfallTraceStep[];
  waterfall_trace_principal_with_trigger?: WaterfallTraceStep[];
  active_trigger_branch?: string;

  // Performance buckets (delinquency breakdown)
  performance_buckets?: Array<{
    bucket: string;
    amount: number;
    count: number;
    pct_amount: number;
    pct_count: number;
  }>;

  // Fees / expenses
  fees_detail?: FeeEntry[];
  expenses_detail?: ExpenseEntry[];

  // Reserve accounts
  reserve_accounts?: AccountEntry[];

  // Events / triggers / servicer
  events?: EventTest[];
  servicer_balances?: ServicerBalance[];

  // CDR/CPR history
  collateral_rates?: CollateralRates;
  performance_history?: CollateralPerformanceHistory[];

  // Cumulative trackers
  cumulative_interest_paid?: number;
  cumulative_principal_paid?: number;
  cumulative_realized_losses?: number;
  cumulative_prepayments?: number;
  cumulative_defaults?: number;
  cumulative_loans_liquidated?: number;
  cumulative_net_liquidation_proceeds?: number;

  // NEW report sections
  structural_features?: StructuralFeatures;
  collateral_realized_loss?: CollateralRealizedLossEntry;
  delinquency_matrix?: DelinquencyMatrix;

  // Dashboard
  dashboard_kpis?: DashboardKPI[];
  trigger_chips?: TriggerChip[];
  distribution_allocation?: DistributionAllocation[];

  // Loan details
  loans_paid_in_full?: LoanDetailRow[];
  loans_reo?: LoanDetailRow[];
  loans_foreclosure?: LoanDetailRow[];
  loans_bankruptcy?: LoanDetailRow[];
  loans_forbearance?: LoanDetailRow[];
  loans_realized_loss?: LoanDetailRow[];
  loans_modified?: LoanDetailRow[];
}

export interface LoanTapeSummary {
  deal_id: string;
  payment_date: string;
  loan_count: number;
  pool_balance: { beginning: number; ending: number };
  rates: { wac: number; avg_rate: number };
  delinquency: Record<string, number>;
  collections: { interest: number; total_principal: number; curtailments: number; pif: number; servicing_fees: number };
  charge_offs: number;
}
