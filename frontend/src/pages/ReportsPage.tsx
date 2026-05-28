import { useState, useEffect } from "react";
import {
  BarChart2, Download, RefreshCw, ChevronDown,
  TrendingUp, DollarSign, Shield, Activity, FileText,
  Gauge, Layers, Wallet, GitBranch, Users,
} from "lucide-react";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, LineChart, Line, Legend, CartesianGrid,
} from "recharts";
import Header from "../components/layout/Header";
import LoadingSpinner from "../components/shared/LoadingSpinner";
import EmptyState from "../components/shared/EmptyState";
import type { WaterfallResult, DashboardKPI } from "../types";
import { listReports, getReport, generateReport, downloadReport } from "../services/api";
import toast from "react-hot-toast";

// ─── Formatters ─────────────────────────────────────────────────────────────
const n = (v: unknown): number => (typeof v === "number" && isFinite(v) ? v : 0);
const fmt = (v: unknown, dec = 2) =>
  n(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
const fmtUSD = (v: unknown) => {
  const val = n(v);
  return val >= 1_000_000 ? `$${(val / 1_000_000).toFixed(2)}M` : `$${fmt(val)}`;
};
const fmtUSDFull = (v: unknown) => `$${fmt(v)}`;
const fmtPct = (v: unknown, dec = 4) => `${(n(v) * 100).toFixed(dec)}%`;

// ─── Color palette ──────────────────────────────────────────────────────────
const COLOR_PRIMARY = "#1B5E45";   // primary-700 (existing template)
const COLOR_GOLD = "#C9A24A";
const COLOR_NAVY = "#0A2342";

// Doughnut slice colors: greens for current/1-29, ambers for 30-89, reds for 90+
const DELINQ_COLORS: Record<string, string> = {
  "Current":      "#1B5E20",
  "1-29 Days":    "#4CAF50",
  "30-59 Days":   "#FF9800",
  "60-89 Days":   "#F57C00",
  "90-119 Days":  "#E53935",
  "120-149 Days": "#C62828",
  "150-179 Days": "#B71C1C",
  "180+ Days":    "#7F0000",
};

// ─── Small components ───────────────────────────────────────────────────────
function KPITile({ kpi }: { kpi: DashboardKPI }) {
  return (
    <div className="bg-primary-700 rounded-xl px-4 py-3 min-w-0">
      <p className="text-white/70 text-[10px] font-semibold uppercase tracking-wider truncate">{kpi.label}</p>
      <p className="text-white text-lg font-bold mt-0.5 truncate">{kpi.value}</p>
    </div>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-semibold text-primary-800 mb-3 mt-1">{children}</h3>;
}

// ─── Reusable data table ────────────────────────────────────────────────────
function DataTable({
  headers,
  rows,
  footer,
}: {
  headers: string[];
  rows: (string | number | React.ReactNode)[][];
  footer?: (string | number | React.ReactNode)[];
}) {
  return (
    <div className="card overflow-x-auto p-0">
      <table className="w-full text-sm">
        <thead>
          <tr className="table-header">
            {headers.map((h, i) => (
              <th key={i} className={`px-3 py-2.5 text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg ${i === 0 ? "text-left" : "text-right"}`}>{h}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri} className={ri % 2 === 0 ? "table-row-even" : "table-row-odd"}>
              {row.map((cell, ci) => (
                <td key={ci} className={`px-3 py-2 ${ci === 0 ? "" : "font-mono text-right"} text-xs`}>{cell}</td>
              ))}
            </tr>
          ))}
        </tbody>
        {footer && (
          <tfoot>
            <tr className="bg-primary-50 border-t-2 border-primary-200 font-semibold">
              {footer.map((cell, ci) => (
                <td key={ci} className={`px-3 py-2.5 ${ci === 0 ? "text-primary-800" : "font-mono text-right text-primary-800"} text-xs`}>{cell}</td>
              ))}
            </tr>
          </tfoot>
        )}
      </table>
    </div>
  );
}

// ─── Top-level ──────────────────────────────────────────────────────────────
interface ReportSummary {
  deal_id: string;
  payment_date: string;
  deal_name?: string;
  asset_class?: string;
  current_pool_balance?: number;
  total_interest_paid?: number;
  total_principal_paid?: number;
}

type TabKey =
  | "dashboard" | "payments" | "collateral" | "accounts"
  | "fees" | "expenses" | "events" | "priority" | "loans";

export default function ReportsPage() {
  const [reports, setReports]           = useState<ReportSummary[]>([]);
  const [loadingList, setLoadingList]   = useState(true);
  const [selectedDeal, setSelectedDeal] = useState("");
  const [selectedDate, setSelectedDate] = useState("");
  const [loading, setLoading]           = useState(false);
  const [generating, setGenerating]     = useState(false);
  const [report, setReport]             = useState<WaterfallResult | null>(null);
  const [activeTab, setActiveTab]       = useState<TabKey>("dashboard");

  const uniqueDeals = [...new Set(reports.map((r) => r.deal_id))];
  const datesForDeal = reports
    .filter((r) => r.deal_id === selectedDeal)
    .map((r) => r.payment_date)
    .sort((a, b) => b.localeCompare(a));

  useEffect(() => {
    listReports()
      .then((d) => setReports(d.reports ?? []))
      .catch(() => {})
      .finally(() => setLoadingList(false));
  }, []);

  const loadReport = (dealId: string, date: string) => {
    setLoading(true);
    setReport(null);
    getReport(dealId, date)
      .then((d) => { setReport(d); setActiveTab("dashboard"); })
      .catch(() => toast.error("Report not found. Generate it first."))
      .finally(() => setLoading(false));
  };

  const handleLoadReport = () => {
    if (!selectedDeal || !selectedDate) return;
    loadReport(selectedDeal, selectedDate);
  };

  const handleGenerate = async () => {
    if (!selectedDeal || !selectedDate) return;
    setGenerating(true);
    try {
      const data = await generateReport(selectedDeal, selectedDate);
      setReport(data.report ?? data);
      setActiveTab("dashboard");
      toast.success("Report generated!");
      listReports().then((d) => setReports(d.reports ?? []));
    } catch (err: unknown) {
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Generation failed.";
      toast.error(msg.slice(0, 100));
    } finally {
      setGenerating(false);
    }
  };

  const tabs: { key: TabKey; label: string; icon: typeof Gauge }[] = [
    { key: "dashboard",  label: "Dashboard",   icon: Gauge },
    { key: "payments",   label: "Payments",    icon: Shield },
    { key: "collateral", label: "Collateral",  icon: TrendingUp },
    { key: "accounts",   label: "Accounts",    icon: Wallet },
    { key: "fees",       label: "Fees",        icon: DollarSign },
    { key: "expenses",   label: "Expenses",    icon: Layers },
    { key: "events",     label: "Events",      icon: Activity },
    { key: "priority",   label: "Priority",    icon: GitBranch },
    { key: "loans",      label: "Loan Detail", icon: Users },
  ];

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <Header
        title="Investor Reports"
        subtitle="View structured waterfall reports by deal and payment date, or download as PDF"
      />

      <main className="flex-1 overflow-y-auto p-6 space-y-5">

        {/* ── Controls ─────────────────────────────────────────────────── */}
        <div className="card">
          <h2 className="section-title mb-4">
            <BarChart2 size={16} className="text-primary-600" /> Select Report
          </h2>

          <div className="flex flex-wrap items-end gap-3">
            <div className="flex-1 min-w-[160px]">
              <label className="label">Deal ID</label>
              <div className="relative">
                <select
                  className="select pr-8"
                  value={selectedDeal}
                  onChange={(e) => { setSelectedDeal(e.target.value); setSelectedDate(""); setReport(null); }}
                >
                  <option value="">Select deal…</option>
                  {uniqueDeals.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              </div>
            </div>

            <div className="flex-1 min-w-[160px]">
              <label className="label">Payment Date</label>
              <div className="relative">
                <select
                  className="select pr-8"
                  value={selectedDate}
                  onChange={(e) => { setSelectedDate(e.target.value); setReport(null); }}
                  disabled={!selectedDeal}
                >
                  <option value="">Select date…</option>
                  {datesForDeal.map((d) => <option key={d} value={d}>{d}</option>)}
                </select>
                <ChevronDown size={14} className="absolute right-2.5 top-1/2 -translate-y-1/2 text-gray-400 pointer-events-none" />
              </div>
            </div>

            <div className="flex gap-2 flex-shrink-0">
              <button
                className="btn-secondary h-[38px] px-4"
                onClick={handleLoadReport}
                disabled={!selectedDeal || !selectedDate || loading}
              >
                <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
                {loading ? "Loading…" : "Load Report"}
              </button>
              <button
                className="btn-primary h-[38px] px-4"
                onClick={handleGenerate}
                disabled={!selectedDeal || !selectedDate || generating}
              >
                <BarChart2 size={14} />
                {generating ? "Generating…" : "Generate"}
              </button>
            </div>
          </div>
        </div>

        {/* ── Available Reports list ────────────────────────────────────── */}
        {!report && !loading && (
          <div className="card">
            <h3 className="section-title mb-4">
              <FileText size={15} className="text-primary-600" /> Available Reports
            </h3>
            {loadingList ? (
              <div className="py-10 flex justify-center">
                <LoadingSpinner text="Loading reports…" />
              </div>
            ) : reports.length === 0 ? (
              <EmptyState
                icon={BarChart2}
                title="No reports yet"
                description="Run a waterfall computation and generate a report to view it here."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Deal", "Payment Date", "Pool Balance", "Int. Paid", "Prin. Paid", "Asset Class", "Actions"].map((h) => (
                        <th key={h} className="px-4 py-3 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {reports.map((r, i) => (
                      <tr
                        key={`${r.deal_id}-${r.payment_date}`}
                        className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}
                      >
                        <td className="px-4 py-3">
                          <p className="font-semibold text-gray-800 text-xs">{r.deal_id}</p>
                          <p className="text-[11px] text-gray-400 mt-0.5 truncate max-w-[180px]">{r.deal_name}</p>
                        </td>
                        <td className="px-4 py-3 font-mono text-gray-700 whitespace-nowrap">{r.payment_date}</td>
                        <td className="px-4 py-3 font-mono text-gray-700">{r.current_pool_balance ? fmtUSD(r.current_pool_balance) : "—"}</td>
                        <td className="px-4 py-3 font-mono text-green-700">{r.total_interest_paid ? fmtUSD(r.total_interest_paid) : "—"}</td>
                        <td className="px-4 py-3 font-mono text-gray-700">{r.total_principal_paid ? fmtUSD(r.total_principal_paid) : "—"}</td>
                        <td className="px-4 py-3 text-gray-500 text-xs">{r.asset_class ?? "—"}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-2">
                            <button
                              className="btn-secondary text-xs px-3 py-1.5 h-auto"
                              onClick={() => { setSelectedDeal(r.deal_id); setSelectedDate(r.payment_date); loadReport(r.deal_id, r.payment_date); }}
                            >
                              View
                            </button>
                            <a
                              href={downloadReport(r.deal_id, r.payment_date)}
                              target="_blank"
                              rel="noreferrer"
                              className="btn-primary text-xs px-3 py-1.5 h-auto inline-flex items-center gap-1"
                            >
                              <Download size={11} /> PDF
                            </a>
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

        {/* ── Loading spinner ───────────────────────────────────────────── */}
        {loading && (
          <div className="card flex items-center justify-center py-14">
            <LoadingSpinner text="Loading report…" />
          </div>
        )}

        {/* ── Report Detail View ────────────────────────────────────────── */}
        {report && !loading && (
          <div className="fade-in space-y-4">

            {/* Header banner */}
            <div className="rounded-2xl bg-gradient-to-br from-primary-800 to-primary-600 p-5 shadow-md">
              <div className="flex items-start justify-between gap-4 flex-wrap">
                <div>
                  <p className="text-white/60 text-[10px] font-semibold uppercase tracking-widest mb-1">Investor Report</p>
                  <h2 className="text-xl font-bold text-white leading-tight">{report.deal_name}</h2>
                  <p className="text-white/70 text-xs mt-1">
                    Payment Date:&nbsp;<span className="text-white font-semibold">{report.payment_date}</span>
                    &nbsp;·&nbsp;Accrual:&nbsp;
                    <span className="text-white font-medium">{report.accrual_start_date} → {report.accrual_end_date}</span>
                  </p>
                </div>
                <div className="flex items-center gap-2 flex-shrink-0">
                  <button
                    className="inline-flex items-center gap-1.5 text-xs text-white/70 hover:text-white transition-colors"
                    onClick={() => setReport(null)}
                  >
                    ← Back to list
                  </button>
                  <a
                    href={downloadReport(report.deal_id ?? selectedDeal, report.payment_date ?? selectedDate)}
                    target="_blank"
                    rel="noreferrer"
                    className="inline-flex items-center gap-2 px-4 py-2 bg-white text-primary-700 text-sm font-semibold rounded-lg hover:bg-primary-50 transition-colors whitespace-nowrap"
                  >
                    <Download size={14} /> Download PDF
                  </a>
                </div>
              </div>
            </div>

            {/* Tabs */}
            <div className="border-b border-gray-200 overflow-x-auto">
              <nav className="flex gap-1 min-w-max">
                {tabs.map(({ key, label, icon: Icon }) => (
                  <button
                    key={key}
                    onClick={() => setActiveTab(key)}
                    className={`pb-2.5 px-3 text-sm flex items-center gap-1.5 transition-colors whitespace-nowrap ${
                      activeTab === key ? "tab-active" : "tab-inactive"
                    }`}
                  >
                    <Icon size={13} />
                    {label}
                  </button>
                ))}
              </nav>
            </div>

            {/* ── DASHBOARD ──────────────────────────────────────────── */}
            {activeTab === "dashboard" && (
              <div className="space-y-4">
                {/* KPI strip */}
                <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-3">
                  {(report.dashboard_kpis ?? []).map((k, i) => <KPITile key={i} kpi={k} />)}
                </div>
              </div>
            )}

            {/* ── PAYMENTS (1) ───────────────────────────────────────── */}
            {activeTab === "payments" && (
              <div className="space-y-6">
                <div>
                  <SectionTitle>1(a) Payments Summary</SectionTitle>
                  {/* Horizontal bar chart of ending balances per class */}
                  <div className="card mb-3 py-3" style={{ height: 280 }}>
                    <ResponsiveContainer width="100%" height="100%">
                      <BarChart
                        data={(report.class_details ?? []).map((cd) => ({
                          name: cd.class_name,
                          ending: n(cd.ending_principal) / 1_000_000,
                        }))}
                        layout="vertical"
                        margin={{ top: 10, right: 30, left: 30, bottom: 5 }}
                      >
                        <CartesianGrid strokeDasharray="3 3" />
                        <XAxis type="number" tick={{ fontSize: 10 }} />
                        <YAxis dataKey="name" type="category" tick={{ fontSize: 10 }} />
                        <Tooltip formatter={(v: number) => `$${v.toFixed(2)}M`} />
                        <Bar dataKey="ending" fill={COLOR_PRIMARY} name="Ending Balance ($MM)" />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                  <DataTable
                    headers={["Class", "Type", "Original", "Beginning", "Int. Paid", "Prin. Paid", "Total Paid", "Ending"]}
                    rows={(report.class_details ?? []).map((cd) => [
                      <span className="font-bold text-primary-700">{cd.class_name}</span>,
                      cd.class_type,
                      fmtUSD(cd.original_principal),
                      fmtUSD(cd.beginning_principal),
                      <span className="text-green-700 font-semibold">{fmtUSD(cd.interest_paid)}</span>,
                      fmtUSD(cd.principal_paid),
                      fmtUSD(cd.total_paid),
                      fmtUSD(cd.ending_principal),
                    ])}
                    footer={[
                      "TOTAL", "",
                      fmtUSD(report.total_original_principal),
                      fmtUSD(report.total_beginning_principal),
                      fmtUSD(report.total_interest_paid),
                      fmtUSD(report.total_principal_paid),
                      fmtUSD(report.total_paid ?? n(report.total_interest_paid) + n(report.total_principal_paid)),
                      fmtUSD(report.total_ending_principal),
                    ]}
                  />
                </div>

                <div>
                  <SectionTitle>1(b) Interest</SectionTitle>
                  <DataTable
                    headers={["Class", "Days", "Day Count", "Rate", "Prior Unpaid", "Optimal Int.", "Total Due", "Paid", "End. Carryforward"]}
                    rows={(report.class_details ?? []).map((cd) => [
                      cd.class_name,
                      cd.days_accrued,
                      cd.day_count_method || ((cd.days_accrued === 30 && report.days_accrued !== 30) ? "30/360" : "actual/360"),
                      fmtPct(cd.interest_rate),
                      fmtUSD(cd.beginning_interest_carryforward),
                      fmtUSD(cd.interest_accrued),
                      fmtUSD(cd.total_interest_due),
                      fmtUSD(cd.interest_paid),
                      n(cd.ending_interest_carryforward) > 0
                        ? <span className="text-red-600">{fmtUSD(cd.ending_interest_carryforward)}</span>
                        : <span className="text-gray-400">—</span>,
                    ])}
                  />
                </div>

                <div>
                  <SectionTitle>1(c) Cap Carryover</SectionTitle>
                  <DataTable
                    headers={["Class", "WAC Cap", "Beg. Cap", "Current", "Total", "Paid", "End. Cap"]}
                    rows={(report.class_details ?? []).map((cd) => [
                      cd.class_name,
                      fmtPct(report.net_wac),
                      fmtUSD(cd.beginning_cap_carryover),
                      fmtUSD(cd.current_cap_carryover),
                      fmtUSD(cd.total_cap_carryover),
                      fmtUSD(cd.cap_carryover_paid),
                      fmtUSD(cd.ending_cap_carryover),
                    ])}
                  />
                </div>

                <div>
                  <SectionTitle>1(d) Principal</SectionTitle>
                  <DataTable
                    headers={["Class", "Beginning", "Paid", "Writeup/(Down)", "Cum. Writedown", "Realized Loss", "Cum. Loss", "Ending"]}
                    rows={(report.class_details ?? []).map((cd) => [
                      cd.class_name,
                      fmtUSD(cd.beginning_principal),
                      fmtUSD(cd.principal_paid),
                      fmtUSD(n(cd.writeup_amount) - n(cd.writedown_amount)),
                      fmtUSD(cd.cumulative_writedown),
                      fmtUSD(cd.realized_loss),
                      fmtUSD(cd.cumulative_realized_loss),
                      fmtUSD(cd.ending_principal),
                    ])}
                  />
                </div>

                <div>
                  <SectionTitle>1(e) Factors</SectionTitle>
                  <DataTable
                    headers={["Class", "Beg. Factor", "Int. Factor", "Prin. Factor", "Total Factor", "End. Factor", "Record Date"]}
                    rows={(report.class_details ?? []).map((cd) => [
                      cd.class_name,
                      n(cd.factor_beginning).toFixed(6),
                      n(cd.factor_interest).toFixed(6),
                      n(cd.factor_principal).toFixed(6),
                      n(cd.factor_total).toFixed(6),
                      n(cd.factor_ending).toFixed(6),
                      cd.record_date ?? "",
                    ])}
                  />
                </div>

                <div>
                  <SectionTitle>1(f) Cumulative Payment Detail</SectionTitle>
                  <DataTable
                    headers={["Class", "Original", "Cum. Interest Paid", "Cum. Principal Paid",
                             "Cum. Total Dist.", "Cum. Realized Loss", "Cum. Deferred Int.", "Ending Bal."]}
                    rows={(report.class_details ?? []).map((cd) => [
                      cd.class_name,
                      fmtUSD(cd.original_principal),
                      fmtUSD(cd.cumulative_interest_paid),
                      fmtUSD(cd.cumulative_principal_paid),
                      fmtUSD(cd.cumulative_total_distribution),
                      fmtUSD(cd.cumulative_realized_loss),
                      fmtUSD(cd.cumulative_deferred_interest),
                      fmtUSD(cd.ending_principal),
                    ])}
                  />
                </div>
              </div>
            )}

            {/* ── COLLATERAL (2) ─────────────────────────────────────── */}
            {activeTab === "collateral" && (
              <div className="space-y-6">
                {/* 2(a) summary */}
                <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-4">
                  {[
                    ["Beginning Pool", fmtUSD(report.prior_pool_balance)],
                    ["Ending Pool",    fmtUSD(report.current_pool_balance)],
                    ["Gross WAC",      fmtPct(report.gross_wac)],
                    ["Net WAC",        fmtPct(report.net_wac)],
                    ["Int. Remittance",  fmtUSD(report.interest_remittance_amount)],
                    ["Prin. Remittance", fmtUSD(report.principal_remittance_amount)],
                    ["Curtailments",     fmtUSD(report.curtailments)],
                    ["Prepayments",      fmtUSD(report.prepayments_in_full)],
                  ].map(([label, value]) => (
                    <div key={label as string} className="card py-3">
                      <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider">{label}</p>
                      <p className="text-base font-bold text-gray-800 mt-1">{value}</p>
                    </div>
                  ))}
                </div>

                {/* 2(b) Performance — doughnut + matrix */}
                <div>
                  <SectionTitle>2(b) Performance</SectionTitle>
                  {(report.performance_buckets ?? []).filter((b) => DELINQ_COLORS[b.bucket]).length > 0 && (
                    <div className="card mb-3 py-3" style={{ height: 360 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <PieChart margin={{ top: 5, right: 5, bottom: 60, left: 5 }}>
                          <Pie
                            data={(report.performance_buckets ?? []).filter((b) => DELINQ_COLORS[b.bucket] && b.pct_amount > 0).map((b) => ({
                              name: b.bucket, value: n(b.pct_amount),
                            }))}
                            innerRadius={55} outerRadius={105}
                            dataKey="value"
                            nameKey="name"
                          >
                            {(report.performance_buckets ?? []).filter((b) => DELINQ_COLORS[b.bucket] && b.pct_amount > 0).map((b, i) => (
                              <Cell key={i} fill={DELINQ_COLORS[b.bucket]} />
                            ))}
                          </Pie>
                          <Tooltip formatter={(v: number) => `${v.toFixed(2)}%`} />
                          <Legend
                            verticalAlign="bottom"
                            align="center"
                            iconType="circle"
                            wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                          />
                        </PieChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                  {/* 2D matrix */}
                  {report.delinquency_matrix && (
                    <div className="card overflow-x-auto p-0">
                      <table className="w-full text-sm">
                        <thead>
                          <tr className="table-header">
                            <th className="px-3 py-2.5 text-left text-xs font-semibold">DPD Bucket</th>
                            {report.delinquency_matrix.columns.map((c) => (
                              <th key={c} className="px-3 py-2.5 text-right text-xs font-semibold">{c}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {report.delinquency_matrix.rows.map((r, ri) => {
                            const cellsForRow = report.delinquency_matrix!.cells.filter((c) => c.dpd_bucket === r);
                            const map = new Map(cellsForRow.map((c) => [c.disposition, c]));
                            return (
                              <tr key={r} className={ri % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                                <td className="px-3 py-2 font-medium text-xs">{r}</td>
                                {report.delinquency_matrix!.columns.slice(0, -1).map((disp) => {
                                  const cell = map.get(disp);
                                  return (
                                    <td key={disp} className="px-3 py-2 font-mono text-right text-xs">
                                      {cell && cell.amount > 0 ? fmtUSD(cell.amount) : <span className="text-gray-300">—</span>}
                                    </td>
                                  );
                                })}
                                <td className="px-3 py-2 font-mono text-right text-xs font-semibold">
                                  {fmtUSD(report.delinquency_matrix!.row_totals[r] ?? 0)}
                                </td>
                              </tr>
                            );
                          })}
                          <tr className="bg-primary-50 border-t-2 border-primary-200 font-semibold">
                            <td className="px-3 py-2.5 text-primary-800 text-xs">Total</td>
                            {report.delinquency_matrix.columns.slice(0, -1).map((disp) => (
                              <td key={disp} className="px-3 py-2.5 font-mono text-right text-xs text-primary-800">
                                {fmtUSD(report.delinquency_matrix!.col_totals[disp] ?? 0)}
                              </td>
                            ))}
                            <td className="px-3 py-2.5 font-mono text-right text-xs text-primary-800">
                              {fmtUSD(Object.values(report.delinquency_matrix!.col_totals).reduce((a, b) => a + b, 0))}
                            </td>
                          </tr>
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                {/* 2(c) Rates — line chart + table */}
                <div>
                  <SectionTitle>2(c) Rates</SectionTitle>
                  {(report.performance_history ?? []).length > 0 && (
                    <div className="card mb-3 py-3" style={{ height: 280 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <LineChart
                          data={(report.performance_history ?? []).map((h) => ({
                            date: h.date,
                            cpr: n(h.cpr_1m) * 100,
                            cdr: n(h.cdr_1m) * 100,
                          }))}
                          margin={{ top: 10, right: 30, left: 0, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="date" tick={{ fontSize: 10 }} />
                          <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => `${v.toFixed(1)}%`} />
                          <Tooltip formatter={(v: number) => `${v.toFixed(3)}%`} />
                          <Legend />
                          <Line type="monotone" dataKey="cpr" stroke={COLOR_NAVY} name="CPR (1M)" />
                          <Line type="monotone" dataKey="cdr" stroke={COLOR_GOLD} name="CDR (1M)" />
                        </LineChart>
                      </ResponsiveContainer>
                    </div>
                  )}
                  <DataTable
                    headers={["Metric", "1M", "3M", "Inception", "SMM (1M)"]}
                    rows={[
                      ["Defaults (CDR)",
                        fmtPct(report.collateral_rates?.cdr_1m),
                        fmtPct(report.collateral_rates?.cdr_3m),
                        fmtPct(report.collateral_rates?.cdr_inception),
                        fmtPct(report.collateral_rates?.smm_default),
                      ],
                      ["Prepayments (CPR)",
                        fmtPct(report.collateral_rates?.cpr_1m),
                        fmtPct(report.collateral_rates?.cpr_3m),
                        fmtPct(report.collateral_rates?.cpr_inception),
                        fmtPct(report.collateral_rates?.smm_prepay),
                      ],
                    ]}
                  />
                </div>

                {/* 2(d) Realized Loss */}
                {report.collateral_realized_loss && (
                  <div>
                    <SectionTitle>2(d) Realized Loss</SectionTitle>
                    <DataTable
                      headers={["Metric", "Current", "Cumulative"]}
                      rows={[
                        ["Realized Loss",
                          fmtUSD(report.collateral_realized_loss.realized_loss_current),
                          fmtUSD(report.collateral_realized_loss.realized_loss_cumulative)],
                        ["Number of Loans Liquidated",
                          fmt(report.collateral_realized_loss.loans_liquidated_current, 0),
                          fmt(report.collateral_realized_loss.loans_liquidated_cumulative, 0)],
                        ["Net Liquidation Proceeds",
                          fmtUSD(report.collateral_realized_loss.net_liquidation_proceeds_current),
                          fmtUSD(report.collateral_realized_loss.net_liquidation_proceeds_cumulative)],
                      ]}
                    />
                  </div>
                )}

                {/* 2(e) Structural Features — grouped bar + table */}
                {report.structural_features && (
                  <div>
                    <SectionTitle>2(e) Structural Features</SectionTitle>
                    <div className="card mb-3 py-3" style={{ height: 280 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={["M-1", "M-2", "M-3"].map((cn) => ({
                            class: cn,
                            original: n(report.structural_features!.original_credit_support[cn]) * 100,
                            current: n(report.structural_features!.current_credit_support[cn]) * 100,
                          }))}
                          margin={{ top: 10, right: 30, left: 0, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis dataKey="class" tick={{ fontSize: 11 }} />
                          <YAxis tick={{ fontSize: 10 }} tickFormatter={(v: number) => `${v.toFixed(0)}%`} />
                          <Tooltip formatter={(v: number) => `${v.toFixed(3)}%`} />
                          <Legend />
                          <Bar dataKey="original" fill={COLOR_NAVY} name="Original CE %" />
                          <Bar dataKey="current" fill={COLOR_GOLD} name="Current CE %" />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                    <DataTable
                      headers={["Metric", "Value"]}
                      rows={[
                        ["Gross WAC", fmtPct(report.structural_features.gross_wac)],
                        ["Net WAC", fmtPct(report.structural_features.net_wac)],
                        ["WAC Cap", fmtPct(report.structural_features.wac_cap)],
                        ...(["M-1", "M-2", "M-3"].map((cn) =>
                          [`Original CE % — ${cn}`,
                           fmtPct(report.structural_features!.original_credit_support[cn])] as [string, string]
                        )),
                        ...(["M-1", "M-2", "M-3"].map((cn) =>
                          [`Current CE % — ${cn}`,
                           fmtPct(report.structural_features!.current_credit_support[cn])] as [string, string]
                        )),
                        ["Non-Performing Loan %", fmtPct(report.structural_features.non_performing_loan_pct)],
                        ["Charged-Off Loan %", fmtPct(report.structural_features.charged_off_loan_pct)],
                        ...Object.entries(report.structural_features.beginning_upb_by_servicer).map(([svc, bal]) =>
                          [`Beginning UPB — ${svc}`, fmtUSDFull(bal)] as [string, string]
                        ),
                        ...Object.entries(report.structural_features.ending_upb_by_servicer).map(([svc, bal]) =>
                          [`Ending UPB — ${svc}`, fmtUSDFull(bal)] as [string, string]
                        ),
                        ["SOFR Fixing", fmtPct(report.structural_features.sofr_fixing)],
                        ["Severely Delinquent (90+ DPD)", fmtUSDFull(report.structural_features.severely_delinquent_balance)],
                        ["Gross Expected Interest", fmtUSDFull(report.structural_features.gross_expected_interest)],
                        ["Net Expected Interest", fmtUSDFull(report.structural_features.net_expected_interest)],
                      ]}
                    />
                  </div>
                )}
              </div>
            )}

            {/* ── ACCOUNTS (3) ───────────────────────────────────────── */}
            {activeTab === "accounts" && (
              <div className="space-y-6">
                <div>
                  <SectionTitle>3(a) Collections</SectionTitle>
                  <div className="grid lg:grid-cols-3 gap-3">
                    <div className="card py-3">
                      <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider mb-2">Principal Collections</p>
                      <dl className="space-y-1 text-xs">
                        {[
                          ["Scheduled Principal", report.principal_scheduled],
                          ["Curtailments", report.principal_curtailments],
                          ["Prepayments in Full", report.principal_prepayments_full],
                          ["Sales", report.principal_sales],
                          ["Liquidations", report.principal_liquidations],
                          ["Repurchases", report.principal_repurchases],
                          ["Recoveries", report.principal_recoveries],
                          ["Other", report.principal_other],
                        ].map(([lbl, v]) => (
                          <div key={lbl as string} className="flex justify-between">
                            <dt className="text-gray-600">{lbl}</dt>
                            <dd className="font-mono">{fmtUSD(v)}</dd>
                          </div>
                        ))}
                        <div className="flex justify-between pt-1 border-t border-gray-200 font-semibold">
                          <dt>Total Net Principal</dt>
                          <dd className="font-mono">{fmtUSD(report.principal_remittance_amount)}</dd>
                        </div>
                      </dl>
                    </div>
                    <div className="card py-3">
                      <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider mb-2">Interest Collections</p>
                      <dl className="space-y-1 text-xs">
                        {[
                          ["Gross Interest", report.gross_interest_collected],
                          ["Less: Servicing Fees", -n(report.servicing_fees_paid)],
                          ["Less: Deal Fees", -n(report.deal_fees_paid)],
                          ["Less: Deal Expenses", -n(report.deal_expenses_paid)],
                          ["Other Amounts", report.other_collections],
                        ].map(([lbl, v]) => (
                          <div key={lbl as string} className="flex justify-between">
                            <dt className="text-gray-600">{lbl}</dt>
                            <dd className="font-mono">{fmtUSD(v)}</dd>
                          </div>
                        ))}
                        <div className="flex justify-between pt-1 border-t border-gray-200 font-semibold">
                          <dt>Total Net Interest</dt>
                          <dd className="font-mono">{fmtUSD(report.interest_remittance_amount)}</dd>
                        </div>
                      </dl>
                    </div>
                    <div className="card py-3">
                      <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider mb-2">Total Available Funds</p>
                      <dl className="space-y-1 text-xs">
                        <div className="flex justify-between">
                          <dt className="text-gray-600">Principal Remittance</dt>
                          <dd className="font-mono">{fmtUSD(report.principal_remittance_amount)}</dd>
                        </div>
                        <div className="flex justify-between">
                          <dt className="text-gray-600">Interest Remittance</dt>
                          <dd className="font-mono">{fmtUSD(report.interest_remittance_amount)}</dd>
                        </div>
                        <div className="flex justify-between pt-1 border-t border-gray-200 font-semibold text-base">
                          <dt>Available Funds</dt>
                          <dd className="font-mono">{fmtUSD(report.available_funds)}</dd>
                        </div>
                      </dl>
                    </div>
                  </div>
                </div>

                {(report.reserve_accounts ?? []).length > 0 && (
                  <div>
                    <SectionTitle>3(b) Reserve Accounts</SectionTitle>
                    <DataTable
                      headers={["Account", "Beginning", "Deposits", "Withdrawals", "Ending", "Required"]}
                      rows={(report.reserve_accounts ?? []).map((a) => [
                        a.account_name,
                        fmtUSD(a.beginning_balance),
                        fmtUSD(a.deposits),
                        fmtUSD(a.withdrawals),
                        fmtUSD(a.ending_balance_post_payment),
                        a.required_balance != null ? fmtUSD(a.required_balance) : <span className="text-gray-400">—</span>,
                      ])}
                    />
                  </div>
                )}
              </div>
            )}

            {/* ── FEES (4) ───────────────────────────────────────────── */}
            {activeTab === "fees" && (
              <div className="space-y-6">
                <div>
                  <SectionTitle>Fees Paid</SectionTitle>
                  <DataTable
                    headers={["Fee Name", "Beg. Shortfall", "Current Due", "Total Due", "Amount Paid", "End. Shortfall"]}
                    rows={(report.fees_detail ?? []).map((f) => [
                      f.fee_name,
                      fmtUSD(f.beginning_shortfall),
                      fmtUSD(f.current_due),
                      fmtUSD(f.total_due),
                      <span className="text-green-700 font-semibold">{fmtUSD(f.amount_paid)}</span>,
                      n(f.ending_shortfall) > 0
                        ? <span className="text-red-600">{fmtUSD(f.ending_shortfall)}</span>
                        : <span className="text-gray-400">—</span>,
                    ])}
                    footer={[
                      "Total", "",
                      fmtUSD((report.fees_detail ?? []).reduce((s, f) => s + n(f.current_due), 0)),
                      fmtUSD((report.fees_detail ?? []).reduce((s, f) => s + n(f.total_due), 0)),
                      fmtUSD(report.total_fees),
                      fmtUSD((report.fees_detail ?? []).reduce((s, f) => s + n(f.ending_shortfall), 0)),
                    ]}
                  />
                </div>

                <div>
                  <SectionTitle>Expenses Paid</SectionTitle>
                  <DataTable
                    headers={["Expense Name", "Amount Due", "Amount Paid", "Shortfall"]}
                    rows={(report.expenses_detail ?? []).length === 0
                      ? [["—", fmtUSD(0), fmtUSD(0), fmtUSD(0)]]
                      : (report.expenses_detail ?? []).map((e) => [
                          e.expense_name,
                          fmtUSD(e.total_due),
                          <span className="text-green-700 font-semibold">{fmtUSD(e.amount_paid)}</span>,
                          n(e.ending_shortfall) > 0
                            ? <span className="text-red-600">{fmtUSD(e.ending_shortfall)}</span>
                            : <span className="text-gray-400">{fmtUSD(0)}</span>,
                        ])}
                  />
                </div>

                <div>
                  <SectionTitle>Reserve Account Balances</SectionTitle>
                  <DataTable
                    headers={["Account Name", "Beginning Balance", "Deposits This Period", "Withdrawals This Period", "Ending Balance"]}
                    rows={(report.reserve_accounts ?? []).length === 0
                      ? [["—", fmtUSD(0), fmtUSD(0), fmtUSD(0), fmtUSD(0)]]
                      : (report.reserve_accounts ?? []).map((a) => [
                          a.account_name,
                          fmtUSD(a.beginning_balance),
                          <span className="text-green-700">{fmtUSD(a.deposits)}</span>,
                          <span className="text-amber-700">{fmtUSD(a.withdrawals)}</span>,
                          <span className="font-semibold">{fmtUSD(a.ending_balance_post_payment)}</span>,
                        ])}
                  />
                </div>
              </div>
            )}

            {/* ── EXPENSES (5) ───────────────────────────────────────── */}
            {activeTab === "expenses" && (
              <div className="space-y-3">
                {(report.expenses_detail ?? []).length === 0 ? (
                  <EmptyState
                    icon={Layers}
                    title="No expense items"
                    description="Indemnification + trust expense rows render here when itemized in the indenture."
                  />
                ) : (
                  <DataTable
                    headers={["Expense Name", "Beg. Shortfall", "Current Due", "Total Due", "Amount Paid", "End. Shortfall"]}
                    rows={(report.expenses_detail ?? []).map((e) => [
                      e.expense_name,
                      fmtUSD(e.beginning_shortfall),
                      fmtUSD(e.current_due),
                      fmtUSD(e.total_due),
                      fmtUSD(e.amount_paid),
                      n(e.ending_shortfall) > 0
                        ? <span className="text-red-600">{fmtUSD(e.ending_shortfall)}</span>
                        : <span className="text-gray-400">—</span>,
                    ])}
                  />
                )}
              </div>
            )}

            {/* ── EVENTS (6) ─────────────────────────────────────────── */}
            {activeTab === "events" && (
              <div className="space-y-3">
                {(report.events ?? []).length === 0 ? (
                  <EmptyState icon={Activity} title="No events" description="No trigger tests configured." />
                ) : (
                  <div className="space-y-2">
                    {(report.events ?? []).map((e) => {
                      const passed = e.status === "Pass" || e.status === "Eligible";
                      return (
                        <div key={e.test_name} className={`flex items-center justify-between p-4 rounded-xl border ${passed ? "bg-green-50 border-green-200" : "bg-red-50 border-red-200"}`}>
                          <div>
                            <p className="text-sm font-semibold text-gray-800">{e.test_name}</p>
                            <p className="text-xs text-gray-500 mt-0.5">{e.description}</p>
                            <p className="text-xs text-gray-500 mt-1">
                              Value: <span className="font-mono font-semibold">{Math.abs(e.current_value) > 100 ? fmtUSD(e.current_value) : fmtPct(e.current_value, 4)}</span>
                              &nbsp;·&nbsp; Threshold: <span className="font-mono font-semibold">{Math.abs(e.threshold) > 100 ? fmtUSD(e.threshold) : fmtPct(e.threshold, 4)}</span>
                            </p>
                          </div>
                          <span className={`badge text-sm px-3 py-1 ${passed ? "badge-green" : "badge-red"}`}>
                            {passed ? "✓ Pass" : "✗ Fail"}
                          </span>
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            )}

            {/* ── PRIORITY (8 + 7 servicer balances) ─────────────────── */}
            {activeTab === "priority" && (
              <div className="space-y-6">
                {/* Servicer balances (7) */}
                {(report.servicer_balances ?? []).length > 0 && (
                  <div>
                    <SectionTitle>7. Servicer Balances</SectionTitle>
                    <DataTable
                      headers={["Servicer", "Beginning UPB", "Ending UPB", "Servicing Fee", "Loan Count"]}
                      rows={(report.servicer_balances ?? []).map((s) => [
                        s.servicer_name,
                        fmtUSD(s.beginning_upb),
                        fmtUSD(s.ending_upb),
                        fmtUSD(s.servicing_fee),
                        fmt(s.loan_count, 0),
                      ])}
                    />
                  </div>
                )}

                {/* 8. Priority of Payments */}
                <div>
                  <SectionTitle>8. Priority of Payments</SectionTitle>

                  {report.active_trigger_branch && (
                    <div className="card py-3 mb-3">
                      <p className="text-[10px] text-gray-400 font-semibold uppercase tracking-wider">Active Branch</p>
                      <p className="text-base font-bold text-primary-700 mt-1">{report.active_trigger_branch}</p>
                    </div>
                  )}

                  {/* Distribution allocation chart */}
                  {(report.distribution_allocation ?? []).length > 0 && (
                    <div className="card mb-3 py-3" style={{ height: 280 }}>
                      <ResponsiveContainer width="100%" height="100%">
                        <BarChart
                          data={(report.distribution_allocation ?? []).map((a) => ({
                            name: a.bucket, amount: n(a.amount) / 1_000_000,
                          }))}
                          layout="vertical"
                          margin={{ top: 10, right: 30, left: 60, bottom: 5 }}
                        >
                          <CartesianGrid strokeDasharray="3 3" />
                          <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={(v: number) => `$${v.toFixed(1)}M`} />
                          <YAxis dataKey="name" type="category" tick={{ fontSize: 10 }} width={120} />
                          <Tooltip formatter={(v: number) => `$${v.toFixed(2)}M`} />
                          <Bar dataKey="amount" fill={COLOR_PRIMARY} name="Distribution ($MM)" />
                        </BarChart>
                      </ResponsiveContainer>
                    </div>
                  )}

                  {([
                    { title: "Interest Priorities",
                      steps: report.waterfall_trace_interest },
                    { title: "Principal — Trigger Not In Effect" + (report.active_trigger_branch === "Trigger Not In Effect" ? " (ACTIVE)" : ""),
                      steps: report.waterfall_trace_principal_no_trigger },
                    { title: "Principal — Trigger In Effect" + (report.active_trigger_branch === "Trigger In Effect" ? " (ACTIVE)" : ""),
                      steps: report.waterfall_trace_principal_with_trigger },
                    { title: "Monthly Excess Cashflow",
                      steps: report.waterfall_trace_excess },
                  ] as { title: string; steps: WaterfallResult["waterfall_trace_interest"] }[]).map(({ title, steps }) => {
                    const arr = steps ?? [];
                    if (arr.length === 0) return null;
                    const isActive = title.includes("(ACTIVE)");
                    return (
                      <div key={title} className="mb-3">
                        <h4 className={`text-xs font-semibold mb-2 ${isActive ? "text-primary-700" : "text-gray-600"}`}>{title}</h4>
                        <DataTable
                          headers={["Priority", "Description", "Class", "Funds Avail.", "Owed", "Paid"]}
                          rows={arr.map((s) => [
                            `(${s.step})`,
                            <span className="text-left">{(s.description ?? "").slice(0, 80)}</span>,
                            s.class_name ?? "",
                            fmtUSD(s.funds_available),
                            fmtUSD(s.amount_owed),
                            fmtUSD(s.amount_paid),
                          ])}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            )}

            {/* ── LOAN DETAILS (9) ───────────────────────────────────── */}
            {activeTab === "loans" && (
              <div className="space-y-6">
                {([
                  { title: "9(a) Paid In Full", loans: report.loans_paid_in_full },
                  { title: "9(b) REO", loans: report.loans_reo },
                  { title: "9(c) Foreclosure", loans: report.loans_foreclosure },
                  { title: "9(d) Bankruptcy", loans: report.loans_bankruptcy },
                  { title: "9(e) Modifications", loans: report.loans_modified },
                ] as { title: string; loans: WaterfallResult["loans_paid_in_full"] }[]).map(({ title, loans }) => {
                  const arr = loans ?? [];
                  return (
                    <div key={title}>
                      <SectionTitle>{`${title} (${arr.length})`}</SectionTitle>
                      {arr.length === 0 ? (
                        <p className="text-xs text-gray-400 italic px-2">No loans to report</p>
                      ) : (
                        <DataTable
                          headers={["Loan ID", "Beginning Principal", "Ending Principal"]}
                          rows={arr.slice(0, 100).map((l) => [
                            l.loan_id,
                            fmtUSD(l.beginning_principal),
                            fmtUSD(l.ending_principal),
                          ])}
                        />
                      )}
                    </div>
                  );
                })}
                <div>
                  <SectionTitle>{`9(f) Forbearance (${(report.loans_forbearance ?? []).length})`}</SectionTitle>
                  {(report.loans_forbearance ?? []).length === 0 ? (
                    <p className="text-xs text-gray-400 italic px-2">No loans to report</p>
                  ) : (
                    <DataTable
                      headers={["Loan ID", "Deferred Amount", "Cumulative Deferred"]}
                      rows={(report.loans_forbearance ?? []).slice(0, 100).map((l) => [
                        l.loan_id,
                        fmtUSD(l.deferred_amount),
                        fmtUSD(l.cumulative_deferred),
                      ])}
                    />
                  )}
                </div>
              </div>
            )}

          </div>
        )}
      </main>
    </div>
  );
}
