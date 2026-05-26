import { useState, useEffect } from "react";
import {
  ClipboardList, ChevronDown, ChevronUp, ShieldCheck,
  Clock, Calendar, Building2, Layers, DollarSign,
  Activity, AlertCircle, RefreshCw,
} from "lucide-react";
import Header from "../components/layout/Header";
import LoadingSpinner from "../components/shared/LoadingSpinner";
import { listDeals, getDeal } from "../services/api";
import toast from "react-hot-toast";
import type { DealConfig } from "../types";

const n = (v: unknown): number =>
  v == null || isNaN(Number(v)) || !isFinite(Number(v)) ? 0 : Number(v);
const fmt = (v: unknown, dec = 2) =>
  n(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec });
const fmtM = (v: unknown) => `$${(n(v) / 1_000_000).toFixed(2)}M`;
const fmtPct = (v: unknown) => v != null ? `${(n(v) * 100).toFixed(4)}%` : "—";

function Section({
  title, count, open, onToggle, children,
}: {
  title: string; count?: number; open: boolean; onToggle: () => void; children: React.ReactNode;
}) {
  return (
    <div className="card">
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-1 py-1"
      >
        <span className="text-sm font-semibold text-gray-700 flex items-center gap-2">
          {title}
          {count !== undefined && (
            <span className="bg-primary-100 text-primary-700 text-xs font-bold px-2 py-0.5 rounded-full">
              {count}
            </span>
          )}
        </span>
        {open
          ? <ChevronUp size={15} className="text-gray-400" />
          : <ChevronDown size={15} className="text-gray-400" />}
      </button>
      {open && <div className="mt-4">{children}</div>}
    </div>
  );
}

function Field({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div>
      <p className="text-[10px] font-semibold text-gray-400 uppercase tracking-wider mb-0.5">{label}</p>
      <p className="text-sm text-gray-800 font-medium break-words">{value ?? "—"}</p>
    </div>
  );
}

export default function DealConfigReviewPage() {
  interface DealSummary { deal_id: string; deal_name?: string; asset_type?: string; manually_verified?: boolean; }

  const [dealSummaries, setDealSummaries] = useState<DealSummary[]>([]);
  const [selectedDeal, setSelectedDeal] = useState("");
  const [config, setConfig] = useState<DealConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState({
    info: true, classes: true, fees: false, interest: false, principal: false, excess: false, triggers: false,
  });
  const toggle = (k: keyof typeof open) => setOpen((s) => ({ ...s, [k]: !s[k] }));

  // Load deal list
  useEffect(() => {
    listDeals()
      .then((d) => {
        // API returns array of DealSummary objects or plain id strings
        const raw: unknown[] = Array.isArray(d) ? d : (d.deals ?? d.deal_ids ?? []);
        const summaries: DealSummary[] = raw.map((item) =>
          typeof item === "string"
            ? { deal_id: item }
            : (item as DealSummary)
        );
        setDealSummaries(summaries);
        if (summaries.length === 1) setSelectedDeal(summaries[0].deal_id);
      })
      .catch(() => toast.error("Failed to load deal list"));
  }, []);

  // Load config when deal selected
  useEffect(() => {
    if (!selectedDeal) return;
    setLoading(true);
    setConfig(null);
    getDeal(selectedDeal)
      .then((d) => setConfig(d as DealConfig))
      .catch(() => toast.error(`Failed to load config for ${selectedDeal}`))
      .finally(() => setLoading(false));
  }, [selectedDeal]);

  const refresh = () => {
    if (!selectedDeal) return;
    setLoading(true);
    setConfig(null);
    getDeal(selectedDeal)
      .then((d) => setConfig(d as DealConfig))
      .catch(() => toast.error("Refresh failed"))
      .finally(() => setLoading(false));
  };

  const classes = config?.classes ?? [];
  const fees = config?.fees ?? [];
  const interestWF = config?.interest_waterfall ?? [];
  const principalWF = config?.principal_waterfall ?? [];
  const excessWF = config?.excess_cashflow_waterfall ?? [];
  const triggers = config?.triggers ?? [];

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <Header
        title="Deal Config Review"
        subtitle="Browse extracted and verified deal configurations"
      />

      <main className="flex-1 overflow-y-auto p-6 space-y-5">
        {/* Deal selector */}
        <div className="card">
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex-1 min-w-[200px]">
              <label className="label flex items-center gap-1.5">
                <ClipboardList size={13} /> Select Deal
              </label>
              <select
                className="input"
                value={selectedDeal}
                onChange={(e) => setSelectedDeal(e.target.value)}
              >
                <option value="">— Choose a deal —</option>
                {dealSummaries.map((s) => (
                  <option key={s.deal_id} value={s.deal_id}>
                    {s.deal_id}{s.deal_name ? ` — ${s.deal_name}` : ""}
                    {s.manually_verified ? " ✓" : " (draft)"}
                  </option>
                ))}
              </select>
            </div>

            {selectedDeal && (
              <button
                className="btn-secondary h-[38px]"
                onClick={refresh}
                disabled={loading}
                title="Refresh"
              >
                <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
                Refresh
              </button>
            )}
          </div>
        </div>

        {/* Loading */}
        {loading && (
          <div className="card flex items-center justify-center py-12">
            <LoadingSpinner text={`Loading ${selectedDeal} config…`} />
          </div>
        )}

        {/* No deals saved yet */}
        {!loading && dealSummaries.length === 0 && (
          <div className="card text-center py-12 text-gray-400">
            <ClipboardList size={36} className="mx-auto mb-3 opacity-40" />
            <p className="font-medium">No deal configs saved yet.</p>
            <p className="text-xs mt-1">Extract a deal indenture PDF from the Deal Indenture tab first.</p>
          </div>
        )}

        {/* Config display */}
        {config && !loading && (
          <div className="space-y-4 fade-in">

            {/* Status bar */}
            <div className={`flex items-center justify-between px-4 py-3 rounded-xl border ${
              config.manually_verified
                ? "bg-green-50 border-green-200"
                : "bg-amber-50 border-amber-200"
            }`}>
              <div className="flex items-center gap-2">
                {config.manually_verified
                  ? <ShieldCheck size={16} className="text-green-600" />
                  : <AlertCircle size={16} className="text-amber-500" />}
                <span className={`text-sm font-semibold ${config.manually_verified ? "text-green-800" : "text-amber-800"}`}>
                  {config.manually_verified
                    ? `Verified by ${config.verified_by ?? "Admin"}`
                    : "Draft — Pending Verification"}
                </span>
              </div>
              <div className="flex items-center gap-4 text-xs text-gray-500">
                {config.verified_at && (
                  <span className="flex items-center gap-1">
                    <ShieldCheck size={11} /> Verified: {config.verified_at.slice(0, 10)}
                  </span>
                )}
                <span className="flex items-center gap-1">
                  <Clock size={11} /> Updated: {(config.updated_at ?? "").slice(0, 10)}
                </span>
                <span className="flex items-center gap-1 bg-gray-100 px-2 py-0.5 rounded-full font-medium text-gray-600">
                  Source: {config.extraction_source ?? "unknown"}
                </span>
              </div>
            </div>

            {/* Quick stats */}
            <div className="grid grid-cols-2 sm:grid-cols-4 gap-4">
              {[
                { icon: Layers, label: "Classes", value: classes.length },
                { icon: DollarSign, label: "Pool Balance", value: fmtM(config.original_pool_balance) },
                { icon: Building2, label: "Asset Type", value: config.asset_type ?? "—" },
                { icon: Activity, label: "Benchmark", value: `${config.benchmark ?? "SOFR"} ${config.benchmark_tenor ?? "1M"}` },
              ].map(({ icon: Icon, label, value }) => (
                <div key={label} className="card py-3 flex items-center gap-3">
                  <div className="w-8 h-8 bg-primary-50 rounded-lg flex items-center justify-center flex-shrink-0">
                    <Icon size={15} className="text-primary-600" />
                  </div>
                  <div>
                    <p className="text-[10px] text-gray-400 uppercase font-semibold tracking-wider">{label}</p>
                    <p className="text-sm font-bold text-gray-800">{value}</p>
                  </div>
                </div>
              ))}
            </div>

            {/* Deal Information */}
            <Section title="Deal Information" open={open.info} onToggle={() => toggle("info")}>
              <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-x-6 gap-y-4">
                <Field label="Deal Name" value={config.deal_name} />
                <Field label="Deal ID" value={config.deal_id} />
                <Field label="Series" value={config.series} />
                <Field label="Issuing Entity" value={config.issuing_entity} />
                <Field label="Depositor" value={config.depositor} />
                <Field label="Closing Date" value={config.closing_date} />
                <Field label="Cut-off Date" value={config.cut_off_date} />
                <Field label="First Payment Date" value={config.first_payment_date} />
                <Field label="Legal Maturity" value={config.legal_maturity_date} />
                <Field label="Payment Frequency" value={config.payment_frequency} />
                <Field label="Lien Position" value={config.lien_position} />
                <Field label="Interest Day Count" value={config.interest_day_count} />
                <Field label="Cleanup Call" value={config.cleanup_call_pct ? `${(config.cleanup_call_pct * 100).toFixed(0)}%` : "—"} />
                <Field label="Revolving Period" value={config.revolving_period ? `Yes (ends ${config.revolving_period_end_date ?? "?"})` : "No"} />
                <Field
                  label="Sponsors"
                  value={(config.sponsors ?? []).join(", ") || "—"}
                />
                <Field
                  label="Rating Agencies"
                  value={(config.rating_agencies ?? []).join(", ") || "—"}
                />
              </div>

              {/* Servicers */}
              {(config.servicers ?? []).length > 0 && (
                <div className="mt-4 pt-4 border-t border-gray-100">
                  <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Servicers</p>
                  <div className="flex flex-wrap gap-3">
                    {(config.servicers ?? []).map((svc, i) => (
                      <div key={i} className="bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 text-xs">
                        <p className="font-semibold text-gray-700">{svc.servicer_name}</p>
                        <p className="text-gray-500 font-mono">{fmtPct(svc.servicing_fee_rate)} p.a.</p>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </Section>

            {/* Certificate Classes */}
            <Section title="Certificate Classes" count={classes.length} open={open.classes} onToggle={() => toggle("classes")}>
              <div className="overflow-x-auto">
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {["Class", "Type", "CUSIP", "Original Balance", "Rate Type", "Rate / Margin", "Cap", "Priority (I/P)", "Method", "Fitch", "KBRA"].map((h) => (
                        <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {classes.map((cls, i) => (
                      <tr key={cls.class_name} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                        <td className="px-3 py-2 font-semibold text-primary-700">{cls.class_name}</td>
                        <td className="px-3 py-2">
                          <span className={`badge text-xs ${
                            cls.type === "Senior" ? "badge-green" :
                            cls.type === "Mezzanine" ? "badge-blue" :
                            cls.type === "Residual" ? "badge-yellow" : "bg-gray-100 text-gray-600"
                          }`}>{cls.type}</span>
                        </td>
                        <td className="px-3 py-2 font-mono text-xs text-gray-500">{cls.cusip ?? "—"}</td>
                        <td className="px-3 py-2 font-mono font-medium">{cls.initial_principal > 0 ? fmtM(cls.initial_principal) : "Notional"}</td>
                        <td className="px-3 py-2 capitalize text-xs">{cls.interest_rate_type}</td>
                        <td className="px-3 py-2 font-mono text-xs">
                          {cls.interest_rate_type === "fixed"
                            ? fmtPct(cls.fixed_rate)
                            : cls.margin != null
                            ? `${fmtPct(cls.margin)} + ${cls.benchmark ?? "SOFR"}`
                            : "—"}
                        </td>
                        <td className="px-3 py-2 font-mono text-xs">{cls.rate_cap != null ? fmtPct(cls.rate_cap) : "Net WAC"}</td>
                        <td className="px-3 py-2 font-mono text-xs">{cls.interest_priority} / {cls.principal_priority}</td>
                        <td className="px-3 py-2 capitalize text-xs">{cls.principal_method ?? "—"}</td>
                        <td className="px-3 py-2 text-xs">{cls.fitch_rating ?? "—"}</td>
                        <td className="px-3 py-2 text-xs">{cls.kbra_rating ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                  <tfoot>
                    <tr className="bg-gray-50 border-t border-gray-200">
                      <td colSpan={3} className="px-3 py-2 text-xs font-semibold text-gray-600">Total</td>
                      <td className="px-3 py-2 font-mono font-bold text-gray-800 text-sm">
                        {fmtM(classes.filter(c => !c.is_notional && !c.is_residual).reduce((s, c) => s + n(c.initial_principal), 0))}
                      </td>
                      <td colSpan={7} />
                    </tr>
                  </tfoot>
                </table>
              </div>
            </Section>

            {/* Fees */}
            <Section title="Fees & Expenses" count={fees.length} open={open.fees} onToggle={() => toggle("fees")}>
              {fees.length === 0
                ? <p className="text-sm text-gray-400 text-center py-4">No fees extracted</p>
                : (
                  <div className="overflow-x-auto">
                    <table className="w-full text-sm">
                      <thead>
                        <tr className="table-header">
                          {["Priority", "Fee Name", "Type", "Rate / Amount", "Applies To", "Servicer"].map((h) => (
                            <th key={h} className="px-3 py-2.5 text-left text-xs font-semibold first:rounded-tl-lg last:rounded-tr-lg">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {[...fees].sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99)).map((fee, i) => (
                          <tr key={fee.fee_name} className={i % 2 === 0 ? "table-row-even" : "table-row-odd"}>
                            <td className="px-3 py-2 text-center">
                              <span className="w-6 h-6 bg-primary-100 text-primary-700 rounded-full flex items-center justify-center text-xs font-bold mx-auto">
                                {fee.priority}
                              </span>
                            </td>
                            <td className="px-3 py-2 font-medium">{fee.fee_name}</td>
                            <td className="px-3 py-2 capitalize text-xs">{fee.fee_type}</td>
                            <td className="px-3 py-2 font-mono text-xs">
                              {fee.fee_type === "percentage"
                                ? fmtPct(fee.fee_rate)
                                : fee.fixed_amount != null ? `$${fmt(fee.fixed_amount)}/yr` : "—"}
                            </td>
                            <td className="px-3 py-2 text-xs text-gray-500">{fee.applies_to ?? "—"}</td>
                            <td className="px-3 py-2 text-xs text-gray-500">{fee.servicer_name ?? "—"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
            </Section>

            {/* Waterfall Steps */}
            {(
              [
                { key: "interest" as const, label: "Interest Waterfall", steps: interestWF, color: "badge-blue" },
                { key: "principal" as const, label: "Principal Waterfall", steps: principalWF, color: "badge-green" },
                { key: "excess" as const, label: "Excess Cashflow Waterfall", steps: excessWF, color: "badge-yellow" },
              ] as const
            ).map(({ key, label, steps, color }) => (
              <Section key={key} title={label} count={steps.length} open={open[key]} onToggle={() => toggle(key)}>
                {steps.length === 0
                  ? <p className="text-xs text-gray-400 py-3 text-center">No steps extracted</p>
                  : (
                    <div className="space-y-1.5">
                      {steps.map((s) => (
                        <div key={s.step} className="flex items-start gap-3 px-3 py-2 rounded-lg hover:bg-gray-50 transition-colors">
                          <span className="flex-shrink-0 w-7 h-7 bg-primary-100 text-primary-700 rounded-full flex items-center justify-center text-xs font-bold">
                            {s.step}
                          </span>
                          <div className="flex-1 min-w-0">
                            <p className="text-sm text-gray-700 leading-snug">{s.description}</p>
                            {s.amount_formula && (
                              <p className="text-[10px] font-mono text-purple-600 bg-purple-50 px-2 py-0.5 rounded mt-1 inline-block">
                                ƒ {s.amount_formula}
                              </p>
                            )}
                            <div className="flex flex-wrap gap-1.5 mt-1">
                              {s.class_name && <span className={`badge text-[10px] ${color}`}>{s.class_name}</span>}
                              <span className="badge bg-gray-100 text-gray-600 text-[10px]">{s.payment_type}</span>
                              {s.source_bucket && <span className="badge bg-gray-100 text-gray-500 text-[10px]">{s.source_bucket}</span>}
                              {s.condition && s.condition !== "always" && (
                                <span className="badge bg-orange-50 text-orange-700 text-[10px]">{s.condition}</span>
                              )}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
              </Section>
            ))}

            {/* Trigger Tests */}
            {triggers.length > 0 && (
              <Section title="Trigger Tests" count={triggers.length} open={open.triggers} onToggle={() => toggle("triggers")}>
                <div className="space-y-2">
                  {triggers.map((t, i) => (
                    <div key={i} className="flex items-start gap-3 p-3 bg-gray-50 rounded-lg border border-gray-100">
                      <AlertCircle size={15} className="text-amber-500 flex-shrink-0 mt-0.5" />
                      <div className="min-w-0 flex-1">
                        <p className="text-sm font-semibold text-gray-700">{t.test_name}</p>
                        <p className="text-xs text-gray-500 mt-0.5">{t.description}</p>
                        <div className="flex gap-2 mt-1">
                          <span className="badge bg-gray-100 text-gray-600 text-[10px]">{t.test_type}</span>
                        </div>
                        {(t.trigger_condition || t.trigger_action) && (
                          <code className="block mt-2 text-xs bg-gray-100 px-2 py-1.5 rounded font-mono text-gray-800 whitespace-pre">
                            {`if ${t.trigger_condition || "…"}:\n    ${t.trigger_action || "…"} = True`}
                          </code>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* Loss Allocation */}
            {(config.loss_allocation_order ?? []).length > 0 && (
              <div className="card">
                <p className="text-sm font-semibold text-gray-700 mb-3 flex items-center gap-2">
                  <Calendar size={14} className="text-primary-600" /> Loss Allocation Order
                </p>
                <p className="text-xs text-gray-500 mb-2">Losses absorbed from most subordinate → most senior:</p>
                <div className="flex flex-wrap gap-2">
                  {(config.loss_allocation_order ?? []).map((cls, i) => (
                    <div key={cls} className="flex items-center gap-1">
                      <span className="badge bg-red-50 text-red-700 font-mono">{cls}</span>
                      {i < (config.loss_allocation_order ?? []).length - 1 && (
                        <span className="text-gray-300 text-xs">→</span>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}
      </main>
    </div>
  );
}
