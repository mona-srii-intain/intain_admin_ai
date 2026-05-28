import { useState, useEffect } from "react";
import {
  Database, PlayCircle, ChevronDown, TrendingUp,
  AlertCircle, ChevronRight, Info,
} from "lucide-react";
import Header from "../components/layout/Header";
import StatCard from "../components/shared/StatCard";
import LoadingSpinner from "../components/shared/LoadingSpinner";
import type { WaterfallResult, WaterfallTraceStep } from "../types";
import { listLoanDeals, getPaymentDates, getLoanSummary, computeWaterfall, getDeal } from "../services/api";
import toast from "react-hot-toast";

const n = (v: unknown): number => (typeof v === "number" && isFinite(v) ? v : 0);
const fmt = (v: unknown, dec = 2) =>
  n(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
const fmtUSD = (v: unknown) => {
  const val = n(v);
  // Full currency display (no M/B compact notation), while preserving
  // meaningful fractional precision from computed values.
  const raw = String(val);
  const frac = raw.includes(".") ? raw.split(".")[1].replace(/0+$/, "").length : 0;
  const maxFrac = Math.min(Math.max(frac, 2), 10);
  return "$" + val.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: maxFrac,
  });
};
const fmtPct = (v: unknown) => `${(n(v) * 100).toFixed(4)}%`;

export default function LoanTapePage() {
  const [deals, setDeals] = useState<string[]>([]);
  const [dates, setDates] = useState<string[]>([]);
  const [selectedDeal, setSelectedDeal] = useState("");
  const [selectedDate, setSelectedDate] = useState("");
  const [sofrRate, setSofrRate] = useState("");
  const [configSofrRate, setConfigSofrRate] = useState<number | null>(null);
  const [summary, setSummary] = useState<Record<string, unknown> | null>(null);
  const [loadingSummary, setLoadingSummary] = useState(false);
  const [computing, setComputing] = useState(false);
  const [result, setResult] = useState<WaterfallResult | null>(null);
  const [activeTab, setActiveTab] = useState<"summary" | "classes" | "trace" | "collateral">("summary");

  // Override beginning balances
  const [showOverride, setShowOverride] = useState(false);
  const [overrideBalances, setOverrideBalances] = useState<Record<string, string>>({});
  const [dealClasses, setDealClasses] = useState<string[]>([]);
  const [configuredPrincipals, setConfiguredPrincipals] = useState<Record<string, number>>({});

  useEffect(() => {
    listLoanDeals()
      .then((d) => setDeals(d.deals ?? []))
      .catch(() => toast.error("Failed to load deals from Snowflake"));
  }, []);

  useEffect(() => {
    if (!selectedDeal) return;
    setDates([]); setSelectedDate(""); setSummary(null); setResult(null);
    getPaymentDates(selectedDeal)
      .then((d) => setDates(d.payment_dates ?? []))
      .catch(() => toast.error("Failed to fetch payment dates"));
    // Load class names + configured principals + default SOFR from deal config
    getDeal(selectedDeal)
      .then((d) => {
        const classes: Array<{ class_name: string; initial_principal: number }> = d.classes ?? [];
        const names = classes.map((c) => c.class_name);
        setDealClasses(names);
        // Store the configured principals so we can show them as placeholders
        const principals: Record<string, number> = {};
        classes.forEach((c) => { if (c.initial_principal > 0) principals[c.class_name] = c.initial_principal; });
        setConfiguredPrincipals(principals);
        // Leave overrideBalances empty — user fills only what they want to override
        const init: Record<string, string> = {};
        names.forEach((nm) => { init[nm] = ""; });
        setOverrideBalances(init);
        // Populate SOFR from deal config if available, else clear to let engine use its default
        const cfgRate: number | null = d.default_sofr_rate ?? null;
        setConfigSofrRate(cfgRate);
        setSofrRate(cfgRate != null ? (cfgRate * 100).toFixed(4) : "");
      })
      .catch(() => {});
  }, [selectedDeal]);

  useEffect(() => {
    if (!selectedDeal || !selectedDate) return;
    setLoadingSummary(true); setSummary(null);
    getLoanSummary(selectedDeal, selectedDate)
      .then((d) => setSummary(d))
      .catch(() => toast.error("Failed to fetch loantape summary"))
      .finally(() => setLoadingSummary(false));
  }, [selectedDeal, selectedDate]);

  const handleRunWaterfall = async () => {
    if (!selectedDeal || !selectedDate) return;
    setComputing(true); setResult(null);
    // Build override_beginning_balances only if any values entered
    const overrides: Record<string, number> = {};
    Object.entries(overrideBalances).forEach(([cls, val]) => {
      const num = parseFloat(val);
      if (!isNaN(num) && num > 0) overrides[cls] = num;
    });

    try {
      const parsedSofr = sofrRate.trim() !== "" ? parseFloat(sofrRate) / 100 : undefined;
      const data = await computeWaterfall({
        deal_id: selectedDeal,
        payment_date: selectedDate,
        ...(parsedSofr !== undefined ? { sofr_rate: parsedSofr } : {}),
        ...(Object.keys(overrides).length > 0 ? { override_beginning_balances: overrides } : {}),
      });
      setResult(data as WaterfallResult);
      setActiveTab("summary");
      toast.success("Waterfall computation complete!");
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Computation failed.";
      toast.error(msg.slice(0, 120));
    } finally {
      setComputing(false);
    }
  };

  // Combined waterfall trace across all buckets
  const allTrace: WaterfallTraceStep[] = [
    ...(result?.waterfall_trace_interest ?? []),
    ...(result?.waterfall_trace_principal ?? []),
    ...(result?.waterfall_trace_excess ?? []),
  ];

  const allBalancesZero = result?.class_details?.every(cd => cd.beginning_principal === 0) ?? false;

  // Safe nested summary helpers
  const poolBal = (summary as Record<string, Record<string, number>> | null)?.pool_balance ?? {};
  const rates   = (summary as Record<string, Record<string, number>> | null)?.rates ?? {};
  const dlq     = (summary as Record<string, Record<string, number>> | null)?.delinquency ?? {};
  const cols    = (summary as Record<string, Record<string, number>> | null)?.collections ?? {};
  const totalBal = n(poolBal.ending);
  const dlq30  = totalBal > 0 ? n(dlq["30_59_days"])  / totalBal : 0;
  const dlq60  = totalBal > 0 ? n(dlq["60_89_days"])  / totalBal : 0;
  const dlq90  = totalBal > 0 ? (n(dlq["90_119_days"]) + n(dlq["120_149_days"]) + n(dlq["150_plus_days"])) / totalBal : 0;

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <Header
        title="Loantape & Waterfall"
        subtitle="Fetch loantape data from Snowflake and run the waterfall computation engine"
      />

      <main className="flex-1 overflow-y-auto p-4 sm:p-6 space-y-6">
        {/* Controls */}
        <div className="card space-y-4">
          <h2 className="section-title">
            <Database size={16} className="text-primary-600" />
            Select Deal & Payment Date
          </h2>

          <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4 items-end">
            <div>
              <label className="label">Deal ID</label>
              <div className="relative">
                <select className="select pr-8" value={selectedDeal} onChange={(e) => setSelectedDeal(e.target.value)}>
                  <option value="">Select deal…</option>
                  {deals.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              </div>
            </div>

            <div>
              <label className="label">Payment Date</label>
              <div className="relative">
                <select className="select pr-8" value={selectedDate} onChange={(e) => setSelectedDate(e.target.value)} disabled={!dates.length}>
                  <option value="">Select date…</option>
                  {dates.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              </div>
            </div>

            <div>
              <label className="label flex items-center gap-1.5">
                1M SOFR Rate (%)
                <span className="text-[10px] font-normal bg-gray-100 text-gray-500 px-1.5 py-0.5 rounded-full">optional</span>
              </label>
              <input
                className="input"
                type="number" step="0.01" min="0" max="20"
                value={sofrRate}
                onChange={(e) => setSofrRate(e.target.value)}
                placeholder={
                  configSofrRate != null
                    ? `Config: ${(configSofrRate * 100).toFixed(4)}%`
                    : "Default: 5.30%"
                }
              />
              <p className="text-[10px] text-gray-400 mt-1">
                {sofrRate.trim() === ""
                  ? configSofrRate != null
                    ? `Using deal config value: ${(configSofrRate * 100).toFixed(4)}%`
                    : "Blank → engine uses 5.30% (no config value found)"
                  : `Override: ${sofrRate}% will be used for this run`}
              </p>
            </div>

            <button className="btn-primary justify-center py-2.5 h-[38px]"
              onClick={handleRunWaterfall} disabled={!selectedDeal || !selectedDate || computing}>
              <PlayCircle size={16} />
              {computing ? "Computing…" : "Run Waterfall"}
            </button>
          </div>

          {/* Override Beginning Balances — optional advanced section */}
          {dealClasses.length > 0 && (
            <div>
              <button
                className="flex items-center gap-2 text-sm text-gray-500 hover:text-primary-700 transition-colors"
                onClick={() => setShowOverride(!showOverride)}
              >
                <ChevronRight size={13} className={`transition-transform ${showOverride ? "rotate-90" : ""}`} />
                <span>Advanced: Override Beginning Class Balances</span>
                <span className="text-xs bg-gray-100 text-gray-500 px-2 py-0.5 rounded-full">optional</span>
              </button>

              {showOverride && (
                <div className="mt-3 p-4 bg-gray-50 border border-gray-200 rounded-xl fade-in">
                  <div className="flex gap-2 mb-4">
                    <Info size={15} className="text-blue-500 flex-shrink-0 mt-0.5" />
                    <div className="text-xs text-gray-600 space-y-1">
                      <p>
                        <strong>You don't need to fill this for normal runs.</strong> The waterfall engine automatically uses the
                        {" "}<strong>initial_principal</strong> values from the saved deal config (shown as placeholders below).
                      </p>
                      <p className="text-gray-400">
                        Only enter a value here if you want to <em>override</em> the deal config balance for a specific class
                        — for example, if you know the actual balance has changed but the config hasn't been updated.
                      </p>
                    </div>
                  </div>
                  <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-3">
                    {dealClasses.map((cls) => {
                      const configured = configuredPrincipals[cls];
                      return (
                        <div key={cls}>
                          <label className="text-xs font-semibold text-gray-600 mb-1 block">
                            Class {cls}
                          </label>
                          {configured ? (
                            <p className="text-[10px] text-green-700 font-mono mb-1">
                              Config: ${(configured / 1_000_000).toFixed(2)}M
                            </p>
                          ) : (
                            <p className="text-[10px] text-amber-600 mb-1">⚠ Not in config</p>
                          )}
                          <input
                            className="input text-sm"
                            type="number"
                            placeholder={configured ? `${configured.toLocaleString()}` : "Enter balance"}
                            value={overrideBalances[cls] ?? ""}
                            onChange={(e) => setOverrideBalances(prev => ({ ...prev, [cls]: e.target.value }))}
                          />
                        </div>
                      );
                    })}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>

        {/* Loantape Summary */}
        {loadingSummary && (
          <div className="card flex items-center justify-center py-10">
            <LoadingSpinner text="Fetching loantape from Snowflake…" />
          </div>
        )}

        {summary && !loadingSummary && (
          <div className="fade-in space-y-4">
            <h3 className="section-title">
              <Database size={15} className="text-primary-600" />
              Loantape Summary — {selectedDeal} · {selectedDate}
            </h3>
            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6 gap-4">
              <StatCard label="Total Loans"    value={fmt(summary.loan_count, 0)} />
              <StatCard label="Pool Balance"   value={fmtUSD(totalBal)} accent />
              <StatCard label="WAC"            value={`${(n(rates.wac) * 100).toFixed(4)}%`} />
              <StatCard label="30–59 Day DQ"   value={`${(dlq30 * 100).toFixed(2)}%`} />
              <StatCard label="60–89 Day DQ"   value={`${(dlq60 * 100).toFixed(2)}%`} />
              <StatCard label="90+ Day DQ"     value={`${(dlq90 * 100).toFixed(2)}%`} />
            </div>
            <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-4">
              <StatCard label="Interest Collections"  value={fmtUSD(cols.interest)} />
              <StatCard label="Principal Collections" value={fmtUSD(cols.total_principal)} />
              <StatCard label="Curtailments"          value={fmtUSD(cols.curtailments)} />
              <StatCard label="Charge-Offs"           value={fmtUSD(summary.charge_offs)} />
            </div>
          </div>
        )}

        {/* Computing */}
        {computing && (
          <div className="card flex flex-col items-center justify-center py-12 gap-4 fade-in">
            <LoadingSpinner size="lg" />
            <div className="text-center">
              <p className="text-sm font-semibold text-gray-700">Running Waterfall Engine…</p>
              <p className="text-xs text-gray-500 mt-1">Processing cashflows, interest accrual, and distribution priorities</p>
            </div>
          </div>
        )}

        {/* Results */}
        {result && !computing && (
          <div className="fade-in space-y-4">

            {/* Warning if all balances are zero */}
            {allBalancesZero && (
              <div className="flex gap-3 p-4 bg-amber-50 border border-amber-200 rounded-xl">
                <AlertCircle size={18} className="text-amber-500 flex-shrink-0 mt-0.5" />
                <div>
                  <p className="text-sm font-semibold text-amber-800">All class balances are zero</p>
                  <p className="text-xs text-amber-700 mt-0.5">
                    The deal configuration has no initial principal values (LLM extraction missed them).
                    Use the <strong>"Override Beginning Class Balances"</strong> section above to enter the correct beginning balances for each class, then re-run the waterfall.
                  </p>
                </div>
              </div>
            )}

            {/* Headline Stats */}
            <h3 className="section-title">
              <TrendingUp size={15} className="text-primary-600" />
              Waterfall Results — {result.payment_date}
            </h3>

            <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 2xl:grid-cols-6 gap-4">
              <StatCard label="Pool Balance"         value={fmtUSD(result.current_pool_balance)} accent />
              <StatCard label="Net WAC"              value={fmtPct(result.net_wac)} />
              <StatCard label="Gross Interest"       value={fmtUSD(result.gross_interest_collected)} />
              <StatCard label="Interest Remittance"  value={fmtUSD(result.interest_remittance_amount)} />
              <StatCard label="Principal Remittance" value={fmtUSD(result.principal_remittance_amount)} />
              <StatCard label="Excess Cashflow"      value={fmtUSD(result.monthly_excess_cashflow)} />
            </div>

            {/* Tabs */}
            <div className="border-b border-gray-200 overflow-x-auto">
              <nav className="flex gap-6 min-w-max">
                {([
                  { key: "summary",    label: "Payment Summary" },
                  { key: "classes",    label: "Class Details" },
                  { key: "trace",      label: `Waterfall Trace (${allTrace.length})` },
                  { key: "collateral", label: "Collateral" },
                ] as const).map(({ key, label }) => (
                  <button key={key} onClick={() => setActiveTab(key)}
                    className={`pb-3 text-sm transition-colors ${activeTab === key ? "tab-active" : "tab-inactive"}`}>
                    {label}
                  </button>
                ))}
              </nav>
            </div>

            {/* Payment Summary table */}
            {activeTab === "summary" && (
              <div className="space-y-4">
              <div className="card overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Class", "Type", "Beg. Balance", "Int. Rate", "Int. Accrued", "Int. Paid", "Shortfall", "Prin. Paid", "End Balance", "Factor"].map(h => (
                        <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(result.class_details ?? []).map((cd, i) => (
                      <tr key={cd.class_name} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                        <td className="px-3 py-2 font-semibold text-primary-700">{cd.class_name}</td>
                        <td className="px-3 py-2">
                          <span className={`badge ${cd.class_type === "Senior" ? "badge-green" : cd.class_type === "Residual" ? "badge-yellow" : "badge-blue"}`}>
                            {cd.class_type}
                          </span>
                        </td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(cd.beginning_principal)}</td>
                        <td className="px-3 py-2 font-mono">{fmtPct(cd.interest_rate)}</td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(cd.interest_accrued)}</td>
                        <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(cd.interest_paid)}</td>
                        <td className="px-3 py-2 font-mono">
                          {n(cd.ending_interest_carryforward) > 0
                            ? <span className="text-red-600 flex items-center gap-1"><AlertCircle size={12}/>{fmtUSD(cd.ending_interest_carryforward)}</span>
                            : <span className="text-gray-400">—</span>}
                        </td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(cd.principal_paid)}</td>
                        <td className="px-3 py-2 font-mono font-semibold">{fmtUSD(cd.ending_principal)}</td>
                        <td className="px-3 py-2 font-mono text-gray-500">{n(cd.factor_ending).toFixed(6)}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="bg-primary-50 border-t-2 border-primary-200 font-bold">
                      <td className="px-3 py-2 text-primary-800" colSpan={2}>TOTAL</td>
                      <td className="px-3 py-2 font-mono">{fmtUSD(result.total_beginning_principal)}</td>
                      <td/>
                      <td className="px-3 py-2 font-mono">{fmtUSD(result.total_interest_paid)}</td>
                      <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(result.total_interest_paid)}</td>
                      <td/>
                      <td className="px-3 py-2 font-mono">{fmtUSD(result.total_principal_paid)}</td>
                      <td className="px-3 py-2 font-mono">{fmtUSD(result.total_ending_principal)}</td>
                      <td/>
                    </tr>
                  </tfoot>
                </table>
              </div>

              {/* Fees Paid */}
              <div className="card overflow-x-auto">
                <h4 className="section-title mb-3">Fees Paid</h4>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Fee Name", "Amount Due", "Amount Paid", "Shortfall"].map(h => (
                        <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(result.fees_detail ?? []).length === 0 ? (
                      <tr><td colSpan={4} className="px-3 py-3 text-center text-gray-400">$0.00</td></tr>
                    ) : (result.fees_detail ?? []).map((f, i) => (
                      <tr key={`fee-${i}`} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                        <td className="px-3 py-2 font-medium">{f.fee_name}</td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(f.total_due)}</td>
                        <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(f.amount_paid)}</td>
                        <td className="px-3 py-2 font-mono">{n(f.ending_shortfall) > 0 ? <span className="text-red-600">{fmtUSD(f.ending_shortfall)}</span> : <span className="text-gray-400">$0.00</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Expenses Paid */}
              <div className="card overflow-x-auto">
                <h4 className="section-title mb-3">Expenses Paid</h4>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Expense Name", "Amount Due", "Amount Paid", "Shortfall"].map(h => (
                        <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(result.expenses_detail ?? []).length === 0 ? (
                      <tr><td colSpan={4} className="px-3 py-3 text-center text-gray-400">$0.00</td></tr>
                    ) : (result.expenses_detail ?? []).map((e, i) => (
                      <tr key={`exp-${i}`} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                        <td className="px-3 py-2 font-medium">{e.expense_name}</td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(e.total_due)}</td>
                        <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(e.amount_paid)}</td>
                        <td className="px-3 py-2 font-mono">{n(e.ending_shortfall) > 0 ? <span className="text-red-600">{fmtUSD(e.ending_shortfall)}</span> : <span className="text-gray-400">$0.00</span>}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>

              {/* Reserve Account Balances */}
              <div className="card overflow-x-auto">
                <h4 className="section-title mb-3">Reserve Account Balances</h4>
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Account Name", "Beginning Balance", "Deposits This Period", "Withdrawals This Period", "Ending Balance"].map(h => (
                        <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {(result.reserve_accounts ?? []).length === 0 ? (
                      <tr><td colSpan={5} className="px-3 py-3 text-center text-gray-400">$0.00</td></tr>
                    ) : (result.reserve_accounts ?? []).map((a, i) => (
                      <tr key={`res-${i}`} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                        <td className="px-3 py-2 font-medium">{a.account_name}</td>
                        <td className="px-3 py-2 font-mono">{fmtUSD(a.beginning_balance)}</td>
                        <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(a.deposits)}</td>
                        <td className="px-3 py-2 font-mono text-amber-700">{fmtUSD(a.withdrawals)}</td>
                        <td className="px-3 py-2 font-mono font-semibold">{fmtUSD(a.ending_balance_post_payment)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              </div>
            )}

            {/* Class Details */}
            {activeTab === "classes" && (
              <div className="grid md:grid-cols-2 gap-4">
                {(result.class_details ?? []).map((cd) => (
                  <div key={cd.class_name} className="card">
                    <div className="flex items-center justify-between mb-4">
                      <h4 className="font-bold text-lg text-primary-700">{cd.class_name}</h4>
                      <span className={`badge ${cd.class_type === "Senior" ? "badge-green" : "badge-blue"}`}>{cd.class_type}</span>
                    </div>
                    <div className="grid grid-cols-2 gap-3 text-sm">
                      {([
                        ["Original Bal", fmtUSD(cd.original_principal)],
                        ["Beginning Bal", fmtUSD(cd.beginning_principal)],
                        ["Ending Bal", fmtUSD(cd.ending_principal)],
                        ["Interest Rate", fmtPct(cd.interest_rate)],
                        ["Days Accrued", String(cd.days_accrued ?? "—")],
                        ["Interest Accrued", fmtUSD(cd.interest_accrued)],
                        ["Interest Paid", fmtUSD(cd.interest_paid)],
                        ["Shortfall", fmtUSD(cd.ending_interest_carryforward)],
                        ["Principal Paid", fmtUSD(cd.principal_paid)],
                        ["Realized Loss", fmtUSD(cd.realized_loss)],
                        ["Factor (Beg)", n(cd.factor_beginning).toFixed(6)],
                        ["Factor (End)", n(cd.factor_ending).toFixed(6)],
                      ] as [string, string][]).map(([label, value]) => (
                        <div key={label}>
                          <p className="text-xs text-gray-400 font-medium">{label}</p>
                          <p className="font-medium text-gray-800">{value}</p>
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}

            {/* Waterfall Trace */}
            {activeTab === "trace" && (
              <div className="card overflow-x-auto">
                {allTrace.length === 0 ? (
                  <div className="py-10 text-center">
                    <p className="text-sm text-gray-500">No waterfall trace steps recorded.</p>
                    <p className="text-xs text-gray-400 mt-1">
                      This usually means the waterfall steps haven't been configured in the deal config, or all class balances are zero.
                    </p>
                  </div>
                ) : (
                  <table className="w-full text-sm">
                    <thead>
                      <tr className="table-header">
                        {["Step", "Description", "Bucket", "Funds Available", "Amount Owed", "Amount Paid", "Funds Remaining"].map(h => (
                          <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {allTrace.map((step, i) => (
                        <tr key={`${step.step}-${i}`} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                          <td className="px-3 py-2">
                            <span className="w-6 h-6 bg-primary-100 text-primary-700 rounded-full flex items-center justify-center text-xs font-bold">
                              {step.step}
                            </span>
                          </td>
                          <td className="px-3 py-2 text-gray-700 max-w-xs">{step.description}</td>
                          <td className="px-3 py-2">
                            <span className="badge bg-gray-100 text-gray-600">{step.payment_type ?? step.source_bucket ?? "—"}</span>
                          </td>
                          <td className="px-3 py-2 font-mono">{fmtUSD(step.funds_available)}</td>
                          <td className="px-3 py-2 font-mono">{fmtUSD(step.amount_owed)}</td>
                          <td className="px-3 py-2 font-mono text-green-700">{fmtUSD(step.amount_paid)}</td>
                          <td className="px-3 py-2 font-mono font-semibold">{fmtUSD(step.funds_remaining)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
              </div>
            )}

            {/* Collateral */}
            {activeTab === "collateral" && (
              <div className="space-y-4">
                <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
                  <StatCard label="Beginning Pool" value={fmtUSD(result.prior_pool_balance)} />
                  <StatCard label="Ending Pool"    value={fmtUSD(result.current_pool_balance)} accent />
                  <StatCard label="Gross WAC"      value={fmtPct(result.gross_wac)} />
                  <StatCard label="Net WAC"        value={fmtPct(result.net_wac)} />
                  <StatCard label="Curtailments"   value={fmtUSD(result.curtailments)} />
                  <StatCard label="Prepayments"    value={fmtUSD(result.prepayments_in_full)} />
                  <StatCard label="Charge-Offs"    value={fmtUSD(result.charge_offs)} />
                  <StatCard label="Loan Count"     value={fmt(result.current_loan_count, 0)} />
                </div>

                {(result.performance_buckets ?? []).length > 0 && (
                  <div className="card overflow-x-auto">
                    <h4 className="section-title mb-3">Delinquency Buckets</h4>
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="table-header">
                          {["Category", "Loan Count", "Balance", "% of Pool"].map(h => (
                            <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {(result.performance_buckets ?? []).map((b, i) => (
                          <tr key={b.bucket} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                            <td className="px-3 py-2 font-medium">{b.bucket}</td>
                            <td className="px-3 py-2 font-mono">{fmt(b.count, 0)}</td>
                            <td className="px-3 py-2 font-mono">{fmtUSD(b.amount)}</td>
                            <td className="px-3 py-2">
                              <div className="flex items-center gap-2">
                                <div className="flex-1 h-2 bg-gray-100 rounded-full overflow-hidden max-w-24">
                                  <div className="h-full bg-primary-500 rounded-full"
                                    style={{ width: `${Math.min(n(b.pct_amount), 100)}%` }} />
                                </div>
                                <span className="font-mono text-xs">{n(b.pct_amount).toFixed(1)}%</span>
                              </div>
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
