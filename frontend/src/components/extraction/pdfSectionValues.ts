import type { DealConfig } from "../../types";

export type UiSectionKey = "deal_info" | "certificate_classes" | "fees" | "waterfall" | "triggers";

// Map ExtractedFields' editing-section keys to the backend's section_page_map keys.
const EDIT_SECTION_TO_UI: Record<string, UiSectionKey> = {
  deal: "deal_info",
  servicers: "deal_info",
  classes: "certificate_classes",
  fees: "fees",
  interest_wf: "waterfall",
  principal_wf: "waterfall",
  excess_wf: "waterfall",
  triggers: "triggers",
  loss: "triggers",
};

export function editSectionToUi(s: string | null): UiSectionKey | null {
  if (!s) return null;
  return EDIT_SECTION_TO_UI[s] ?? null;
}

const MONTHS_LONG = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const MONTHS_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function dateVariants(s: string): string[] {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s.trim());
  if (!m) return [s];
  const [, y, mo, d] = m;
  const moNum = parseInt(mo, 10);
  const dNum = parseInt(d, 10);
  const longMonth = MONTHS_LONG[moNum - 1];
  const abbrMonth = MONTHS_ABBR[moNum - 1];
  return [
    s,
    `${longMonth} ${dNum}, ${y}`,
    `${abbrMonth} ${dNum}, ${y}`,
    `${mo}/${d}/${y}`,
    `${moNum}/${dNum}/${y}`,
  ];
}

function numberVariants(n: number): string[] {
  const out = new Set<string>();
  if (!isFinite(n)) return [];
  out.add(String(n));
  out.add(n.toLocaleString("en-US"));
  out.add(n.toLocaleString("en-US", { maximumFractionDigits: 2 }));
  if (Number.isInteger(n)) out.add(n.toLocaleString("en-US", { maximumFractionDigits: 0 }));
  // Large round numbers often appear truncated, e.g. "186,391,076" or "$186,391,076.00"
  return Array.from(out);
}

function rateVariants(r: number): string[] {
  const out = new Set<string>();
  if (!isFinite(r)) return [];
  const pct = r * 100;
  for (const dec of [2, 3, 4, 5]) {
    out.add(`${pct.toFixed(dec)}%`);
    out.add(pct.toFixed(dec));
  }
  // basis points (only for small rates)
  if (Math.abs(r) < 0.1) {
    const bps = Math.round(pct * 100);
    out.add(`${bps} basis points`);
    out.add(`${bps} bps`);
  }
  return Array.from(out);
}

export function valuesForSection(uiSec: UiSectionKey, cfg: DealConfig): string[] {
  const out: string[] = [];
  const push = (s: string | number | null | undefined) => {
    if (s == null || s === "") return;
    const str = String(s).trim();
    if (str.length >= 2) out.push(str);
  };

  if (uiSec === "deal_info") {
    push(cfg.deal_name);
    push(cfg.issuing_entity);
    push(cfg.series);
    push(cfg.depositor);
    push(cfg.lien_position);
    push(cfg.asset_class);
    push(cfg.asset_type);
    push(cfg.payment_frequency);
    push(cfg.custodian);
    push(cfg.securities_administrator);
    push(cfg.owner_trustee);
    push(cfg.benchmark);
    push(cfg.benchmark_tenor);
    push(cfg.interest_day_count);
    for (const d of [cfg.closing_date, cfg.cut_off_date, cfg.first_payment_date, cfg.legal_maturity_date, cfg.pricing_date, cfg.revolving_period_end_date]) {
      if (d) dateVariants(d).forEach(push);
    }
    if (cfg.original_pool_balance) numberVariants(cfg.original_pool_balance).forEach(push);
    for (const sp of cfg.sponsors ?? []) push(sp);
    for (const sv of cfg.servicers ?? []) {
      push(sv.servicer_name);
      if (sv.servicing_fee_rate) rateVariants(sv.servicing_fee_rate).forEach(push);
    }
  }

  if (uiSec === "certificate_classes") {
    for (const c of cfg.classes ?? []) {
      push(c.class_name);
      push(`Class ${c.class_name}`);
      push(c.cusip);
      if (c.initial_principal) numberVariants(c.initial_principal).forEach(push);
      if (c.fixed_rate) rateVariants(c.fixed_rate).forEach(push);
      if (c.margin) rateVariants(c.margin).forEach(push);
      if (c.rate_cap) rateVariants(c.rate_cap).forEach(push);
      push(c.fitch_rating);
      push(c.moodys_rating);
      push(c.sp_rating);
      push(c.kbra_rating);
    }
  }

  if (uiSec === "fees") {
    for (const f of cfg.fees ?? []) {
      push(f.fee_name);
      if (f.fee_rate) rateVariants(f.fee_rate).forEach(push);
      if (f.fixed_amount) numberVariants(f.fixed_amount).forEach(push);
      push(f.servicer_name);
    }
  }

  if (uiSec === "waterfall") {
    const allSteps = [
      ...(cfg.interest_waterfall ?? []),
      ...(cfg.principal_waterfall ?? []),
      ...(cfg.excess_cashflow_waterfall ?? []),
    ];
    for (const s of allSteps) {
      if (s.description) {
        const desc = s.description.trim();
        if (desc.length >= 5) out.push(desc.slice(0, 80));
      }
      if (s.condition && s.condition !== "always") push(s.condition);
      if (s.class_name) push(`Class ${s.class_name}`);
    }
  }

  if (uiSec === "triggers") {
    for (const t of cfg.triggers ?? []) {
      push(t.test_name);
      if (t.description) {
        const d = t.description.trim();
        if (d.length >= 5) out.push(d.slice(0, 80));
      }
      if (typeof t.threshold === "number" && t.threshold) numberVariants(t.threshold).forEach(push);
      else if (typeof t.threshold === "string") push(t.threshold);
    }
    for (const cls of cfg.loss_allocation_order ?? []) push(`Class ${cls}`);
  }

  // Dedupe and sort longest-first so more specific matches win.
  return Array.from(new Set(out)).sort((a, b) => b.length - a.length);
}
