import React, { useState, useEffect, useCallback } from "react";
import {
  Edit3, Save, CheckCircle2, X, ChevronDown, ChevronUp,
  ShieldCheck, Plus, Trash2, ArrowUp, ArrowDown, Mail, Loader2,
  PanelRightOpen, Sparkles, RefreshCw,
} from "lucide-react";
import type {
  DealConfig, CertificateClass, FeeConfig, WaterfallStep,
  ServicerConfig, TriggerTest, ReserveAccount,
} from "../../types";
import { submitReview, getAuditAnnotations, addAuditEntry, dealPdfUrl, generateTriggerExpression } from "../../services/api";
import toast from "react-hot-toast";
import PDFVerificationPanel from "./PDFVerificationPanel";
import { editSectionToUi, valuesForSection } from "./pdfSectionValues";

// ─── Types ────────────────────────────────────────────────────────────────────

interface AnnotEntry {
  sender: string;
  content: string;
  created_at: string;
}

type AnnotationsMap = Record<string, AnnotEntry[]>;

interface AnnotPanelState {
  key: string;
  newSender: string;
  newContent: string;
  saving: boolean;
}

// ─── Formatting helpers ───────────────────────────────────────────────────────

const n = (v: unknown): number =>
  v == null || isNaN(v as number) || !isFinite(v as number) ? 0 : Number(v);
const fmtNum = (v: number | null | undefined, dec = 0) =>
  v != null ? n(v).toLocaleString("en-US", { minimumFractionDigits: dec, maximumFractionDigits: dec }) : "—";
const fmtPctOrDash = (v: number | null | undefined) =>
  v != null && n(v) !== 0 ? `${(n(v) * 100).toFixed(5)}%` : "—";
const toPct = (v: number | null | undefined): string =>
  v != null && v !== 0 ? (n(v) * 100).toFixed(5) : "";
const fromPct = (s: string): number | null =>
  s.trim() === "" ? null : parseFloat(s) / 100;

// ─── Module-level primitives ──────────────────────────────────────────────────

function TxtInput({ value, onChange, className = "" }: { value: string; onChange: (v: string) => void; className?: string }) {
  return <input className={`input text-sm py-1 px-2 ${className}`} value={value} onChange={(e) => onChange(e.target.value)} />;
}
function NumInput({
  value,
  onChange,
  step = "any",
  min,
  className = "",
}: {
  value: string | number | null | undefined;
  onChange: (v: string) => void;
  step?: string;
  min?: string;
  className?: string;
}) {
  const [draft, setDraft] = useState<string>("");
  const [error, setError] = useState<string>("");
  const [isFocused, setIsFocused] = useState<boolean>(false);
  const resolvedValue = value == null ? "" : String(value);
  const visibleValue = isFocused ? draft : resolvedValue;

  const isPartialNumber = (v: string) => /^-?\d*\.?\d*$/.test(v);
  const isCompleteNumber = (v: string) => /^-?(?:\d+\.?\d*|\.\d+)$/.test(v);

  const validateMin = (num: number) => {
    if (min == null || min === "") return true;
    const minNum = Number(min);
    return Number.isFinite(minNum) ? num >= minNum : true;
  };

  const handleChange = (next: string) => {
    if (!isPartialNumber(next)) {
      setError("Enter numbers only.");
      return;
    }

    setDraft(next);

    if (next === "" || next === "-" || next === "." || next === "-." || next.endsWith(".")) {
      setError("");
      if (next === "") onChange("");
      return;
    }

    const parsed = Number(next);
    if (!Number.isFinite(parsed)) {
      setError("Enter a valid number.");
      return;
    }
    if (!validateMin(parsed)) {
      setError(`Value must be >= ${min}.`);
      return;
    }

    setError("");
    onChange(next);
  };

  const handleBlur = () => {
    const trimmed = draft.trim();

    if (trimmed === "") {
      setError("");
      onChange("");
      setIsFocused(false);
      return;
    }

    if (!isCompleteNumber(trimmed)) {
      setError("Enter a valid number.");
      setIsFocused(false);
      return;
    }

    const parsed = Number(trimmed);
    if (!Number.isFinite(parsed)) {
      setError("Enter a valid number.");
      setIsFocused(false);
      return;
    }
    if (!validateMin(parsed)) {
      setError(`Value must be >= ${min}.`);
      setIsFocused(false);
      return;
    }

    setError("");
    onChange(trimmed);
    setIsFocused(false);
  };

  return (
    <div className="w-full">
      <input
        type="text"
        inputMode="decimal"
        step={step}
        min={min}
        className={`input text-sm py-1 px-2 ${error ? "border-red-500 focus:ring-red-500 focus:border-red-500" : ""} ${className}`}
        value={visibleValue}
        onFocus={() => {
          setDraft(resolvedValue);
          setError("");
          setIsFocused(true);
        }}
        onBlur={handleBlur}
        onChange={(e) => handleChange(e.target.value)}
        aria-invalid={error ? "true" : "false"}
      />
      {error && <p className="mt-1 text-[10px] text-red-600">{error}</p>}
    </div>
  );
}
function SelectInput({ value, onChange, options, className = "" }: { value: string; onChange: (v: string) => void; options: string[]; className?: string }) {
  return (
    <select className={`input text-sm py-1 px-2 ${className}`} value={value} onChange={(e) => onChange(e.target.value)}>
      {options.map((o) => <option key={o} value={o}>{o}</option>)}
    </select>
  );
}
function TA({ value, onChange, rows = 2, className = "" }: { value: string; onChange: (v: string) => void; rows?: number; className?: string }) {
  return <textarea rows={rows} className={`input text-xs py-1 px-2 font-mono resize-y ${className}`} value={value} onChange={(e) => onChange(e.target.value)} />;
}
function FieldGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="text-xs text-gray-400 font-medium uppercase tracking-wider mb-0.5">{label}</p>
      {children}
    </div>
  );
}
function ViewVal({ val }: { val?: string | number | null }) {
  return <p className="text-sm font-medium text-gray-800">{val != null && val !== "" ? String(val) : "—"}</p>;
}
function AddRowBtn({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button type="button" onClick={onClick} className="mt-3 flex items-center gap-1.5 text-xs text-primary-600 hover:text-primary-800 font-medium">
      <Plus size={13} /> {label}
    </button>
  );
}
function DelBtn({ onClick }: { onClick: () => void }) {
  return <button type="button" onClick={onClick} className="p-1 text-red-400 hover:text-red-600 rounded transition-colors" title="Remove"><Trash2 size={14} /></button>;
}
function MoveBtn({ dir, onClick }: { dir: "up" | "down"; onClick: () => void }) {
  return <button type="button" onClick={onClick} className="p-1 text-gray-400 hover:text-gray-600 rounded transition-colors">{dir === "up" ? <ArrowUp size={13} /> : <ArrowDown size={13} />}</button>;
}

// ─── Section header with per-section edit controls ───────────────────────────

interface SectionHeaderProps {
  title: string;
  open: boolean;
  onToggle: () => void;
  isEditing: boolean;
  saving: boolean;
  onEdit: () => void;
  onSave: () => void;
  onDiscard: () => void;
  canEdit?: boolean;   // false while another section is being edited
}

function SectionHeader({ title, open, onToggle, isEditing, saving, onEdit, onSave, onDiscard, canEdit = true }: SectionHeaderProps) {
  return (
    <div className="flex items-stretch bg-gray-50 border border-gray-200 rounded-lg overflow-hidden">
      <button
        onClick={onToggle}
        className="flex-1 flex items-center justify-between px-4 py-3 hover:bg-gray-100 transition-colors text-left min-w-0"
      >
        <span className="text-sm font-semibold text-gray-700 truncate">{title}</span>
        {open ? <ChevronUp size={16} className="text-gray-400 flex-shrink-0 ml-2" /> : <ChevronDown size={16} className="text-gray-400 flex-shrink-0 ml-2" />}
      </button>

      {/* Edit controls — separated by a divider */}
      <div className="border-l border-gray-200 px-3 flex items-center gap-1.5 flex-shrink-0">
        {isEditing ? (
          <>
            <button
              onClick={(e) => { e.stopPropagation(); onDiscard(); }}
              disabled={saving}
              className="text-xs text-gray-500 hover:text-gray-700 px-2.5 py-1.5 rounded-md border border-gray-300 hover:bg-gray-100 transition-colors disabled:opacity-50"
            >
              Discard
            </button>
            <button
              onClick={(e) => { e.stopPropagation(); onSave(); }}
              disabled={saving}
              className="text-xs text-white bg-primary-700 hover:bg-primary-800 px-2.5 py-1.5 rounded-md transition-colors disabled:opacity-50 flex items-center gap-1"
            >
              {saving ? <><Loader2 size={11} className="animate-spin" />Saving…</> : <><Save size={11} />Save</>}
            </button>
          </>
        ) : (
          <button
            onClick={(e) => { e.stopPropagation(); if (canEdit) onEdit(); }}
            disabled={!canEdit}
            title={canEdit ? "Edit this section" : "Save or discard the current section first"}
            className="flex items-center gap-1 text-xs text-gray-400 hover:text-primary-600 px-2 py-1.5 rounded-md hover:bg-primary-50 transition-colors disabled:opacity-30 disabled:cursor-not-allowed"
          >
            <Edit3 size={13} />
            <span className="hidden sm:inline text-xs">Edit</span>
          </button>
        )}
      </div>
    </div>
  );
}

// ─── Audit annotation panel ───────────────────────────────────────────────────

interface AnnotPanelProps {
  entries: AnnotEntry[];
  panelState: AnnotPanelState;
  onChangeSender: (v: string) => void;
  onChangeContent: (v: string) => void;
  onAddEntry: () => void;
  onClose: () => void;
}

function AnnotationPanel({ entries, panelState, onChangeSender, onChangeContent, onAddEntry, onClose }: AnnotPanelProps) {
  return (
    <div className="bg-slate-50/80 border border-slate-200 rounded-lg px-4 py-3 space-y-3">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-1.5 text-slate-600 text-xs font-semibold">
          <Mail size={12} />
          {entries.length > 0
            ? `${entries.length} logged email change${entries.length > 1 ? "s" : ""}`
            : "Log email-instructed change"}
        </div>
        <button onClick={onClose} className="text-slate-400 hover:text-slate-600 p-0.5 rounded transition-colors">
          <X size={12} />
        </button>
      </div>

      {/* Existing entries */}
      {entries.map((e, i) => (
        <div key={i} className="bg-white border border-slate-200 rounded-md px-3 py-2 space-y-1 text-xs">
          <div className="flex items-center gap-2 text-slate-700 font-semibold">
            <span className="inline-flex w-4 h-4 bg-slate-200 text-slate-600 rounded-full items-center justify-center text-[9px] flex-shrink-0">{i + 1}</span>
            <span className="truncate">{e.sender}</span>
            <span className="ml-auto text-[10px] text-slate-400 font-normal flex-shrink-0">
              {new Date(e.created_at).toLocaleDateString("en-GB", { day: "2-digit", month: "short", year: "numeric" })}
            </span>
          </div>
          <p className="text-slate-600 whitespace-pre-wrap leading-relaxed pl-6">{e.content}</p>
        </div>
      ))}

      {/* Add new entry form */}
      <div className="bg-white border border-blue-200 rounded-md px-3 py-2.5 space-y-2">
        <p className="text-[11px] font-semibold text-blue-700">
          {entries.length === 0 ? "Enter email details" : "Add another entry"}
        </p>
        <input
          placeholder="Sender name / email address"
          value={panelState.newSender}
          onChange={(e) => onChangeSender(e.target.value)}
          className="w-full px-2 py-1.5 text-xs border border-blue-100 rounded bg-blue-50/30 focus:outline-none focus:ring-1 focus:ring-blue-300 placeholder-slate-400"
        />
        <textarea
          placeholder="Paste email content or relevant excerpt…"
          value={panelState.newContent}
          onChange={(e) => onChangeContent(e.target.value)}
          rows={3}
          className="w-full px-2 py-1.5 text-xs border border-blue-100 rounded bg-blue-50/30 focus:outline-none focus:ring-1 focus:ring-blue-300 placeholder-slate-400 resize-y font-mono leading-relaxed"
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-2.5 py-1 text-[11px] text-slate-500 border border-slate-200 rounded hover:bg-slate-100 transition-colors"
          >
            Close
          </button>
          <button
            onClick={onAddEntry}
            disabled={!panelState.newSender.trim() || !panelState.newContent.trim() || panelState.saving}
            className="px-2.5 py-1 text-[11px] text-white bg-blue-600 hover:bg-blue-700 rounded transition-colors disabled:opacity-40 flex items-center gap-1"
          >
            {panelState.saving ? <Loader2 size={10} className="animate-spin" /> : <Plus size={10} />}
            Add Entry
          </button>
        </div>
      </div>
    </div>
  );
}

// Mail icon shown on row hover — invisible at rest
function AnnotIcon({
  rowKey, annotations, annotPanel, setAnnotPanel,
}: {
  rowKey: string;
  annotations: AnnotationsMap;
  annotPanel: AnnotPanelState | null;
  setAnnotPanel: (s: AnnotPanelState | null) => void;
}) {
  const count = annotations[rowKey]?.length ?? 0;
  const isOpen = annotPanel?.key === rowKey;
  return (
    <button
      onClick={() => {
        if (isOpen) {
          setAnnotPanel(null);
        } else {
          setAnnotPanel({ key: rowKey, newSender: "", newContent: "", saving: false });
        }
      }}
      title="Log email-instructed change"
      // Invisible at rest, faint on row-hover (via group-hover), full on icon-hover / when open
      className={`transition-all duration-150 p-1 rounded relative ${isOpen ? "opacity-80 text-blue-500 bg-blue-50" : "opacity-0 group-hover:opacity-20 hover:!opacity-80 text-slate-400 hover:text-blue-500"}`}
    >
      <Mail size={11} />
      {count > 0 && (
        <span className="absolute -top-1 -right-1 bg-blue-400 text-white text-[7px] rounded-full w-3 h-3 flex items-center justify-center leading-none">
          {count > 9 ? "9+" : count}
        </span>
      )}
    </button>
  );
}

// ─── Waterfall step table (module level) ──────────────────────────────────────

type WFKey = "interest_waterfall" | "principal_waterfall" | "excess_cashflow_waterfall";

interface WFTableProps {
  label: string;
  wfPrefix: string;
  steps: WaterfallStep[];
  editing: boolean;
  onUpdate: (i: number, patch: Partial<WaterfallStep>) => void;
  onDelete: (i: number) => void;
  onAdd: () => void;
  onMove: (i: number, dir: -1 | 1) => void;
  annotations: AnnotationsMap;
  annotPanel: AnnotPanelState | null;
  setAnnotPanel: (s: AnnotPanelState | null) => void;
  onAddAnnotEntry: () => void;
}

function WaterfallTable({ label, wfPrefix, steps, editing, onUpdate, onDelete, onAdd, onMove, annotations, annotPanel, setAnnotPanel, onAddAnnotEntry }: WFTableProps) {
  const thCls = "px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap";
  return (
    <div>
      <p className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">{label}</p>
      {steps.length === 0 && !editing
        ? <p className="text-xs text-gray-400 pl-2 mb-3">No steps extracted</p>
        : (
          <div className="overflow-x-auto">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="table-header">
                  {editing && <th className={`${thCls} w-16`}>Order</th>}
                  <th className={`${thCls} w-12`}>#</th>
                  <th className={`${thCls} min-w-[200px]`}>Description</th>
                  <th className={`${thCls} w-24`}>Class</th>
                  <th className={`${thCls} w-28`}>Payment Type</th>
                  <th className={`${thCls} w-36`}>Source Bucket</th>
                  <th className={`${thCls} w-28`}>Condition</th>
                  <th className={`${thCls} min-w-[200px]`}>Formula</th>
                  <th className="w-7"></th>
                  {editing && <th className={`${thCls} w-10`}></th>}
                </tr>
              </thead>
              <tbody>
                {steps.map((s, i) => {
                  const rowKey = `${wfPrefix}:step:${s.step}`;
                  const isAnnotOpen = annotPanel?.key === rowKey;
                  return (
                    <React.Fragment key={i}>
                      <tr className={`${i % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                        {editing && (
                          <td className="px-2 py-1">
                            <div className="flex gap-0.5">
                              <MoveBtn dir="up" onClick={() => onMove(i, -1)} />
                              <MoveBtn dir="down" onClick={() => onMove(i, 1)} />
                            </div>
                          </td>
                        )}
                        <td className="px-2 py-1">
                          {editing
                            ? <NumInput value={s.step} onChange={(v) => onUpdate(i, { step: +v })} step="1" min="1" className="w-14" />
                            : <span className="inline-flex w-6 h-6 bg-primary-100 text-primary-700 rounded-full items-center justify-center font-bold">{s.step}</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <TA value={s.description} onChange={(v) => onUpdate(i, { description: v })} rows={2} className="w-full min-w-[180px]" />
                            : <span className="text-gray-700 leading-snug">{s.description || "—"}</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <TxtInput value={s.class_name ?? ""} onChange={(v) => onUpdate(i, { class_name: v || undefined })} className="w-20" />
                            : s.class_name ? <span className="badge badge-blue">{s.class_name}</span> : <span className="text-gray-400">—</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <SelectInput value={s.payment_type} onChange={(v) => onUpdate(i, { payment_type: v })} options={["interest", "principal", "fee", "reserve", "excess", "expense", "loss"]} className="w-28" />
                            : <span className="badge bg-gray-100 text-gray-600">{s.payment_type}</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <SelectInput value={s.source_bucket} onChange={(v) => onUpdate(i, { source_bucket: v })} options={["available_funds", "interest_remittance", "principal_remittance", "excess_cashflow", "reserve"]} className="w-40" />
                            : <span className="text-gray-600 text-xs">{s.source_bucket || "—"}</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <TxtInput value={s.condition ?? ""} onChange={(v) => onUpdate(i, { condition: v || undefined })} className="w-28" />
                            : s.condition ? <span className="badge badge-yellow">{s.condition}</span> : <span className="text-gray-400">—</span>}
                        </td>
                        <td className="px-2 py-1">
                          {editing
                            ? <TA value={s.amount_formula ?? ""} onChange={(v) => onUpdate(i, { amount_formula: v || undefined })} rows={2} className="w-full min-w-[180px]" />
                            : s.amount_formula
                              ? <code className="text-xs bg-gray-100 px-1.5 py-0.5 rounded font-mono text-gray-700 break-all">{s.amount_formula}</code>
                              : <span className="text-gray-400">—</span>}
                        </td>
                        {/* Hidden audit icon */}
                        <td className="px-1 py-2 w-7">
                          <AnnotIcon rowKey={rowKey} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} />
                        </td>
                        {editing && <td className="px-2 py-1"><DelBtn onClick={() => onDelete(i)} /></td>}
                      </tr>
                      {isAnnotOpen && (
                        <tr>
                          <td colSpan={99} className="p-0 border-b border-slate-100">
                            <div className="px-4 py-3 bg-slate-50/60">
                              <AnnotationPanel
                                entries={annotations[rowKey] ?? []}
                                panelState={annotPanel!}
                                onChangeSender={(v) => setAnnotPanel({ ...annotPanel!, newSender: v })}
                                onChangeContent={(v) => setAnnotPanel({ ...annotPanel!, newContent: v })}
                                onAddEntry={onAddAnnotEntry}
                                onClose={() => setAnnotPanel(null)}
                              />
                            </div>
                          </td>
                        </tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      {editing && <AddRowBtn label="Add Step" onClick={onAdd} />}
    </div>
  );
}

// ─── Trigger logic editor (NL → Python expression) ──────────────────────────
//
// Users don't know the engine's variable names (e.g. `delinquency_60plus_pct`),
// so we let them describe the trigger in plain English. The LLM is given the
// curated variable catalog server-side and returns a valid Python expression.
// The manual Condition / Action boxes are still the source of truth — the NL
// path simply pre-fills them so the user can review/edit before saving.

interface TriggerLogicEditorProps {
  trigger: TriggerTest;
  onUpdate: (patch: Partial<TriggerTest>) => void;
  enableNlAssist?: boolean;
}

function TriggerLogicEditor({ trigger, onUpdate, enableNlAssist = true }: TriggerLogicEditorProps) {
  const [nlOpen, setNlOpen] = useState(false);
  const [nlText, setNlText] = useState("");
  const [generating, setGenerating] = useState(false);
  const [explanation, setExplanation] = useState<string>("");
  const [hasGenerated, setHasGenerated] = useState(false);

  const handleGenerate = async () => {
    if (!nlText.trim()) {
      toast.error("Enter a description first");
      return;
    }
    setGenerating(true);
    try {
      const result = await generateTriggerExpression({
        description: nlText,
        test_name: trigger.test_name,
      });
      if (!result.condition) {
        toast.error("Could not generate an expression — try rephrasing.");
        if (result.explanation) setExplanation(result.explanation);
        return;
      }
      onUpdate({
        trigger_condition: result.condition,
        trigger_action: result.action || trigger.trigger_action,
      });
      setExplanation(result.explanation);
      setHasGenerated(true);
      toast.success("Expression generated. Review before saving.");
    } catch {
      toast.error("Generation failed. Please try again or enter manually.");
    } finally {
      setGenerating(false);
    }
  };

  return (
    <div className="space-y-2 min-w-[280px]">
      {enableNlAssist && (
        <>
          {/* NL → expression toggle */}
          <div className="flex items-center justify-between gap-2">
            <button
              type="button"
              onClick={() => setNlOpen((v) => !v)}
              className={`flex items-center gap-1 text-[11px] font-medium px-2 py-1 rounded transition-colors ${
                nlOpen
                  ? "bg-purple-100 text-purple-700"
                  : "text-purple-600 hover:bg-purple-50"
              }`}
              title="Describe the trigger in plain English; the LLM will generate a valid expression"
            >
              <Sparkles size={11} />
              {nlOpen ? "Hide plain-English input" : "Describe in plain English"}
            </button>
            {nlOpen && hasGenerated && (
              <button
                type="button"
                onClick={handleGenerate}
                disabled={generating || !nlText.trim()}
                className="flex items-center gap-1 text-[11px] text-gray-500 hover:text-gray-700 disabled:opacity-40"
                title="Regenerate the expression"
              >
                <RefreshCw size={10} className={generating ? "animate-spin" : ""} />
                Regenerate
              </button>
            )}
          </div>

          {nlOpen && (
            <div className="border border-purple-200 bg-purple-50/40 rounded-md p-2 space-y-2">
              <textarea
                rows={3}
                placeholder='e.g. "fires when the 6-month rolling 60+ day delinquency rate exceeds 5%"'
                value={nlText}
                onChange={(e) => setNlText(e.target.value)}
                className="w-full px-2 py-1.5 text-xs border border-purple-200 rounded bg-white focus:outline-none focus:ring-1 focus:ring-purple-300 resize-y"
              />
              <div className="flex items-center justify-between gap-2">
                <p className="text-[10px] text-purple-600 leading-tight">
                  The generated expression below uses only variables known to the
                  waterfall engine.
                </p>
                <button
                  type="button"
                  onClick={handleGenerate}
                  disabled={generating || !nlText.trim()}
                  className="text-[11px] text-white bg-purple-600 hover:bg-purple-700 px-2.5 py-1 rounded disabled:opacity-40 flex items-center gap-1 flex-shrink-0"
                >
                  {generating ? (
                    <><Loader2 size={10} className="animate-spin" /> Generating…</>
                  ) : (
                    <><Sparkles size={10} /> Generate</>
                  )}
                </button>
              </div>
              {explanation && (
                <p className="text-[10px] text-gray-600 bg-white border border-purple-100 rounded px-2 py-1 leading-snug">
                  {explanation}
                </p>
              )}
            </div>
          )}
        </>
      )}

      {/* Manual condition + action — always visible, source of truth on save */}
      <div>
        <p className="text-xs text-gray-400 font-medium">Condition</p>
        <TA
          value={trigger.trigger_condition ?? ""}
          onChange={(v) => onUpdate({ trigger_condition: v || undefined })}
          rows={2}
          className="w-full font-mono text-xs"
        />
      </div>
      <div>
        <p className="text-xs text-gray-400 font-medium">Action</p>
        <input
          className="input text-sm py-1 px-2 w-full font-mono text-xs"
          placeholder="e.g. CREDIT_SUPPORT_DEPLETION"
          value={trigger.trigger_action ?? ""}
          onChange={(e) => onUpdate({ trigger_action: e.target.value || undefined })}
        />
      </div>
    </div>
  );
}

// ─── Default factories ────────────────────────────────────────────────────────

const newClass = (): CertificateClass => ({
  class_name: "", cusip: "", type: "Senior", sub_type: "", is_notional: false,
  is_exchangeable: false, is_residual: false, initial_principal: 0,
  interest_rate_type: "floating", fixed_rate: undefined, margin: undefined,
  benchmark: "SOFR", benchmark_tenor: "1M", rate_cap: undefined, rate_floor: 0,
  accrual_convention: "30/360", interest_priority: 0, principal_priority: 0, principal_method: "sequential",
});
const newFee = (): FeeConfig => ({ fee_name: "", fee_type: "percentage", fee_rate: undefined, fixed_amount: undefined, priority: 1, applies_to: "pool_balance", servicer_name: "", category: "fee", paid_from: "interest_remittance", payee: "", accrues: true, shortfall_carried: true });
const newExpense = (): FeeConfig => ({ fee_name: "", fee_type: "fixed", fee_rate: undefined, fixed_amount: undefined, priority: 1, applies_to: "pool_balance", servicer_name: "", category: "expense", paid_from: "interest_remittance", payee: "", accrues: false, shortfall_carried: true });
const newReserveAcct = (): ReserveAccount => ({ account_name: "", account_type: "reserve", initial_balance: 0, target_amount: undefined, target_formula: "", funded_from: "excess_cashflow", released_to: "available_funds", release_condition: "", release_formula: "", floor: 0, draws_allowed: true, draw_priority: 99 });
const newServicer = (): ServicerConfig => ({ servicer_name: "", servicing_fee_rate: 0.0025, advance_obligation: false, portfolio_pct: undefined });
const newTrigger = (): TriggerTest => ({ test_name: "", test_type: "oc", description: "", trigger_condition: "", trigger_action: "" });

// ─── Main component ───────────────────────────────────────────────────────────

interface Props {
  config: DealConfig;
  onSaved: (cfg: DealConfig) => void;
  onPersist?: (cfg: DealConfig) => Promise<DealConfig | void>;
  showPdfPanel?: boolean;
  enableTriggerNlAssist?: boolean;
  saveSuccessMessage?: string;
}

const normalise = (c: DealConfig): DealConfig => ({
  ...c,
  servicers: c.servicers ?? [],
  classes: c.classes ?? [],
  fees: c.fees ?? [],
  reserve_accounts: c.reserve_accounts ?? [],
  interest_waterfall: c.interest_waterfall ?? [],
  principal_waterfall: c.principal_waterfall ?? [],
  excess_cashflow_waterfall: c.excess_cashflow_waterfall ?? [],
  triggers: c.triggers ?? [],
  loss_allocation_order: c.loss_allocation_order ?? [],
});

export default function ExtractedFields({
  config: initial,
  onSaved,
  onPersist,
  showPdfPanel = true,
  enableTriggerNlAssist = true,
  saveSuccessMessage = "Saved and verified!",
}: Props) {
  const [config, setConfig] = useState<DealConfig>(() => normalise(initial));
  // "saved baseline" — Discard resets to this (updates after every successful save)
  const [baseline, setBaseline] = useState<DealConfig>(() => normalise(initial));

  const [editingSection, setEditingSection] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [open, setOpen] = useState<Record<string, boolean>>({
    deal: true, servicers: false, classes: true, fees: false,
    expenses: false, accounts: false,
    interest_wf: false, principal_wf: false, excess_wf: false,
    triggers: false, loss: false,
  });

  // Audit annotations
  const [annotations, setAnnotations] = useState<AnnotationsMap>({});
  const [annotPanel, setAnnotPanel] = useState<AnnotPanelState | null>(null);

  // PDF verification panel — visible only while a section is being edited.
  // Users can collapse it on smaller screens via the toggle button.
  const [pdfPanelOpen, setPdfPanelOpen] = useState(true);

  // Track which originally-extracted values the user has overridden (per save).
  // Pre-computed search strings are compared against the baseline value set so we
  // can keep highlights stable as the user types — the value flips to "overridden"
  // styling on save, not on each keystroke.
  const [overriddenValues, setOverriddenValues] = useState<Set<string>>(new Set());

  const pdfUrl = showPdfPanel && initial.deal_id ? dealPdfUrl(initial.deal_id) : null;

  // Load annotations when deal config is available
  useEffect(() => {
    if (!initial.deal_id) return;
    getAuditAnnotations(initial.deal_id)
      .then((d) => setAnnotations(d.entries ?? {}))
      .catch(() => {});
  }, [initial.deal_id]);

  const tog = (k: string) => setOpen((s) => ({ ...s, [k]: !s[k] }));
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const setF = (field: string, value: any) => setConfig((p) => ({ ...p, [field]: value }));

  const startEdit = (section: string) => {
    setEditingSection(section);
    if (!open[section]) setOpen((s) => ({ ...s, [section]: true }));
    // Surface the PDF panel automatically — even if the user previously collapsed it,
    // entering an edit is a strong signal they want to cross-check against the source.
    if (showPdfPanel) setPdfPanelOpen(true);
  };

  const discardEdit = () => {
    setConfig(baseline);
    setEditingSection(null);
  };

  // Compute the set of *original* (baseline) search strings that no longer appear
  // in the updated config — these are the extracted values the user just overrode,
  // and they get rendered with the grey/strikethrough highlight in the PDF.
  const computeOverridesForSection = (section: string, oldCfg: DealConfig, newCfg: DealConfig): string[] => {
    const uiKey = editSectionToUi(section);
    if (!uiKey) return [];
    const before = new Set(valuesForSection(uiKey, oldCfg));
    const after = new Set(valuesForSection(uiKey, newCfg));
    return Array.from(before).filter((v) => !after.has(v));
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const persisted = onPersist
        ? await onPersist(config)
        : (await submitReview({ deal_id: config.deal_id, reviewed_config: config, corrections: [], reviewer_name: "Admin", notes: "Reviewed via UI" }), config);
      toast.success(saveSuccessMessage);
      const updated = normalise(persisted ?? config);

      // Capture overrides for the section that was just saved (if any).
      if (showPdfPanel && editingSection) {
        const newOverrides = computeOverridesForSection(editingSection, baseline, updated);
        if (newOverrides.length > 0) {
          setOverriddenValues((prev) => {
            const next = new Set(prev);
            newOverrides.forEach((v) => next.add(v));
            return next;
          });
        }
      }

      setBaseline(updated);
      setConfig(updated);
      setEditingSection(null);
      onSaved(updated);
    } catch {
      toast.error("Failed to save.");
    } finally {
      setSaving(false);
    }
  };

  // ── Audit helpers ──────────────────────────────────────────────────────────
  const handleAddAnnotEntry = useCallback(async () => {
    if (!annotPanel || !annotPanel.newSender.trim() || !annotPanel.newContent.trim()) return;
    setAnnotPanel((p) => p ? { ...p, saving: true } : p);
    try {
      const result = await addAuditEntry(initial.deal_id, {
        row_key: annotPanel.key,
        sender: annotPanel.newSender.trim(),
        content: annotPanel.newContent.trim(),
      });
      const newEntry: AnnotEntry = result.entry;
      setAnnotations((prev) => ({
        ...prev,
        [annotPanel.key]: [...(prev[annotPanel.key] ?? []), newEntry],
      }));
      setAnnotPanel((p) => p ? { ...p, newSender: "", newContent: "", saving: false } : p);
      toast.success("Change logged.");
    } catch {
      toast.error("Failed to log entry.");
      setAnnotPanel((p) => p ? { ...p, saving: false } : p);
    }
  }, [annotPanel, initial.deal_id]);

  // ── Array helpers ──────────────────────────────────────────────────────────
  const updClass = (i: number, patch: Partial<CertificateClass>) =>
    setConfig((p) => { const a = [...p.classes]; a[i] = { ...a[i], ...patch }; return { ...p, classes: a }; });
  const delClass = (i: number) => setConfig((p) => ({ ...p, classes: p.classes.filter((_, j) => j !== i) }));

  const updFee = (i: number, patch: Partial<FeeConfig>) =>
    setConfig((p) => { const a = [...p.fees]; a[i] = { ...a[i], ...patch }; return { ...p, fees: a }; });
  const delFee = (i: number) => setConfig((p) => ({ ...p, fees: p.fees.filter((_, j) => j !== i) }));

  // Expenses are stored in the same fees[] array, filtered by category="expense".
  // Helper that maps a filtered index back into the underlying array.
  const expenseIndices = (): number[] =>
    config.fees.map((f, i) => ({ f, i })).filter(({ f }) => (f.category ?? "fee") === "expense").map(({ i }) => i);
  const feeOnlyIndices = (): number[] =>
    config.fees.map((f, i) => ({ f, i })).filter(({ f }) => (f.category ?? "fee") !== "expense").map(({ i }) => i);

  const moveFeeRow = (fromIdx: number, toIdx: number) =>
    setConfig((p) => {
      const a = [...p.fees];
      if (fromIdx < 0 || fromIdx >= a.length || toIdx < 0 || toIdx >= a.length) return p;
      [a[fromIdx], a[toIdx]] = [a[toIdx], a[fromIdx]];
      return { ...p, fees: a };
    });

  const updRA = (i: number, patch: Partial<ReserveAccount>) =>
    setConfig((p) => { const a = [...(p.reserve_accounts ?? [])]; a[i] = { ...a[i], ...patch }; return { ...p, reserve_accounts: a }; });
  const delRA = (i: number) => setConfig((p) => ({ ...p, reserve_accounts: (p.reserve_accounts ?? []).filter((_, j) => j !== i) }));
  const moveRA = (i: number, dir: -1 | 1) => setConfig((p) => {
    const a = [...(p.reserve_accounts ?? [])]; const j = i + dir;
    if (j < 0 || j >= a.length) return p;
    [a[i], a[j]] = [a[j], a[i]];
    return { ...p, reserve_accounts: a };
  });

  const updSvc = (i: number, patch: Partial<ServicerConfig>) =>
    setConfig((p) => { const a = [...(p.servicers ?? [])]; a[i] = { ...a[i], ...patch }; return { ...p, servicers: a }; });
  const delSvc = (i: number) => setConfig((p) => ({ ...p, servicers: (p.servicers ?? []).filter((_, j) => j !== i) }));

  const makeWFHandlers = (wfKey: WFKey) => ({
    onUpdate: (i: number, patch: Partial<WaterfallStep>) =>
      setConfig((p) => { const a = [...p[wfKey]]; a[i] = { ...a[i], ...patch }; return { ...p, [wfKey]: a }; }),
    onDelete: (i: number) => setConfig((p) => ({ ...p, [wfKey]: p[wfKey].filter((_, j) => j !== i) })),
    onAdd: () => setConfig((p) => {
      const steps = p[wfKey];
      const next = steps.length > 0 ? Math.max(...steps.map((s) => s.step)) + 1 : 1;
      return { ...p, [wfKey]: [...steps, { step: next, description: "", class_name: "", payment_type: "interest", source_bucket: "available_funds", condition: "", amount_formula: "" }] };
    }),
    onMove: (i: number, dir: -1 | 1) => setConfig((p) => {
      const a = [...p[wfKey]]; const j = i + dir;
      if (j < 0 || j >= a.length) return p;
      [a[i], a[j]] = [a[j], a[i]];
      return { ...p, [wfKey]: a };
    }),
  });

  const updTrigger = (i: number, patch: Partial<TriggerTest>) =>
    setConfig((p) => { const a = [...(p.triggers ?? [])]; a[i] = { ...a[i], ...patch }; return { ...p, triggers: a }; });
  const delTrigger = (i: number) => setConfig((p) => ({ ...p, triggers: (p.triggers ?? []).filter((_, j) => j !== i) }));
  const updLoss = (i: number, val: string) => setConfig((p) => { const a = [...(p.loss_allocation_order ?? [])]; a[i] = val; return { ...p, loss_allocation_order: a }; });
  const delLoss = (i: number) => setConfig((p) => ({ ...p, loss_allocation_order: (p.loss_allocation_order ?? []).filter((_, j) => j !== i) }));
  const moveLoss = (i: number, dir: -1 | 1) => setConfig((p) => {
    const a = [...(p.loss_allocation_order ?? [])]; const j = i + dir;
    if (j < 0 || j >= a.length) return p;
    [a[i], a[j]] = [a[j], a[i]];
    return { ...p, loss_allocation_order: a };
  });

  const intWF  = makeWFHandlers("interest_waterfall");
  const prinWF = makeWFHandlers("principal_waterfall");
  const exWF   = makeWFHandlers("excess_cashflow_waterfall");

  const editing = (sec: string) => editingSection === sec;
  const canEdit = (sec: string) => editingSection === null || editingSection === sec;

  const sectionHdr = (key: string, title: string) => (
    <SectionHeader
      title={title}
      open={!!open[key]}
      onToggle={() => tog(key)}
      isEditing={editingSection === key}
      saving={saving}
      canEdit={canEdit(key)}
      onEdit={() => startEdit(key)}
      onSave={handleSave}
      onDiscard={discardEdit}
    />
  );

  const thCls = "px-3 py-2.5 text-left text-xs font-semibold whitespace-nowrap";
  const tdCls = "px-3 py-2 text-sm";

  const splitActive = showPdfPanel && editingSection !== null && pdfPanelOpen && !!pdfUrl;

  return (
    <div className={`flex gap-4 fade-in ${splitActive ? "items-start" : ""}`}>
      <div className={`space-y-4 min-w-0 ${splitActive ? "flex-1 lg:w-1/2" : "w-full"}`}>
      {/* Status banner */}
      {!config.manually_verified ? (
        <div className="flex items-start gap-3 bg-amber-50 border border-amber-200 rounded-xl px-4 py-3">
          <span className="text-amber-500 text-lg mt-0.5">⚠</span>
          <div>
            <p className="text-sm font-semibold text-amber-800">Draft — Pending Review</p>
            <p className="text-xs text-amber-700 mt-0.5">
              Auto-saved as a draft. Use the <strong>Edit</strong> button on each section to make corrections, then <strong>Save</strong> to verify.
            </p>
          </div>
        </div>
      ) : (
        <div className="flex items-center gap-3 bg-green-50 border border-green-200 rounded-xl px-4 py-3">
          <ShieldCheck size={18} className="text-green-600 flex-shrink-0" />
          <p className="text-sm font-semibold text-green-800">Verified &amp; Saved — ready for waterfall computation</p>
        </div>
      )}

      {/* Summary line */}
      <div className="flex items-center gap-2 flex-wrap">
        <CheckCircle2 size={16} className="text-green-600" />
        <span className="text-sm font-medium text-gray-600">
          {config.classes.length} classes · {config.fees.length} fees · {config.interest_waterfall.length} interest steps · {config.principal_waterfall.length} principal steps
        </span>
        {editingSection && (
          <span className="ml-auto text-xs text-amber-600 bg-amber-50 border border-amber-200 px-2 py-0.5 rounded-full">
            Editing: {editingSection.replace("_wf", " waterfall").replace(/_/g, " ")}
          </span>
        )}
      </div>

      {/* ── 1. Deal Information ──────────────────────────────────────────── */}

      <div className="card">
        {sectionHdr("deal", "Deal Information")}
        {open.deal && (
          <div className="mt-4 space-y-5">
            <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4">
              {([
                ["Deal ID", "deal_id", true], ["Deal Name", "deal_name", false],
                ["Issuing Entity", "issuing_entity", false], ["Series", "series", false],
                ["Depositor", "depositor", false], ["Asset Class", "asset_class", false],
                ["Asset Type", "asset_type", false], ["Lien Position", "lien_position", false],
                ["Payment Frequency", "payment_frequency", false], ["Closing Date", "closing_date", false],
                ["Cut-off Date", "cut_off_date", false], ["First Payment Date", "first_payment_date", false],
                ["Legal Maturity Date", "legal_maturity_date", false], ["Benchmark", "benchmark", false],
                ["Benchmark Tenor", "benchmark_tenor", false], ["Day Count", "interest_day_count", false],
                ["Custodian", "custodian", false], ["Securities Administrator", "securities_administrator", false],
                ["Owner Trustee", "owner_trustee", false],
              ] as [string, string, boolean][]).map(([label, field, readOnly]) => (
                <FieldGroup key={field} label={label}>
                  {editing("deal") && !readOnly ? (
                    field === "interest_day_count" ? (
                      <SelectInput
                        value={String((config as unknown as Record<string, unknown>)[field] ?? "actual/360")}
                        onChange={(v) => setF(field, v || undefined)}
                        options={["30/360", "actual/360", "actual/365", "actual/actual"]}
                      />
                    ) : (
                      <TxtInput
                        value={String((config as unknown as Record<string, unknown>)[field] ?? "")}
                        onChange={(v) => setF(field, v || undefined)}
                      />
                    )
                  ) : (
                    <ViewVal val={String((config as unknown as Record<string, unknown>)[field] ?? "")} />
                  )}
                </FieldGroup>
              ))}
              <FieldGroup label="Original Pool Balance ($)">
                {editing("deal") ? <NumInput value={config.original_pool_balance} onChange={(v) => setF("original_pool_balance", +v)} step="1" /> : <ViewVal val={`$${fmtNum(config.original_pool_balance)}`} />}
              </FieldGroup>
              <FieldGroup label="Cleanup Call (%)">
                {editing("deal") ? <NumInput value={toPct(config.cleanup_call_pct)} onChange={(v) => setF("cleanup_call_pct", fromPct(v) ?? 0.1)} step="0.01" /> : <ViewVal val={fmtPctOrDash(config.cleanup_call_pct)} />}
              </FieldGroup>
              <FieldGroup label="Default SOFR Rate (%)">
                {editing("deal") ? <NumInput value={toPct(config.default_sofr_rate)} onChange={(v) => setF("default_sofr_rate", fromPct(v))} step="0.001" /> : <ViewVal val={fmtPctOrDash(config.default_sofr_rate)} />}
              </FieldGroup>
              <FieldGroup label="Accural Days (First Payment)">
                {editing("deal")
                  ? (
                    <NumInput
                      value={config.accrual_days ?? ""}
                      onChange={(v) => setF("accrual_days", v === "" ? undefined : Math.max(1, Math.floor(Number(v))))}
                      step="1"
                      min="1"
                    />
                  )
                  : <ViewVal val={config.accrual_days ?? "—"} />}
              </FieldGroup>
            </div>
            <FieldGroup label="Notes / Additional Rules">
              {editing("deal") ? <TA value={config.notes ?? ""} onChange={(v) => setF("notes", v || undefined)} rows={3} className="w-full" /> : <ViewVal val={config.notes} />}
            </FieldGroup>
          </div>
        )}
      </div>

      {/* ── 2. Servicers ────────────────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("servicers", `Servicers (${(config.servicers ?? []).length})`)}
        {open.servicers && (
          <div className="mt-4 overflow-x-auto">
            {(config.servicers ?? []).length === 0 && !editing("servicers")
              ? <p className="text-sm text-gray-400 text-center py-4">No servicers extracted</p>
              : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      <th className={thCls}>Servicer Name</th><th className={thCls}>Fee Rate (%)</th>
                      <th className={thCls}>Advances P&amp;I?</th><th className={thCls}>Portfolio %</th>
                      <th className="w-7"></th>{editing("servicers") && <th className={thCls}></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {(config.servicers ?? []).map((svc, i) => {
                      const rk = `servicers:${svc.servicer_name || i}`;
                      return (
                        <React.Fragment key={i}>
                          <tr className={`${i % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                            <td className={tdCls}>{editing("servicers") ? <TxtInput value={svc.servicer_name} onChange={(v) => updSvc(i, { servicer_name: v })} className="w-full" /> : svc.servicer_name || "—"}</td>
                            <td className={tdCls}>{editing("servicers") ? <NumInput value={toPct(svc.servicing_fee_rate)} onChange={(v) => updSvc(i, { servicing_fee_rate: fromPct(v) ?? 0 })} step="0.001" className="w-28" /> : fmtPctOrDash(svc.servicing_fee_rate)}</td>
                            <td className={tdCls}>{editing("servicers") ? <input type="checkbox" checked={svc.advance_obligation} onChange={(e) => updSvc(i, { advance_obligation: e.target.checked })} className="w-4 h-4" /> : svc.advance_obligation ? "Yes" : "No"}</td>
                            <td className={tdCls}>{editing("servicers") ? <NumInput value={toPct(svc.portfolio_pct)} onChange={(v) => updSvc(i, { portfolio_pct: fromPct(v) ?? undefined })} step="0.1" className="w-24" /> : svc.portfolio_pct ? fmtPctOrDash(svc.portfolio_pct) : "—"}</td>
                            <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                            {editing("servicers") && <td className={tdCls}><DelBtn onClick={() => delSvc(i)} /></td>}
                          </tr>
                          {annotPanel?.key === rk && (
                            <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            {editing("servicers") && <AddRowBtn label="Add Servicer" onClick={() => setConfig((p) => ({ ...p, servicers: [...(p.servicers ?? []), newServicer()] }))} />}
          </div>
        )}
      </div>

      {/* ── 3. Certificate Classes ───────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("classes", `Certificate Classes (${config.classes.length})`)}
        {open.classes && (
          <div className="mt-4 overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="table-header">
                  <th className={thCls}>Class</th><th className={thCls}>CUSIP</th><th className={thCls}>Type</th>
                  <th className={thCls}>Balance ($)</th><th className={thCls}>Rate Type</th>
                  <th className={thCls}>Margin (%)</th><th className={thCls}>Fixed Rate (%)</th><th className={thCls}>Cap (%)</th>
                  <th className={thCls}>Accrual</th><th className={thCls}>Int Pri</th><th className={thCls}>Prin Pri</th>
                  <th className={thCls}>Method</th><th className={thCls}>Notional</th><th className={thCls}>Exchg</th>
                  <th className={thCls}>Fitch</th><th className={thCls}>Moody's</th><th className={thCls}>S&amp;P</th><th className={thCls}>KBRA</th>
                  <th className="w-7"></th>{editing("classes") && <th className={thCls}></th>}
                </tr>
              </thead>
              <tbody>
                {config.classes.map((cls, i) => {
                  const rk = `classes:${cls.class_name || i}`;
                  return (
                    <React.Fragment key={i}>
                      <tr className={`${i % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                        <td className="px-3 py-2 font-semibold text-primary-700">{editing("classes") ? <TxtInput value={cls.class_name} onChange={(v) => updClass(i, { class_name: v })} className="w-20" /> : cls.class_name}</td>
                        <td className={tdCls}>{editing("classes") ? <TxtInput value={cls.cusip ?? ""} onChange={(v) => updClass(i, { cusip: v || undefined })} className="w-32" /> : <span className="font-mono text-xs">{cls.cusip || "—"}</span>}</td>
                        <td className={tdCls}>{editing("classes") ? <SelectInput value={cls.type} onChange={(v) => updClass(i, { type: v })} options={["Senior", "Mezzanine", "Subordinate", "IO", "Exchangable", "Residual", "PO", "Excess Interest"]} className="w-32" /> : <span className={`badge ${cls.type === "Senior" ? "badge-green" : cls.type === "Residual" ? "badge-yellow" : "badge-blue"}`}>{cls.type}</span>}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={cls.initial_principal} onChange={(v) => updClass(i, { initial_principal: +v })} step="1000" className="w-36" /> : <span className="font-mono">${fmtNum(cls.initial_principal)}</span>}</td>
                        <td className={tdCls}>{editing("classes") ? <SelectInput value={cls.interest_rate_type} onChange={(v) => updClass(i, { interest_rate_type: v })} options={["floating", "fixed", "principal_only", "io", "excess_cashflow", "exchangeable", "residual"]} className="w-36" /> : <span className="capitalize">{cls.interest_rate_type}</span>}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={toPct(cls.margin)} onChange={(v) => updClass(i, { margin: fromPct(v) ?? undefined })} step="0.001" className="w-24" /> : fmtPctOrDash(cls.margin)}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={toPct(cls.fixed_rate)} onChange={(v) => updClass(i, { fixed_rate: fromPct(v) ?? undefined })} step="0.001" className="w-24" /> : fmtPctOrDash(cls.fixed_rate)}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={toPct(cls.rate_cap)} onChange={(v) => updClass(i, { rate_cap: fromPct(v) ?? undefined })} step="0.001" className="w-24" /> : fmtPctOrDash(cls.rate_cap)}</td>
                        <td className={tdCls}>{editing("classes") ? <SelectInput value={cls.accrual_convention} onChange={(v) => updClass(i, { accrual_convention: v })} options={["30/360", "actual/360", "actual/365", "actual/actual"]} className="w-28" /> : cls.accrual_convention}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={cls.interest_priority} onChange={(v) => updClass(i, { interest_priority: +v })} step="1" min="0" className="w-16" /> : cls.interest_priority}</td>
                        <td className={tdCls}>{editing("classes") ? <NumInput value={cls.principal_priority} onChange={(v) => updClass(i, { principal_priority: +v })} step="1" min="0" className="w-16" /> : cls.principal_priority}</td>
                        <td className={tdCls}>{editing("classes") ? <SelectInput value={cls.principal_method} onChange={(v) => updClass(i, { principal_method: v })} options={["sequential", "pro_rata"]} className="w-28" /> : cls.principal_method}</td>
                        <td className={tdCls}>{editing("classes") ? <input type="checkbox" checked={cls.is_notional} onChange={(e) => updClass(i, { is_notional: e.target.checked })} className="w-4 h-4" /> : cls.is_notional ? "Yes" : "No"}</td>
                        <td className={tdCls}>{editing("classes") ? <input type="checkbox" checked={cls.is_exchangeable} onChange={(e) => updClass(i, { is_exchangeable: e.target.checked })} className="w-4 h-4" /> : cls.is_exchangeable ? "Yes" : "No"}</td>
                        {(["fitch_rating", "moodys_rating", "sp_rating", "kbra_rating"] as const).map((rf) => (
                          <td key={rf} className={tdCls}>{editing("classes") ? <TxtInput value={cls[rf] ?? ""} onChange={(v) => updClass(i, { [rf]: v || undefined })} className="w-16" /> : cls[rf] || "—"}</td>
                        ))}
                        <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                        {editing("classes") && <td className={tdCls}><DelBtn onClick={() => delClass(i)} /></td>}
                      </tr>
                      {annotPanel?.key === rk && (
                        <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                      )}
                    </React.Fragment>
                  );
                })}
              </tbody>
            </table>
            {editing("classes") && <AddRowBtn label="Add Class" onClick={() => setConfig((p) => ({ ...p, classes: [...p.classes, newClass()] }))} />}
          </div>
        )}
      </div>

      {/* ── 4. Fees ────────────────────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("fees", `Fees (${feeOnlyIndices().length})`)}
        {open.fees && (
          <div className="mt-4 overflow-x-auto">
            {feeOnlyIndices().length === 0 && !editing("fees")
              ? <p className="text-sm text-gray-400 text-center py-4">No fees extracted</p>
              : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      <th className={thCls}>Fee Name</th><th className={thCls}>Type</th>
                      <th className={thCls}>Rate (%)</th><th className={thCls}>Fixed Amount ($)</th>
                      <th className={thCls}>Priority</th><th className={thCls}>Applies To</th>
                      <th className={thCls}>Servicer</th><th className="w-7"></th>
                      {editing("fees") && <th className={thCls}></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {feeOnlyIndices().map((globalIdx, rowPos) => {
                      const fee = config.fees[globalIdx];
                      const rk = `fees:${fee.fee_name || globalIdx}`;
                      return (
                        <React.Fragment key={globalIdx}>
                          <tr className={`${rowPos % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                            <td className={tdCls}>{editing("fees") ? <TxtInput value={fee.fee_name} onChange={(v) => updFee(globalIdx, { fee_name: v })} className="w-full" /> : <span className="font-medium">{fee.fee_name}</span>}</td>
                            <td className={tdCls}>{editing("fees") ? <SelectInput value={fee.fee_type} onChange={(v) => updFee(globalIdx, { fee_type: v })} options={["percentage", "fixed"]} className="w-28" /> : <span className="capitalize">{fee.fee_type}</span>}</td>
                            <td className={tdCls}>{editing("fees") ? <NumInput value={toPct(fee.fee_rate ?? undefined)} onChange={(v) => updFee(globalIdx, { fee_rate: fromPct(v) ?? null })} step="0.001" className="w-24" /> : fmtPctOrDash(fee.fee_rate)}</td>
                            <td className={tdCls}>{editing("fees") ? <NumInput value={fee.fixed_amount ?? ""} onChange={(v) => updFee(globalIdx, { fixed_amount: v ? +v : null })} step="1" className="w-28" /> : fee.fixed_amount != null ? `$${fmtNum(fee.fixed_amount, 2)}` : "—"}</td>
                            <td className={tdCls}>{editing("fees") ? <NumInput value={fee.priority} onChange={(v) => updFee(globalIdx, { priority: +v })} step="1" min="1" className="w-16" /> : fee.priority}</td>
                            <td className={tdCls}>{editing("fees") ? <TxtInput value={fee.applies_to} onChange={(v) => updFee(globalIdx, { applies_to: v })} className="w-32" /> : fee.applies_to}</td>
                            <td className={tdCls}>{editing("fees") ? <TxtInput value={fee.servicer_name ?? ""} onChange={(v) => updFee(globalIdx, { servicer_name: v || null })} className="w-28" /> : fee.servicer_name || "—"}</td>
                            <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                            {editing("fees") && <td className={tdCls}><DelBtn onClick={() => delFee(globalIdx)} /></td>}
                          </tr>
                          {annotPanel?.key === rk && (
                            <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            {editing("fees") && <AddRowBtn label="Add Fee" onClick={() => setConfig((p) => ({ ...p, fees: [...p.fees, newFee()] }))} />}
          </div>
        )}
      </div>

      {/* ── 4b. Expenses ───────────────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("expenses", `Expenses (${expenseIndices().length})`)}
        {open.expenses && (
          <div className="mt-4 overflow-x-auto">
            {expenseIndices().length === 0 && !editing("expenses")
              ? <p className="text-sm text-gray-400 text-center py-4">No expenses extracted</p>
              : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {editing("expenses") && <th className={`${thCls} w-16`}>Order</th>}
                      <th className={thCls}>Expense Name</th>
                      <th className={thCls}>Payee</th>
                      <th className={thCls}>Type</th>
                      <th className={thCls}>Rate (%) / Amount ($)</th>
                      <th className={thCls}>Paid From</th>
                      <th className={thCls}>Carries Shortfall</th>
                      <th className={thCls}>Priority</th>
                      <th className="w-7"></th>
                      {editing("expenses") && <th className={thCls}></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {expenseIndices().map((globalIdx, rowPos) => {
                      const fee = config.fees[globalIdx];
                      const rk = `expenses:${fee.fee_name || globalIdx}`;
                      const xIdxs = expenseIndices();
                      return (
                        <React.Fragment key={globalIdx}>
                          <tr className={`${rowPos % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                            {editing("expenses") && (
                              <td className="px-2 py-1">
                                <div className="flex gap-0.5">
                                  <MoveBtn dir="up" onClick={() => { if (rowPos > 0) moveFeeRow(globalIdx, xIdxs[rowPos - 1]); }} />
                                  <MoveBtn dir="down" onClick={() => { if (rowPos < xIdxs.length - 1) moveFeeRow(globalIdx, xIdxs[rowPos + 1]); }} />
                                </div>
                              </td>
                            )}
                            <td className={tdCls}>{editing("expenses") ? <TxtInput value={fee.fee_name} onChange={(v) => updFee(globalIdx, { fee_name: v })} className="w-full" /> : <span className="font-medium">{fee.fee_name || "—"}</span>}</td>
                            <td className={tdCls}>{editing("expenses") ? <TxtInput value={fee.payee ?? ""} onChange={(v) => updFee(globalIdx, { payee: v || null })} className="w-32" /> : (fee.payee || "—")}</td>
                            <td className={tdCls}>{editing("expenses") ? <SelectInput value={fee.fee_type} onChange={(v) => updFee(globalIdx, { fee_type: v })} options={["fixed", "percentage"]} className="w-28" /> : <span className="capitalize">{fee.fee_type}</span>}</td>
                            <td className={tdCls}>
                              {editing("expenses")
                                ? (fee.fee_type === "percentage"
                                    ? <NumInput value={toPct(fee.fee_rate ?? undefined)} onChange={(v) => updFee(globalIdx, { fee_rate: fromPct(v) ?? null })} step="0.001" className="w-28" />
                                    : <NumInput value={fee.fixed_amount ?? ""} onChange={(v) => updFee(globalIdx, { fixed_amount: v ? +v : null })} step="1" className="w-32" />)
                                : (fee.fee_type === "percentage" ? fmtPctOrDash(fee.fee_rate) : (fee.fixed_amount != null ? `$${fmtNum(fee.fixed_amount, 2)}` : "—"))}
                            </td>
                            <td className={tdCls}>{editing("expenses") ? <SelectInput value={fee.paid_from ?? "interest_remittance"} onChange={(v) => updFee(globalIdx, { paid_from: v })} options={["interest_remittance", "principal_remittance", "available_funds", "excess_cashflow"]} className="w-44" /> : <span className="text-xs text-gray-600">{fee.paid_from || "interest_remittance"}</span>}</td>
                            <td className={tdCls}>{editing("expenses") ? <input type="checkbox" checked={fee.shortfall_carried ?? true} onChange={(e) => updFee(globalIdx, { shortfall_carried: e.target.checked })} className="w-4 h-4" /> : (fee.shortfall_carried ?? true) ? "Yes" : "No"}</td>
                            <td className={tdCls}>{editing("expenses") ? <NumInput value={fee.priority} onChange={(v) => updFee(globalIdx, { priority: +v })} step="1" min="1" className="w-16" /> : fee.priority}</td>
                            <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                            {editing("expenses") && <td className={tdCls}><DelBtn onClick={() => delFee(globalIdx)} /></td>}
                          </tr>
                          {annotPanel?.key === rk && (
                            <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            {editing("expenses") && <AddRowBtn label="Add Expense" onClick={() => setConfig((p) => ({ ...p, fees: [...p.fees, newExpense()] }))} />}
          </div>
        )}
      </div>

      {/* ── 4c. Reserve Accounts & Accounts ────────────────────────────────── */}
      <div className="card">
        {sectionHdr("accounts", `Reserve Accounts & Accounts (${(config.reserve_accounts ?? []).length})`)}
        {open.accounts && (
          <div className="mt-4 overflow-x-auto">
            {(config.reserve_accounts ?? []).length === 0 && !editing("accounts")
              ? <p className="text-sm text-gray-400 text-center py-4">No reserve accounts extracted</p>
              : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      {editing("accounts") && <th className={`${thCls} w-16`}>Order</th>}
                      <th className={thCls}>Account Name</th>
                      <th className={thCls}>Type</th>
                      <th className={thCls}>Initial Balance ($)</th>
                      <th className={thCls}>Target Amount ($)</th>
                      <th className={thCls} title="Python expression. Available variables: total_beginning_balance, total_ending_balance, original_pool_balance">Target Formula</th>
                      <th className={thCls}>Funded From</th>
                      <th className={thCls}>Released To</th>
                      <th className={thCls}>Release Condition</th>
                      <th className={thCls}>Floor ($)</th>
                      <th className="w-7"></th>
                      {editing("accounts") && <th className={thCls}></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {(config.reserve_accounts ?? []).map((ra, i) => {
                      const rk = `accounts:${ra.account_name || i}`;
                      return (
                        <React.Fragment key={i}>
                          <tr className={`${i % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                            {editing("accounts") && (
                              <td className="px-2 py-1">
                                <div className="flex gap-0.5">
                                  <MoveBtn dir="up" onClick={() => moveRA(i, -1)} />
                                  <MoveBtn dir="down" onClick={() => moveRA(i, 1)} />
                                </div>
                              </td>
                            )}
                            <td className={tdCls}>{editing("accounts") ? <TxtInput value={ra.account_name} onChange={(v) => updRA(i, { account_name: v })} className="w-full" /> : <span className="font-medium">{ra.account_name || "—"}</span>}</td>
                            <td className={tdCls}>{editing("accounts") ? <SelectInput value={ra.account_type ?? "reserve"} onChange={(v) => updRA(i, { account_type: v })} options={["reserve", "prefunding", "capitalized_interest", "liquidity", "spread", "collection"]} className="w-40" /> : <span className="capitalize text-xs">{ra.account_type ?? "reserve"}</span>}</td>
                            <td className={tdCls}>{editing("accounts") ? <NumInput value={ra.initial_balance} onChange={(v) => updRA(i, { initial_balance: +v })} step="1" className="w-32" /> : <span className="font-mono">${fmtNum(ra.initial_balance, 2)}</span>}</td>
                            <td className={tdCls}>{editing("accounts") ? <NumInput value={ra.target_amount ?? ""} onChange={(v) => updRA(i, { target_amount: v ? +v : null })} step="1" className="w-32" /> : (ra.target_amount != null ? <span className="font-mono">${fmtNum(ra.target_amount, 2)}</span> : "—")}</td>
                            <td className={tdCls}>{editing("accounts") ? <TA value={ra.target_formula ?? ""} onChange={(v) => updRA(i, { target_formula: v || null })} rows={2} className="w-full min-w-[160px]" /> : (ra.target_formula ? <code className="text-xs bg-gray-100 px-1.5 py-0.5 rounded font-mono break-all">{ra.target_formula}</code> : "—")}</td>
                            <td className={tdCls}>{editing("accounts") ? <SelectInput value={ra.funded_from ?? "excess_cashflow"} onChange={(v) => updRA(i, { funded_from: v })} options={["excess_cashflow", "available_funds", "principal_remittance"]} className="w-44" /> : <span className="text-xs text-gray-600">{ra.funded_from ?? "excess_cashflow"}</span>}</td>
                            <td className={tdCls}>{editing("accounts") ? <SelectInput value={ra.released_to ?? "available_funds"} onChange={(v) => updRA(i, { released_to: v })} options={["available_funds", "excess_cashflow", "issuer"]} className="w-40" /> : <span className="text-xs text-gray-600">{ra.released_to ?? "available_funds"}</span>}</td>
                            <td className={tdCls}>{editing("accounts") ? <SelectInput value={ra.release_condition ?? ""} onChange={(v) => updRA(i, { release_condition: v || null })} options={["", "always", "trigger_failure", "cleanup_call"]} className="w-36" /> : (ra.release_condition || "—")}</td>
                            <td className={tdCls}>{editing("accounts") ? <NumInput value={ra.floor ?? 0} onChange={(v) => updRA(i, { floor: +v })} step="1" className="w-28" /> : <span className="font-mono">${fmtNum(ra.floor ?? 0, 2)}</span>}</td>
                            <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                            {editing("accounts") && <td className={tdCls}><DelBtn onClick={() => delRA(i)} /></td>}
                          </tr>
                          {annotPanel?.key === rk && (
                            <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            {editing("accounts") && (
              <>
                <AddRowBtn label="Add Reserve Account" onClick={() => setConfig((p) => ({ ...p, reserve_accounts: [...(p.reserve_accounts ?? []), newReserveAcct()] }))} />
                <p className="text-[11px] text-gray-400 mt-2 leading-relaxed">
                  <strong>Target Formula tip:</strong> Python expression. Available variables: <code className="bg-gray-100 px-1 rounded">total_beginning_balance</code>,{" "}
                  <code className="bg-gray-100 px-1 rounded">total_ending_balance</code>, <code className="bg-gray-100 px-1 rounded">original_pool_balance</code>.
                </p>
              </>
            )}
          </div>
        )}
      </div>

      {/* ── 5-7. Waterfall Sections ─────────────────────────────────────────── */}
      {([
        ["interest_wf",  "interest_waterfall",          "Interest Waterfall",          "wf:interest"],
        ["principal_wf", "principal_waterfall",         "Principal Waterfall",         "wf:principal"],
        ["excess_wf",    "excess_cashflow_waterfall",   "Excess Cashflow Waterfall",   "wf:excess"],
      ] as const).map(([openKey, wfKey, label, wfPrefix]) => {
        const handlers = openKey === "interest_wf" ? intWF : openKey === "principal_wf" ? prinWF : exWF;
        const steps = config[wfKey] ?? [];
        return (
          <div key={openKey} className="card">
            {sectionHdr(openKey, `${label} (${steps.length} steps)`)}
            {open[openKey] && (
              <div className="mt-4">
                <WaterfallTable
                  label={label} wfPrefix={wfPrefix} steps={steps}
                  editing={editing(openKey)}
                  onUpdate={handlers.onUpdate} onDelete={handlers.onDelete}
                  onAdd={handlers.onAdd} onMove={handlers.onMove}
                  annotations={annotations} annotPanel={annotPanel}
                  setAnnotPanel={setAnnotPanel} onAddAnnotEntry={handleAddAnnotEntry}
                />
              </div>
            )}
          </div>
        );
      })}

      {/* ── 8. Trigger Tests ────────────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("triggers", `Trigger Tests (${(config.triggers ?? []).length})`)}
        {open.triggers && (
          <div className="mt-4 overflow-x-auto">
            {(config.triggers ?? []).length === 0 && !editing("triggers")
              ? <p className="text-sm text-gray-400 text-center py-4">No trigger tests extracted</p>
              : (
                <table className="w-full text-sm">
                  <thead>
                    <tr className="table-header">
                      <th className={thCls}>Test Name</th><th className={thCls}>Type</th>
                      <th className={thCls}>Description</th>
                      <th className={thCls}>Trigger Logic</th><th className="w-7"></th>
                      {editing("triggers") && <th className={thCls}></th>}
                    </tr>
                  </thead>
                  <tbody>
                    {(config.triggers ?? []).map((t, i) => {
                      const rk = `triggers:${t.test_name || i}`;
                      return (
                        <React.Fragment key={i}>
                          <tr className={`${i % 2 === 0 ? "table-row-even" : "table-row-odd"} group`}>
                            <td className={tdCls}>{editing("triggers") ? <TxtInput value={t.test_name} onChange={(v) => updTrigger(i, { test_name: v })} className="w-40" /> : <span className="font-medium">{t.test_name || "—"}</span>}</td>
                            <td className={tdCls}>{editing("triggers") ? <SelectInput value={t.test_type} onChange={(v) => updTrigger(i, { test_type: v })} options={["oc", "ce", "cleanup_call", "delinquency", "other"]} className="w-28" /> : <span className="uppercase text-xs badge bg-gray-100 text-gray-700">{t.test_type}</span>}</td>
                            <td className={tdCls}>{editing("triggers") ? <TA value={t.description} onChange={(v) => updTrigger(i, { description: v })} rows={2} className="w-full min-w-[180px]" /> : <span className="text-gray-700 text-xs">{t.description || "—"}</span>}</td>
                            <td className={tdCls}>
                              {editing("triggers") ? (
                                <TriggerLogicEditor
                                  trigger={t}
                                  onUpdate={(patch) => updTrigger(i, patch)}
                                  enableNlAssist={enableTriggerNlAssist}
                                />
                              ) : (t.trigger_condition || t.trigger_action) ? (
                                <code className="block text-xs bg-gray-100 px-2 py-1.5 rounded font-mono text-gray-800 whitespace-pre min-w-[200px]">{`if ${t.trigger_condition || "…"}:\n    ${t.trigger_action || "…"} = True`}</code>
                              ) : <span className="text-gray-400">—</span>}
                            </td>
                            <td className="px-1 py-2 w-7"><AnnotIcon rowKey={rk} annotations={annotations} annotPanel={annotPanel} setAnnotPanel={setAnnotPanel} /></td>
                            {editing("triggers") && <td className={tdCls}><DelBtn onClick={() => delTrigger(i)} /></td>}
                          </tr>
                          {annotPanel?.key === rk && (
                            <tr><td colSpan={99} className="p-0 border-b border-slate-100"><div className="px-4 py-3 bg-slate-50/60"><AnnotationPanel entries={annotations[rk] ?? []} panelState={annotPanel} onChangeSender={(v) => setAnnotPanel({ ...annotPanel, newSender: v })} onChangeContent={(v) => setAnnotPanel({ ...annotPanel, newContent: v })} onAddEntry={handleAddAnnotEntry} onClose={() => setAnnotPanel(null)} /></div></td></tr>
                          )}
                        </React.Fragment>
                      );
                    })}
                  </tbody>
                </table>
              )}
            {editing("triggers") && <AddRowBtn label="Add Trigger" onClick={() => setConfig((p) => ({ ...p, triggers: [...(p.triggers ?? []), newTrigger()] }))} />}
          </div>
        )}
      </div>

      {/* ── 9. Loss Allocation Order ────────────────────────────────────────── */}
      <div className="card">
        {sectionHdr("loss", `Loss Allocation Order (${(config.loss_allocation_order ?? []).length} classes)`)}
        {open.loss && (
          <div className="mt-4">
            {(config.loss_allocation_order ?? []).length === 0 && !editing("loss")
              ? <p className="text-sm text-gray-400 text-center py-4">No loss allocation order extracted</p>
              : (
                <div className="space-y-2">
                  {(config.loss_allocation_order ?? []).map((cls, i) => (
                    <div key={i} className="flex items-center gap-3 px-3 py-2 bg-gray-50 rounded-lg border border-gray-200">
                      <span className="w-6 h-6 flex-shrink-0 bg-red-100 text-red-700 rounded-full flex items-center justify-center text-xs font-bold">{i + 1}</span>
                      {editing("loss") ? (
                        <>
                          <TxtInput value={cls} onChange={(v) => updLoss(i, v)} className="flex-1" />
                          <div className="flex gap-1">
                            <MoveBtn dir="up" onClick={() => moveLoss(i, -1)} />
                            <MoveBtn dir="down" onClick={() => moveLoss(i, 1)} />
                            <DelBtn onClick={() => delLoss(i)} />
                          </div>
                        </>
                      ) : (
                        <span className="text-sm font-medium text-gray-800 flex-1">{cls}</span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            {editing("loss") && <AddRowBtn label="Add Class to Loss Order" onClick={() => setConfig((p) => ({ ...p, loss_allocation_order: [...(p.loss_allocation_order ?? []), ""] }))} />}
          </div>
        )}
      </div>

      {/* ── Save All button ──────────────────────────────────────────────── */}
      <div className="flex justify-center pt-2 pb-4">
        <button
          onClick={handleSave}
          disabled={saving}
          className="btn-primary px-8 py-2.5 text-sm flex items-center gap-2 shadow-md"
        >
          {saving
            ? <><Loader2 size={15} className="animate-spin" /> Saving…</>
            : <><Save size={15} /> Save All Changes</>}
        </button>
      </div>
      </div>

      {/* ── Right side: PDF verification panel (sticky, only while editing) ── */}
      {splitActive && (
        <aside
          className="hidden lg:block lg:w-1/2 lg:sticky lg:top-4 lg:self-start"
          style={{ height: "calc(100vh - 6rem)" }}
        >
          <PDFVerificationPanel
            pdfUrl={pdfUrl}
            config={config}
            editingSection={editingSection}
            overriddenValues={overriddenValues}
            onClose={() => setPdfPanelOpen(false)}
          />
        </aside>
      )}

      {/* Floating reopen button when the user has collapsed the panel mid-edit. */}
      {showPdfPanel && editingSection !== null && !pdfPanelOpen && (
        <button
          onClick={() => setPdfPanelOpen(true)}
          className="hidden lg:flex fixed right-4 top-24 z-40 items-center gap-1.5 bg-white border border-slate-200 rounded-full shadow-md px-3 py-2 text-xs font-semibold text-slate-700 hover:bg-slate-50 transition-colors"
          title="Show PDF verification panel"
        >
          <PanelRightOpen size={14} /> Show PDF
        </button>
      )}
    </div>
  );
}
