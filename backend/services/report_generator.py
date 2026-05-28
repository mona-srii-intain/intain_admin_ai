"""
Report Generator - creates comprehensive investor reports from waterfall computation results.

Outputs:
  1. Structured JSON report (for API responses)
  2. Vector PDF report (downloadable investor report) — all charts rendered as
     native reportlab.graphics primitives so they remain crisp at any zoom.

Section outline (Part C of the upgrade spec):
  0. Contact Information  [omitted — no source schema]
  1. Payments         1(a) Summary, 1(b) Interest, 1(c) Cap Carryover,
                      1(d) Principal, 1(e) Factors, 1(f) Cumulative
  2. Collateral       2(a) Summary, 2(b) Performance, 2(c) Rates,
                      2(d) Realized Loss, 2(e) Structural Features
  3. Accounts         3(a) Collections, 3(b) Reserve Accounts
  4. Fees
  5. Expenses
  6. Events (Triggers)
  7. Servicer Balances
  8. Priority of Payments
  9. Loan Details     9(a) PIF, 9(b) REO, 9(c) Foreclosure, 9(d) Bankruptcy,
                      9(e) Modifications, 9(f) Forbearance
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from models.waterfall import WaterfallResult

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import inch, mm
    from reportlab.platypus import (
        HRFlowable,
        KeepTogether,
        PageBreak,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )
    from reportlab.graphics.shapes import Drawing, String, Rect
    from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
    from reportlab.graphics.charts.lineplots import LinePlot
    from reportlab.graphics.charts.piecharts import Pie
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False

REPORTS_DIR = Path(__file__).resolve().parents[1] / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Color palette (preserve existing template; add gold for series + amber for chips)
# ---------------------------------------------------------------------------
NAVY = colors.HexColor("#0A2342") if REPORTLAB_AVAILABLE else None
BLUE = colors.HexColor("#1565C0") if REPORTLAB_AVAILABLE else None
LIGHT_BLUE = colors.HexColor("#E3F2FD") if REPORTLAB_AVAILABLE else None
LIGHT_GRAY = colors.HexColor("#F5F5F5") if REPORTLAB_AVAILABLE else None
WHITE = colors.white if REPORTLAB_AVAILABLE else None
GOLD = colors.HexColor("#C9A24A") if REPORTLAB_AVAILABLE else None
# Status colors — used ONLY on trigger chips and dashboard KPIs
GREEN = colors.HexColor("#1B5E20") if REPORTLAB_AVAILABLE else None
AMBER = colors.HexColor("#F57C00") if REPORTLAB_AVAILABLE else None
RED = colors.HexColor("#C62828") if REPORTLAB_AVAILABLE else None
GREEN_FILL = colors.HexColor("#C8E6C9") if REPORTLAB_AVAILABLE else None
AMBER_FILL = colors.HexColor("#FFE0B2") if REPORTLAB_AVAILABLE else None
RED_FILL = colors.HexColor("#FFCDD2") if REPORTLAB_AVAILABLE else None


def _fmt_currency(val: Optional[float], decimals: int = 2) -> str:
    if val is None:
        return ""
    return f"${val:,.{decimals}f}"


def _fmt_pct(val: Optional[float], decimals: int = 5) -> str:
    if val is None:
        return ""
    return f"{val * 100:.{decimals}f}%"


def _fmt_num(val: Optional[float], decimals: int = 6) -> str:
    if val is None:
        return ""
    return f"{val:,.{decimals}f}"


def _fmt_int(val: Optional[float]) -> str:
    if val is None:
        return ""
    return f"{int(val):,}"


def _day_count_label(convention: Optional[str]) -> str:
    """Display label for Section 1(b) Day Count Method column."""
    conv = (convention or "actual/360").lower()
    if "actual/actual" in conv or "act/act" in conv:
        return "Act/Act"
    if "30/360" in conv or conv.startswith("30"):
        return "30/360"
    if "365" in conv:
        return "Act/365"
    return "Act/360"


# ---------------------------------------------------------------------------
# JSON report (nested view used for completeness; the API returns the flat
# WaterfallResult to the frontend, this is for archival / future use).
# ---------------------------------------------------------------------------

def build_json_report(result: WaterfallResult) -> Dict[str, Any]:
    cd_list = result.class_details

    def _cls_dict(cd):
        return {
            "class_name": cd.class_name,
            "cusip": cd.cusip,
            "type": cd.class_type,
            "original_principal": cd.original_principal,
            "beginning_principal": cd.beginning_principal,
            "interest_rate": cd.interest_rate,
            "interest_paid": cd.interest_paid,
            "principal_paid": cd.principal_paid,
            "total_paid": cd.total_paid,
            "ending_principal": cd.ending_principal,
        }

    report: Dict[str, Any] = {
        "meta": {
            "deal_id": result.deal_id,
            "deal_name": result.deal_name,
            "distribution_date": result.distribution_date,
            "report_type": result.report_type,
            "asset_class": result.asset_class,
            "accrual_period": f"{result.accrual_start_date} to {result.accrual_end_date}",
            "benchmark": result.benchmark,
            "benchmark_rate": result.benchmark_rate,
            "net_wac": result.net_wac,
            "gross_wac": result.gross_wac,
            "computed_at": result.computed_at,
        },

        # ── Dashboard (page 1) ────────────────────────────────────────────
        "dashboard": {
            "kpis": [
                {"label": k.label, "value": k.value, "raw": k.raw}
                for k in (result.dashboard_kpis or [])
            ],
            "trigger_chips": [
                {
                    "name": c.name, "status": c.status,
                    "current_value": c.current_value, "threshold": c.threshold,
                    "margin_pct": c.margin_pct, "description": c.description,
                }
                for c in (result.trigger_chips or [])
            ],
        },

        # ── Section 1 Payments ─────────────────────────────────────────────
        "section_1a_payments_summary": {
            "classes": [_cls_dict(cd) for cd in cd_list],
            "totals": {
                "original_principal": result.total_original_principal,
                "beginning_principal": result.total_beginning_principal,
                "interest_paid": result.total_interest_paid,
                "principal_paid": result.total_principal_paid,
                "total_paid": result.total_paid,
                "ending_principal": result.total_ending_principal,
            },
        },
        "section_1b_interest": [
            {
                "class_name": cd.class_name,
                "accrual_start": cd.accrual_start,
                "accrual_end": cd.accrual_end,
                "days_accrued": cd.days_accrued,
                "day_count_method": _day_count_label(cd.day_count_method),
                "interest_rate": cd.interest_rate,
                "prior_unpaid_interest": cd.beginning_interest_carryforward,
                "optimal_interest": cd.interest_accrued,
                "total_due": cd.total_interest_due,
                "interest_paid": cd.interest_paid,
                "ending_carryforward": cd.ending_interest_carryforward,
            }
            for cd in cd_list
        ],
        "section_1c_cap_carryover": [
            {
                "class_name": cd.class_name,
                "wac_cap": result.net_wac,  # WAC cap (uniform across classes)
                "beginning_cap_carryover": cd.beginning_cap_carryover,
                "current_cap_carryover": cd.current_cap_carryover,
                "total_cap_carryover": cd.total_cap_carryover,
                "cap_carryover_paid": cd.cap_carryover_paid,
                "ending_cap_carryover": cd.ending_cap_carryover,
            }
            for cd in cd_list
        ],
        "section_1d_principal": [
            {
                "class_name": cd.class_name,
                "beginning_principal": cd.beginning_principal,
                "principal_paid": cd.principal_paid,
                "writedown_amount": cd.writedown_amount,
                "cumulative_writedown": cd.cumulative_writedown,
                "realized_loss": cd.realized_loss,
                "cumulative_realized_loss": cd.cumulative_realized_loss,
                "ending_principal": cd.ending_principal,
            }
            for cd in cd_list
        ],
        "section_1e_factors": [
            {
                "class_name": cd.class_name,
                "factor_beginning": cd.factor_beginning,
                "factor_interest": cd.factor_interest,
                "factor_principal": cd.factor_principal,
                "factor_total": cd.factor_total,
                "factor_ending": cd.factor_ending,
                "record_date": cd.record_date,
            }
            for cd in cd_list
        ],
        "section_1f_cumulative": [
            {
                "class_name": cd.class_name,
                "original_principal": cd.original_principal,
                "cumulative_interest_paid": cd.cumulative_interest_paid,
                "cumulative_principal_paid": cd.cumulative_principal_paid,
                "cumulative_total_distribution": cd.cumulative_total_distribution,
                "cumulative_realized_loss": cd.cumulative_realized_loss,
                "cumulative_deferred_interest": cd.cumulative_deferred_interest,
                "ending_balance": cd.ending_principal,
            }
            for cd in cd_list
        ],

        # ── Section 2 Collateral ──────────────────────────────────────────
        "section_2a_collateral_summary": {
            "original_balance": result.original_pool_balance,
            "prior_balance": result.prior_pool_balance,
            "purchases": result.purchases,
            "funded_draws": result.funded_draws,
            "capitalized_amounts": result.capitalized_amounts,
            "less_scheduled_principal": result.scheduled_principal_collateral,
            "less_curtailments": result.curtailments,
            "less_prepayments_in_full": result.prepayments_in_full,
            "less_repurchases": result.repurchases,
            "less_charge_offs": result.charge_offs,
            "less_sales": result.sales,
            "less_liquidations": result.liquidations,
            "less_realized_losses": result.realized_losses_collateral,
            "other": result.other_collateral,
            "current_balance": result.current_pool_balance,
            "current_loan_count": result.current_loan_count,
        },
        "section_2b_performance": {
            "buckets_1d": [
                {"bucket": b.bucket, "amount": b.amount, "count": b.count,
                 "pct_amount": b.pct_amount, "pct_count": b.pct_count}
                for b in result.performance_buckets
            ],
            "matrix": (result.delinquency_matrix.model_dump()
                       if result.delinquency_matrix else None),
        },
        "section_2c_rates": {
            "current_month": result.collateral_rates.model_dump(),
            "history": [h.model_dump() for h in result.performance_history],
        },
        "section_2d_realized_loss": (
            result.collateral_realized_loss.model_dump()
            if result.collateral_realized_loss else None
        ),
        "section_2e_structural_features": (
            result.structural_features.model_dump()
            if result.structural_features else None
        ),

        # ── Section 3 Accounts ────────────────────────────────────────────
        "section_3a_collections": {
            "principal_collections": {
                "scheduled_principal": result.principal_scheduled,
                "curtailments": result.principal_curtailments,
                "prepayments_in_full": result.principal_prepayments_full,
                "sales": result.principal_sales,
                "liquidations": result.principal_liquidations,
                "repurchases": result.principal_repurchases,
                "recoveries": result.principal_recoveries,
                "other": result.principal_other,
                "total_net_principal": result.principal_remittance_amount,
            },
            "interest_collections": {
                "gross_interest": result.gross_interest_collected,
                "less_servicing_fees": -result.servicing_fees_paid,
                "less_deal_fees": -result.deal_fees_paid,
                "less_deal_expenses": -result.deal_expenses_paid,
                "other_amounts": result.other_collections,
                "total_net_interest": result.interest_remittance_amount,
            },
            "total_available_funds": {
                "principal": result.principal_remittance_amount,
                "interest": result.interest_remittance_amount,
                "total": result.available_funds,
            },
        },
        "section_3b_accounts": [
            {
                "account_name": a.account_name,
                "beginning_balance": a.beginning_balance,
                "deposits": a.deposits,
                "withdrawals": a.withdrawals,
                "ending_balance": a.ending_balance_post_payment,
                "required_balance": a.required_balance,
            }
            for a in result.reserve_accounts
        ],

        # ── Section 4 Fees / 5 Expenses ───────────────────────────────────
        "section_4_fees": [f.model_dump() for f in result.fees_detail],
        "section_5_expenses": [e.model_dump() for e in result.expenses_detail],

        # ── Section 6 Events ──────────────────────────────────────────────
        "section_6_events": [e.model_dump() for e in result.events],

        # ── Section 7 Servicer Balances ───────────────────────────────────
        "section_7_servicer_balances": [s.model_dump() for s in result.servicer_balances],

        # ── Section 8 Priority of Payments ────────────────────────────────
        "section_8_priority_of_payments": {
            "active_branch": result.active_trigger_branch,
            "interest": [s.model_dump() for s in result.waterfall_trace_interest],
            "principal_actual": [s.model_dump() for s in result.waterfall_trace_principal],
            "principal_no_trigger": [s.model_dump() for s in result.waterfall_trace_principal_no_trigger],
            "principal_with_trigger": [s.model_dump() for s in result.waterfall_trace_principal_with_trigger],
            "excess_cashflow": [s.model_dump() for s in result.waterfall_trace_excess],
            "allocation": [a.model_dump() for a in result.distribution_allocation],
        },

        # ── Section 9 Loan Details ────────────────────────────────────────
        "section_9_loan_details": {
            "paid_in_full": [
                {"loan_id": l.loan_id, "beginning_principal": l.beginning_principal,
                 "ending_principal": l.ending_principal}
                for l in result.loans_paid_in_full
            ],
            "reo": [
                {"loan_id": l.loan_id, "beginning_principal": l.beginning_principal,
                 "ending_principal": l.ending_principal}
                for l in result.loans_reo
            ],
            "foreclosure": [
                {"loan_id": l.loan_id, "beginning_principal": l.beginning_principal,
                 "ending_principal": l.ending_principal}
                for l in result.loans_foreclosure
            ],
            "bankruptcy": [
                {"loan_id": l.loan_id, "beginning_principal": l.beginning_principal,
                 "ending_principal": l.ending_principal}
                for l in result.loans_bankruptcy
            ],
            "modifications": [
                {"loan_id": l.loan_id, "ending_principal": l.ending_principal,
                 "status": l.status}
                for l in result.loans_modified
            ],
            "forbearance": [
                {"loan_id": l.loan_id, "deferred_amount": l.deferred_amount,
                 "cumulative_deferred": l.cumulative_deferred}
                for l in result.loans_forbearance
            ],
        },
    }
    return report


# ---------------------------------------------------------------------------
# Chart helpers (all native reportlab.graphics → vector PDF output)
# ---------------------------------------------------------------------------

def _chart_horizontal_bar(
    values: List[float], labels: List[str], width: float = 480, height: float = 140,
    bar_color=None,
) -> "Drawing":
    """Single-series horizontal bar chart. Used by 1(a) tranche balances + 8 allocation."""
    if bar_color is None:
        bar_color = NAVY
    d = Drawing(width, height)
    chart = HorizontalBarChart()
    chart.x = 80
    chart.y = 20
    chart.width = width - 100
    chart.height = height - 40
    chart.data = [values]
    chart.categoryAxis.categoryNames = labels
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontSize = 6
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = bar_color
    chart.bars[0].strokeColor = bar_color
    chart.barWidth = 8
    d.add(chart)
    return d


def _chart_grouped_bar(
    group_labels: List[str], series_a: List[float], series_b: List[float],
    series_a_name: str = "Series A", series_b_name: str = "Series B",
    width: float = 320, height: float = 160,
) -> "Drawing":
    """Two-series vertical grouped bar chart. Used by 2(e) CE original vs current."""
    d = Drawing(width, height)
    chart = VerticalBarChart()
    chart.x = 40
    chart.y = 40
    chart.width = width - 60
    chart.height = height - 70
    chart.data = [series_a, series_b]
    chart.categoryAxis.categoryNames = group_labels
    chart.categoryAxis.labels.fontSize = 7
    chart.valueAxis.labels.fontSize = 6
    chart.valueAxis.valueMin = 0
    chart.bars[0].fillColor = NAVY
    chart.bars[0].strokeColor = NAVY
    chart.bars[1].fillColor = GOLD
    chart.bars[1].strokeColor = GOLD
    chart.barWidth = 10
    chart.groupSpacing = 12
    # Manual legend (reportlab Legend can be fiddly)
    d.add(Rect(40, 10, 10, 8, fillColor=NAVY, strokeColor=NAVY))
    d.add(String(54, 12, series_a_name, fontSize=7))
    d.add(Rect(140, 10, 10, 8, fillColor=GOLD, strokeColor=GOLD))
    d.add(String(154, 12, series_b_name, fontSize=7))
    d.add(chart)
    return d


def _chart_doughnut(
    values: List[float], labels: List[str], width: float = 240, height: float = 160,
) -> "Drawing":
    """Doughnut chart. Used by 2(b) delinquency distribution."""
    d = Drawing(width, height)
    pie = Pie()
    pie.x = 30
    pie.y = 10
    pie.width = 130
    pie.height = 130
    pie.data = values
    pie.labels = labels
    pie.slices.strokeColor = WHITE
    pie.slices.strokeWidth = 1
    pie.simpleLabels = 1
    # Color slices: greens for current/1-29, ambers for 30-89, reds for 90+.
    slice_colors = [
        colors.HexColor("#1B5E20"),  # Current
        colors.HexColor("#4CAF50"),  # 1-29
        colors.HexColor("#FF9800"),  # 30-59
        colors.HexColor("#F57C00"),  # 60-89
        colors.HexColor("#E53935"),  # 90-119
        colors.HexColor("#C62828"),  # 120-149
        colors.HexColor("#B71C1C"),  # 150-179
        colors.HexColor("#7F0000"),  # 180+
    ]
    for i, c in enumerate(slice_colors):
        if i < len(pie.slices):
            pie.slices[i].fillColor = c
    pie.slices[0].popout = 0
    # Inner hole for doughnut effect — drawn as a white circle on top.
    from reportlab.graphics.shapes import Circle
    cx = pie.x + pie.width / 2
    cy = pie.y + pie.height / 2
    d.add(pie)
    d.add(Circle(cx, cy, 30, fillColor=WHITE, strokeColor=WHITE))
    # Manual legend on right side
    for i, lbl in enumerate(labels[:8]):
        y = height - 12 - i * 14
        color = slice_colors[i] if i < len(slice_colors) else NAVY
        d.add(Rect(170, y, 8, 8, fillColor=color, strokeColor=color))
        d.add(String(182, y + 1, f"{lbl}", fontSize=6))
    return d


def _chart_line(
    x_labels: List[str],
    series_named: List[tuple],  # [(name, color, [y values]), ...]
    width: float = 480, height: float = 160,
) -> "Drawing":
    """Multi-series line chart. Used by 2(c) CPR/CDR history."""
    d = Drawing(width, height)
    plot = LinePlot()
    plot.x = 50
    plot.y = 30
    plot.width = width - 70
    plot.height = height - 60
    n_points = len(x_labels) if x_labels else 1
    plot_data = []
    for _name, _color, ys in series_named:
        plot_data.append(list(zip(range(n_points), ys)))
    plot.data = plot_data
    for i, (_name, color, _ys) in enumerate(series_named):
        plot.lines[i].strokeColor = color
        plot.lines[i].strokeWidth = 1.5
    plot.xValueAxis.labels.fontSize = 6
    plot.yValueAxis.labels.fontSize = 6
    plot.xValueAxis.valueMin = 0
    plot.xValueAxis.valueMax = max(0, n_points - 1)
    plot.xValueAxis.valueSteps = list(range(n_points))
    # Manual x-axis tick labels
    for i, lbl in enumerate(x_labels):
        if i % max(1, n_points // 6) == 0:
            xpos = plot.x + (i / max(1, n_points - 1)) * plot.width
            d.add(String(xpos - 12, plot.y - 12, str(lbl)[:10], fontSize=5))
    d.add(plot)
    # Legend
    for i, (name, color, _ys) in enumerate(series_named):
        x_off = 50 + i * 80
        d.add(Rect(x_off, 8, 10, 5, fillColor=color, strokeColor=color))
        d.add(String(x_off + 14, 7, name, fontSize=7))
    return d


# ---------------------------------------------------------------------------
# PDF Report
# ---------------------------------------------------------------------------

def generate_pdf_report(result: WaterfallResult, output_path: Optional[str] = None) -> bytes:
    """Generate the full investor PDF."""
    if not REPORTLAB_AVAILABLE:
        raise ImportError("reportlab is not installed. Install it with: pip install reportlab")

    buffer = io.BytesIO()

    if not output_path:
        safe_date = result.payment_date.replace("-", "")
        output_path = str(REPORTS_DIR / result.deal_id / f"report_{safe_date}.pdf")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=18 * mm,
        bottomMargin=15 * mm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "Title", parent=styles["Heading1"],
        fontSize=14, textColor=NAVY, alignment=TA_CENTER, spaceAfter=6,
    )
    h2_style = ParagraphStyle(
        "H2", parent=styles["Heading2"],
        fontSize=11, textColor=NAVY, spaceAfter=4, spaceBefore=8,
    )
    h3_style = ParagraphStyle(
        "H3", parent=styles["Heading3"],
        fontSize=9, textColor=BLUE, spaceAfter=3, spaceBefore=6,
    )
    body_style = ParagraphStyle("Body", parent=styles["Normal"], fontSize=7, spaceAfter=2)
    small_style = ParagraphStyle("Small", parent=styles["Normal"], fontSize=6.5, spaceAfter=1)

    def _table_style_base(col_count: int) -> TableStyle:
        return TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), NAVY),
            ("TEXTCOLOR", (0, 0), (-1, 0), WHITE),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 6.5),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [WHITE, LIGHT_GRAY]),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("LEFTPADDING", (0, 0), (-1, -1), 3),
            ("RIGHTPADDING", (0, 0), (-1, -1), 3),
            ("ALIGN", (1, 1), (-1, -1), "RIGHT"),
            ("ALIGN", (0, 0), (0, -1), "LEFT"),
        ])

    story: List[Any] = []
    cd_list = result.class_details

    # Find each class's accrual_convention from the prior data — we need it
    # for the Day Count Method column. ClassPaymentSummary doesn't carry it,
    # so derive from class_days vs the period's actual days.
    pool_actual_days = result.days_accrued
    def _conv_label_for_cd(cd) -> str:
        if cd.days_accrued == 30 and pool_actual_days != 30:
            return "30/360"
        return "Act/360"

    # ─────────────────────────────────────────────────────────────────────
    # PAGE 1 — DASHBOARD
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph(result.deal_name.upper(), title_style))
    story.append(Paragraph(
        f"Distribution Date: {result.distribution_date}  |  Report Type: {result.report_type}  |  "
        f"Asset Class: {result.asset_class}",
        ParagraphStyle("subtitle", parent=styles["Normal"], fontSize=8, alignment=TA_CENTER, textColor=NAVY),
    ))
    story.append(HRFlowable(width="100%", thickness=1.5, color=NAVY))
    story.append(Spacer(1, 8))

    # KPI strip — 6 tiles in a single row table, navy fill, white text
    kpis = result.dashboard_kpis or []
    if kpis:
        kpi_row_labels = [Paragraph(
            f'<font color="white"><b>{k.label.upper()}</b></font>',
            ParagraphStyle("kpil", fontSize=6.5, alignment=TA_CENTER, textColor=WHITE)
        ) for k in kpis]
        kpi_row_values = [Paragraph(
            f'<font color="white"><b>{k.value}</b></font>',
            ParagraphStyle("kpiv", fontSize=11, alignment=TA_CENTER, textColor=WHITE)
        ) for k in kpis]
        kpi_table = Table([kpi_row_labels, kpi_row_values], colWidths=[120] * len(kpis))
        kpi_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), NAVY),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("TOPPADDING", (0, 0), (-1, -1), 6),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("GRID", (0, 0), (-1, -1), 0.5, WHITE),
        ]))
        story.append(kpi_table)
        story.append(Spacer(1, 10))

    # Trigger chips — 3 chips in a row, color-coded by status
    chips = result.trigger_chips or []
    if chips:
        story.append(Paragraph("TRIGGER STATUS", h3_style))
        chip_row = []
        for c in chips:
            if c.status == "green":
                bg, fg = GREEN_FILL, GREEN
            elif c.status == "amber":
                bg, fg = AMBER_FILL, AMBER
            else:
                bg, fg = RED_FILL, RED
            chip = Table([[
                Paragraph(f'<b>{c.name}</b>', ParagraphStyle("chip", fontSize=8, textColor=fg)),
                Paragraph(f'<font color="{fg.hexval()}">{c.status.upper()}</font>',
                          ParagraphStyle("chipS", fontSize=8, alignment=TA_RIGHT, textColor=fg)),
            ]], colWidths=[160, 80])
            chip.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), bg),
                ("BOX", (0, 0), (-1, -1), 1, fg),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ]))
            chip_row.append(chip)
        chip_layout = Table([chip_row], colWidths=[245] * len(chip_row))
        chip_layout.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        story.append(chip_layout)
        story.append(Spacer(1, 6))

    story.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(a) — PAYMENTS SUMMARY (with horizontal bar chart)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(a) PAYMENTS: SUMMARY", h2_style))
    # Chart: ending balance per class
    sum_chart_labels = [cd.class_name for cd in cd_list]
    sum_chart_values = [cd.ending_principal / 1_000_000 for cd in cd_list]
    if sum_chart_values:
        story.append(_chart_horizontal_bar(sum_chart_values, sum_chart_labels))
        story.append(Spacer(1, 4))

    sum_headers = ["Class", "CUSIP", "Type", "Original Principal", "Beginning Principal",
                   "Interest Paid", "Principal Paid", "Total Paid", "Ending Principal"]
    sum_data = [sum_headers]
    for cd in cd_list:
        sum_data.append([
            cd.class_name, cd.cusip or "", cd.class_type,
            _fmt_currency(cd.original_principal),
            _fmt_currency(cd.beginning_principal),
            _fmt_currency(cd.interest_paid),
            _fmt_currency(cd.principal_paid),
            _fmt_currency(cd.total_paid),
            _fmt_currency(cd.ending_principal),
        ])
    sum_data.append([
        "Total:", "", "",
        _fmt_currency(result.total_original_principal),
        _fmt_currency(result.total_beginning_principal),
        _fmt_currency(result.total_interest_paid),
        _fmt_currency(result.total_principal_paid),
        _fmt_currency(result.total_paid),
        _fmt_currency(result.total_ending_principal),
    ])
    t = Table(sum_data, repeatRows=1)
    ts = _table_style_base(len(sum_headers))
    ts.add("FONTNAME", (0, len(sum_data) - 1), (-1, len(sum_data) - 1), "Helvetica-Bold")
    ts.add("BACKGROUND", (0, len(sum_data) - 1), (-1, len(sum_data) - 1), LIGHT_BLUE)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(b) — INTEREST  (formerly 1(c))
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(b) PAYMENTS: INTEREST", h2_style))
    int_headers = ["Class", "Accrual Start", "Accrual End", "Days", "Day Count",
                   "Rate", "Prior Unpaid", "Optimal Interest", "Total Due",
                   "Interest Paid", "End. Carryforward"]
    int_data = [int_headers]
    for cd in cd_list:
        int_data.append([
            cd.class_name,
            cd.accrual_start, cd.accrual_end,
            str(cd.days_accrued),
            _conv_label_for_cd(cd),
            _fmt_pct(cd.interest_rate),
            _fmt_currency(cd.beginning_interest_carryforward),
            _fmt_currency(cd.interest_accrued),
            _fmt_currency(cd.total_interest_due),
            _fmt_currency(cd.interest_paid),
            _fmt_currency(cd.ending_interest_carryforward),
        ])
    t = Table(int_data, repeatRows=1)
    t.setStyle(_table_style_base(len(int_headers)))
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(c) — CAP CARRYOVER (WAC Cap column instead of class rate)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(c) PAYMENTS: CAP CARRYOVER", h2_style))
    cap_headers = ["Class", "WAC Cap", "Beg. Cap Carryover", "Current Cap Carryover",
                   "Total Cap Carryover", "Cap Carryover Paid", "End. Cap Carryover"]
    cap_data = [cap_headers]
    for cd in cd_list:
        cap_data.append([
            cd.class_name,
            _fmt_pct(result.net_wac),
            _fmt_currency(cd.beginning_cap_carryover),
            _fmt_currency(cd.current_cap_carryover),
            _fmt_currency(cd.total_cap_carryover),
            _fmt_currency(cd.cap_carryover_paid),
            _fmt_currency(cd.ending_cap_carryover),
        ])
    t = Table(cap_data, repeatRows=1)
    t.setStyle(_table_style_base(len(cap_headers)))
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(d) — PRINCIPAL  (formerly 1(e))
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(d) PAYMENTS: PRINCIPAL", h2_style))
    prin_headers = ["Class", "Beginning Principal", "Principal Paid", "Writeup/(Down)",
                    "Cum. Writedown", "Realized Loss", "Cum. Realized Loss", "Ending Principal"]
    prin_data = [prin_headers]
    for cd in cd_list:
        prin_data.append([
            cd.class_name,
            _fmt_currency(cd.beginning_principal),
            _fmt_currency(cd.principal_paid),
            _fmt_currency(cd.writeup_amount - cd.writedown_amount),
            _fmt_currency(cd.cumulative_writedown),
            _fmt_currency(cd.realized_loss),
            _fmt_currency(cd.cumulative_realized_loss),
            _fmt_currency(cd.ending_principal),
        ])
    prin_data.append([
        "Total:",
        _fmt_currency(result.total_beginning_principal),
        _fmt_currency(result.total_principal_paid),
        "", "", "", "",
        _fmt_currency(result.total_ending_principal),
    ])
    t = Table(prin_data, repeatRows=1)
    ts = _table_style_base(len(prin_headers))
    ts.add("FONTNAME", (0, len(prin_data) - 1), (-1, len(prin_data) - 1), "Helvetica-Bold")
    ts.add("BACKGROUND", (0, len(prin_data) - 1), (-1, len(prin_data) - 1), LIGHT_BLUE)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(e) — FACTORS  (formerly 1(f))
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(e) PAYMENTS: FACTORS", h2_style))
    fac_headers = ["Class", "Beginning Factor", "Interest Factor", "Principal Factor",
                   "Total Factor", "Ending Factor", "Record Date"]
    fac_data = [fac_headers]
    for cd in cd_list:
        fac_data.append([
            cd.class_name,
            _fmt_num(cd.factor_beginning),
            _fmt_num(cd.factor_interest),
            _fmt_num(cd.factor_principal),
            _fmt_num(cd.factor_total),
            _fmt_num(cd.factor_ending),
            cd.record_date,
        ])
    t = Table(fac_data, repeatRows=1)
    t.setStyle(_table_style_base(len(fac_headers)))
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 1(f) — CUMULATIVE PAYMENT DETAIL  (NEW)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("1(f) PAYMENTS: CUMULATIVE PAYMENT DETAIL", h2_style))
    cum_headers = ["Class", "Original Principal", "Cum. Interest Paid",
                   "Cum. Principal Paid", "Cum. Total Distribution",
                   "Cum. Realized Loss", "Cum. Deferred Interest", "Ending Balance"]
    cum_data = [cum_headers]
    for cd in cd_list:
        cum_data.append([
            cd.class_name,
            _fmt_currency(cd.original_principal),
            _fmt_currency(cd.cumulative_interest_paid),
            _fmt_currency(cd.cumulative_principal_paid),
            _fmt_currency(cd.cumulative_total_distribution),
            _fmt_currency(cd.cumulative_realized_loss),
            _fmt_currency(cd.cumulative_deferred_interest),
            _fmt_currency(cd.ending_principal),
        ])
    t = Table(cum_data, repeatRows=1)
    t.setStyle(_table_style_base(len(cum_headers)))
    story.append(t)

    story.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 2(a) — COLLATERAL SUMMARY
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2(a) COLLATERAL: SUMMARY", h2_style))
    coll_data = [
        ["Description", "Amount", "Count"],
        ["Original Balance", _fmt_currency(result.original_pool_balance), ""],
        ["Prior Balance", _fmt_currency(result.prior_pool_balance),
         str(result.current_loan_count + len(result.loans_paid_in_full))],
        ["Plus: Purchases", _fmt_currency(result.purchases), "0"],
        ["Plus: Funded Draws", _fmt_currency(result.funded_draws), ""],
        ["Plus: Capitalized Amounts", _fmt_currency(result.capitalized_amounts), ""],
        ["Less: Scheduled Principal", _fmt_currency(result.scheduled_principal_collateral), ""],
        ["Less: Curtailments", _fmt_currency(result.curtailments), ""],
        ["Less: Prepayments In Full", _fmt_currency(result.prepayments_in_full),
         str(len(result.loans_paid_in_full))],
        ["Less: Repurchases/Substitutions", _fmt_currency(result.repurchases), "0"],
        ["Less: Charged-Offs", _fmt_currency(result.charge_offs), "0"],
        ["Less: Sales", _fmt_currency(result.sales), "0"],
        ["Less: Liquidations", _fmt_currency(result.liquidations), "0"],
        ["Less: Realized Losses", _fmt_currency(result.realized_losses_collateral), "0"],
        ["Plus/(Less): Other", _fmt_currency(result.other_collateral), ""],
        ["Current Balance", _fmt_currency(result.current_pool_balance), str(result.current_loan_count)],
    ]
    t = Table(coll_data, colWidths=[220, 130, 80], repeatRows=1)
    ts = _table_style_base(3)
    ts.add("FONTNAME", (0, len(coll_data) - 1), (-1, len(coll_data) - 1), "Helvetica-Bold")
    ts.add("BACKGROUND", (0, len(coll_data) - 1), (-1, len(coll_data) - 1), LIGHT_BLUE)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 2(b) — COLLATERAL PERFORMANCE (chart + 2D matrix)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2(b) COLLATERAL: PERFORMANCE", h2_style))
    # Doughnut chart of pool by DPD bucket (% of pool balance)
    dq_buckets_for_chart = [b for b in result.performance_buckets
                            if b.bucket in ("Current", "1-29 Days", "30-59 Days", "60-89 Days",
                                            "90-119 Days", "120-149 Days", "150-179 Days", "180+ Days")
                            and b.pct_amount > 0]
    if dq_buckets_for_chart:
        story.append(_chart_doughnut(
            values=[b.pct_amount for b in dq_buckets_for_chart],
            labels=[b.bucket for b in dq_buckets_for_chart],
        ))
        story.append(Spacer(1, 4))

    # 2D matrix table
    matrix = result.delinquency_matrix
    if matrix:
        matrix_headers = ["DPD Bucket"] + matrix.columns
        rows = []
        rows.append(matrix_headers)
        cells_by_key = {(c.dpd_bucket, c.disposition): c for c in matrix.cells}
        for r in matrix.rows:
            row = [r]
            for disposition in matrix.columns[:-1]:  # all but Total
                cell = cells_by_key.get((r, disposition))
                row.append(_fmt_currency(cell.amount) if cell else "")
            row.append(_fmt_currency(matrix.row_totals.get(r, 0.0)))
            rows.append(row)
        # Column totals row
        tot_row = ["Total"]
        for c in matrix.columns[:-1]:
            tot_row.append(_fmt_currency(matrix.col_totals.get(c, 0.0)))
        tot_row.append(_fmt_currency(sum(matrix.col_totals.values())))
        rows.append(tot_row)
        t = Table(rows, repeatRows=1)
        ts = _table_style_base(len(matrix_headers))
        ts.add("FONTNAME", (0, len(rows) - 1), (-1, len(rows) - 1), "Helvetica-Bold")
        ts.add("BACKGROUND", (0, len(rows) - 1), (-1, len(rows) - 1), LIGHT_BLUE)
        t.setStyle(ts)
        story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 2(c) — RATES (chart + table)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2(c) COLLATERAL: RATES", h2_style))
    history = result.performance_history or []
    rates = result.collateral_rates
    if len(history) >= 2:
        x_labels = [h.date for h in history]
        cpr_series = [h.cpr_1m * 100 for h in history]
        cdr_series = [h.cdr_1m * 100 for h in history]
        story.append(_chart_line(
            x_labels=x_labels,
            series_named=[("CPR (1M) %", NAVY, cpr_series), ("CDR (1M) %", GOLD, cdr_series)],
        ))
        story.append(Spacer(1, 4))
    else:
        # Fallback when there isn't enough history for a meaningful line chart —
        # show the 1M / 3M / Inception snapshot as a grouped bar instead.
        story.append(_chart_grouped_bar(
            group_labels=["1M", "3M", "Inception"],
            series_a=[rates.cpr_1m * 100, rates.cpr_3m * 100, rates.cpr_inception * 100],
            series_b=[rates.cdr_1m * 100, rates.cdr_3m * 100, rates.cdr_inception * 100],
            series_a_name="CPR %", series_b_name="CDR %",
        ))
        story.append(Spacer(1, 4))

    rates_data = [
        ["Metric", "CD/PR", "CD/PR - 3 Months", "CD/PR - Inception", "SMM - 1 Month"],
        ["Defaults (CDR)",
         _fmt_pct(rates.cdr_1m), _fmt_pct(rates.cdr_3m),
         _fmt_pct(rates.cdr_inception), _fmt_pct(rates.smm_default)],
        ["Prepayments (CPR)",
         _fmt_pct(rates.cpr_1m), _fmt_pct(rates.cpr_3m),
         _fmt_pct(rates.cpr_inception), _fmt_pct(rates.smm_prepay)],
    ]
    t = Table(rates_data, repeatRows=1)
    t.setStyle(_table_style_base(5))
    story.append(t)
    story.append(Spacer(1, 4))
    story.append(Paragraph(
        "CDR: SMM = New Defaults / Beginning Balance; CDR = 1−(1−SMM)^12 | "
        "CPR: SMM = Unscheduled Principal / (Beginning Balance − Scheduled Principal); CPR = 1−(1−SMM)^12",
        small_style,
    ))
    story.append(Spacer(1, 6))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 2(d) — COLLATERAL REALIZED LOSS  (NEW)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2(d) COLLATERAL: REALIZED LOSS", h2_style))
    crl = result.collateral_realized_loss
    if crl:
        # Bar chart: realized loss + net liquidation proceeds (current vs cumulative)
        if (crl.realized_loss_current or crl.realized_loss_cumulative
                or crl.net_liquidation_proceeds_current or crl.net_liquidation_proceeds_cumulative):
            story.append(_chart_grouped_bar(
                group_labels=["Realized Loss", "Net Liq. Proceeds"],
                series_a=[crl.realized_loss_current, crl.net_liquidation_proceeds_current],
                series_b=[crl.realized_loss_cumulative, crl.net_liquidation_proceeds_cumulative],
                series_a_name="Current ($)", series_b_name="Cumulative ($)",
            ))
            story.append(Spacer(1, 4))

        crl_data = [
            ["Metric", "Current", "Cumulative"],
            ["Realized Loss",
             _fmt_currency(crl.realized_loss_current),
             _fmt_currency(crl.realized_loss_cumulative)],
            ["Number of Loans Liquidated",
             _fmt_int(crl.loans_liquidated_current),
             _fmt_int(crl.loans_liquidated_cumulative)],
            ["Net Liquidation Proceeds",
             _fmt_currency(crl.net_liquidation_proceeds_current),
             _fmt_currency(crl.net_liquidation_proceeds_cumulative)],
        ]
        t = Table(crl_data, colWidths=[240, 130, 130], repeatRows=1)
        t.setStyle(_table_style_base(3))
        story.append(t)
    story.append(Spacer(1, 8))

    story.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 2(e) — STRUCTURAL FEATURES  (NEW, with grouped bar chart)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("2(e) COLLATERAL: STRUCTURAL FEATURES", h2_style))
    sf = result.structural_features
    if sf:
        cs_classes = ["M-1", "M-2", "M-3"]
        orig_pct = [sf.original_credit_support.get(n, 0.0) * 100 for n in cs_classes]
        curr_pct = [sf.current_credit_support.get(n, 0.0) * 100 for n in cs_classes]
        if any(v > 0 for v in orig_pct + curr_pct):
            story.append(_chart_grouped_bar(
                group_labels=cs_classes,
                series_a=orig_pct, series_b=curr_pct,
                series_a_name="Original CE %", series_b_name="Current CE %",
            ))
            story.append(Spacer(1, 4))

        sf_rows = [
            ["Metric", "Value"],
            ["Gross WAC", _fmt_pct(sf.gross_wac)],
            ["Net WAC", _fmt_pct(sf.net_wac)],
            ["WAC Cap", _fmt_pct(sf.wac_cap)],
        ]
        for cn in cs_classes:
            sf_rows.append([f"Original Applicable Credit Support % — {cn}",
                            _fmt_pct(sf.original_credit_support.get(cn, 0.0))])
        for cn in cs_classes:
            sf_rows.append([f"Current Applicable Credit Support % — {cn}",
                            _fmt_pct(sf.current_credit_support.get(cn, 0.0))])
        sf_rows.append(["Non-Performing Loan %", _fmt_pct(sf.non_performing_loan_pct)])
        sf_rows.append(["Charged-Off Loan %", _fmt_pct(sf.charged_off_loan_pct)])
        for svc, bal in sorted(sf.beginning_upb_by_servicer.items()):
            sf_rows.append([f"Beginning UPB — {svc}", _fmt_currency(bal)])
        for svc, bal in sorted(sf.ending_upb_by_servicer.items()):
            sf_rows.append([f"Ending UPB — {svc}", _fmt_currency(bal)])
        sf_rows.append(["SOFR Fixing", _fmt_pct(sf.sofr_fixing)])
        sf_rows.append(["Severely Delinquent Mortgage Loan Balance (90+ DPD)",
                        _fmt_currency(sf.severely_delinquent_balance)])
        sf_rows.append(["Gross Expected Interest", _fmt_currency(sf.gross_expected_interest)])
        sf_rows.append(["Net Expected Interest", _fmt_currency(sf.net_expected_interest)])
        t = Table(sf_rows, colWidths=[340, 160], repeatRows=1)
        t.setStyle(_table_style_base(2))
        story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 3(a) — COLLECTIONS (3 nested sub-blocks)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("3(a) ACCOUNTS: COLLECTIONS", h2_style))

    pc_rows = [
        ["Principal Collections", ""],
        ["Plus: Scheduled Principal", _fmt_currency(result.principal_scheduled)],
        ["Plus: Curtailments", _fmt_currency(result.principal_curtailments)],
        ["Plus: Prepayments in Full", _fmt_currency(result.principal_prepayments_full)],
        ["Plus: Sales", _fmt_currency(result.principal_sales)],
        ["Plus: Liquidations", _fmt_currency(result.principal_liquidations)],
        ["Plus: Repurchases/Substitutions", _fmt_currency(result.principal_repurchases)],
        ["Plus: Recoveries", _fmt_currency(result.principal_recoveries)],
        ["Plus/(Less): Other", _fmt_currency(result.principal_other)],
        ["Total Net Principal", _fmt_currency(result.principal_remittance_amount)],
    ]
    ic_rows = [
        ["Interest Collections", ""],
        ["Plus: Gross Interest", _fmt_currency(result.gross_interest_collected)],
        ["Less: Servicing Fees", _fmt_currency(-result.servicing_fees_paid)],
        ["Less: Deal Fees", _fmt_currency(-result.deal_fees_paid)],
        ["Less: Deal Expenses", _fmt_currency(-result.deal_expenses_paid)],
        ["Plus/(Less): Other Amounts", _fmt_currency(result.other_collections)],
        ["Total Net Interest", _fmt_currency(result.interest_remittance_amount)],
    ]
    af_rows = [
        ["Total Available Funds", ""],
        ["Principal Remittance", _fmt_currency(result.principal_remittance_amount)],
        ["Interest Remittance", _fmt_currency(result.interest_remittance_amount)],
        ["Total", _fmt_currency(result.available_funds)],
    ]

    for block in (pc_rows, ic_rows, af_rows):
        t = Table(block, colWidths=[260, 140])
        ts = TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 7),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("BACKGROUND", (0, 0), (-1, 0), LIGHT_BLUE),
            ("FONTNAME", (0, -1), (-1, -1), "Helvetica-Bold"),
            ("BACKGROUND", (0, -1), (-1, -1), LIGHT_GRAY),
            ("ALIGN", (1, 0), (1, -1), "RIGHT"),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.grey),
        ])
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 4))
    story.append(Spacer(1, 4))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 3(b) — RESERVE ACCOUNTS  (NEW)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("3(b) ACCOUNTS: RESERVE ACCOUNTS", h2_style))
    if result.reserve_accounts:
        acc_headers = ["Account", "Beginning Balance", "Deposits", "Withdrawals",
                       "Ending Balance", "Required Balance"]
        acc_data = [acc_headers]
        for a in result.reserve_accounts:
            acc_data.append([
                a.account_name,
                _fmt_currency(a.beginning_balance),
                _fmt_currency(a.deposits),
                _fmt_currency(a.withdrawals),
                _fmt_currency(a.ending_balance_post_payment),
                _fmt_currency(a.required_balance) if a.required_balance is not None else "",
            ])
        t = Table(acc_data, repeatRows=1)
        t.setStyle(_table_style_base(len(acc_headers)))
        story.append(t)
    story.append(Spacer(1, 8))

    story.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 4 — FEES
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("4. FEES", h2_style))
    fee_headers = ["Name", "Beg. Shortfall", "Current Due", "Total Due",
                   "Amount Paid", "End. Shortfall"]
    fee_data = [fee_headers]
    for f in result.fees_detail:
        fee_data.append([
            f.fee_name,
            _fmt_currency(f.beginning_shortfall),
            _fmt_currency(f.current_due),
            _fmt_currency(f.total_due),
            _fmt_currency(f.amount_paid),
            _fmt_currency(f.ending_shortfall),
        ])
    fee_data.append([
        "Total:", "", "",
        _fmt_currency(sum(f.total_due for f in result.fees_detail)),
        _fmt_currency(result.total_fees),
        _fmt_currency(sum(f.ending_shortfall for f in result.fees_detail)),
    ])
    t = Table(fee_data, repeatRows=1)
    ts = _table_style_base(len(fee_headers))
    ts.add("FONTNAME", (0, len(fee_data) - 1), (-1, len(fee_data) - 1), "Helvetica-Bold")
    ts.add("BACKGROUND", (0, len(fee_data) - 1), (-1, len(fee_data) - 1), LIGHT_BLUE)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 5 — EXPENSES  (NEW, distinct from Fees)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("5. EXPENSES", h2_style))
    exp_headers = ["Name", "Beg. Shortfall", "Current Due", "Total Due",
                   "Amount Paid", "End. Shortfall"]
    exp_data = [exp_headers]
    for e in result.expenses_detail:
        exp_data.append([
            e.expense_name,
            _fmt_currency(e.beginning_shortfall),
            _fmt_currency(e.current_due),
            _fmt_currency(e.total_due),
            _fmt_currency(e.amount_paid),
            _fmt_currency(e.ending_shortfall),
        ])
    t = Table(exp_data, repeatRows=1)
    t.setStyle(_table_style_base(len(exp_headers)))
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 6 — EVENTS (TRIGGERS)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("6. EVENTS", h2_style))
    events_headers = ["Name", "Current Value", "Operator", "Limit", "Status"]
    events_data = [events_headers]
    for e in result.events:
        cv_disp = _fmt_currency(e.current_value) if abs(e.current_value) > 100 else _fmt_pct(e.current_value)
        th_disp = _fmt_currency(e.threshold) if abs(e.threshold) > 100 else _fmt_pct(e.threshold)
        events_data.append([
            e.test_name, cv_disp,
            e.operator.replace("_", " ").title(),
            th_disp, e.status,
        ])
    t = Table(events_data, repeatRows=1)
    ts = _table_style_base(5)
    for i, e in enumerate(result.events, 1):
        color = GREEN_FILL if e.status in ("Pass", "Eligible") else RED_FILL
        ts.add("BACKGROUND", (4, i), (4, i), color)
    t.setStyle(ts)
    story.append(t)
    story.append(Spacer(1, 8))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 7 — SERVICER BALANCES
    # ─────────────────────────────────────────────────────────────────────
    if result.servicer_balances:
        story.append(Paragraph("7. SERVICER BALANCES", h2_style))
        # Grouped bar chart: beginning UPB vs ending UPB per servicer
        story.append(_chart_grouped_bar(
            group_labels=[s.servicer_name for s in result.servicer_balances],
            series_a=[s.beginning_upb / 1_000_000 for s in result.servicer_balances],
            series_b=[s.ending_upb / 1_000_000 for s in result.servicer_balances],
            series_a_name="Beginning UPB ($MM)", series_b_name="Ending UPB ($MM)",
        ))
        story.append(Spacer(1, 4))

        svc_headers = ["Servicer", "Beginning UPB", "Ending UPB", "Servicing Fee", "Loan Count"]
        svc_data = [svc_headers]
        for s in result.servicer_balances:
            svc_data.append([
                s.servicer_name,
                _fmt_currency(s.beginning_upb),
                _fmt_currency(s.ending_upb),
                _fmt_currency(s.servicing_fee),
                str(s.loan_count),
            ])
        t = Table(svc_data, repeatRows=1)
        t.setStyle(_table_style_base(5))
        story.append(t)
        story.append(Spacer(1, 8))

    story.append(PageBreak())

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 8 — PRIORITY OF PAYMENTS (with allocation chart + both branches)
    # ─────────────────────────────────────────────────────────────────────
    story.append(Paragraph("8. PRIORITY OF PAYMENTS", h2_style))

    alloc = result.distribution_allocation or []
    if alloc:
        story.append(_chart_horizontal_bar(
            values=[a.amount / 1_000_000 for a in alloc],
            labels=[a.bucket for a in alloc],
        ))
        story.append(Spacer(1, 4))

    def _wf_table(steps, header_note=""):
        wf_headers = ["Priority", "Description", "Class", "Funds Avail.",
                      "Amount Owed", "Amount Paid"]
        wf_data = [wf_headers]
        for s in steps:
            wf_data.append([
                f"({s.step})",
                (s.description or "")[:60],
                s.class_name or "",
                _fmt_currency(s.funds_available),
                _fmt_currency(s.amount_owed),
                _fmt_currency(s.amount_paid),
            ])
        t = Table(wf_data, repeatRows=1, colWidths=[40, 220, 60, 80, 80, 80])
        t.setStyle(_table_style_base(6))
        return t

    if result.waterfall_trace_interest:
        story.append(Paragraph("Interest Priorities of Payments", h3_style))
        story.append(_wf_table(result.waterfall_trace_interest))
        story.append(Spacer(1, 6))

    active = result.active_trigger_branch or "Trigger Not In Effect"
    if result.waterfall_trace_principal_no_trigger:
        marker = " (ACTIVE)" if active == "Trigger Not In Effect" else ""
        story.append(Paragraph(f"Principal — Trigger Not In Effect{marker}", h3_style))
        story.append(_wf_table(result.waterfall_trace_principal_no_trigger))
        story.append(Spacer(1, 6))
    if result.waterfall_trace_principal_with_trigger:
        marker = " (ACTIVE)" if active == "Trigger In Effect" else ""
        story.append(Paragraph(f"Principal — Trigger In Effect{marker}", h3_style))
        story.append(_wf_table(result.waterfall_trace_principal_with_trigger))
        story.append(Spacer(1, 6))

    if result.waterfall_trace_excess:
        story.append(Paragraph("Monthly Excess Cashflow Priorities", h3_style))
        story.append(_wf_table(result.waterfall_trace_excess))

    # ─────────────────────────────────────────────────────────────────────
    # SECTION 9 — LOAN DETAILS
    # ─────────────────────────────────────────────────────────────────────
    story.append(PageBreak())
    story.append(Paragraph("9. LOAN DETAILS", h2_style))

    def _loan_table(title: str, loans, principal_only: bool = True):
        count = len(loans)
        story.append(Paragraph(f"{title} ({count})", h3_style))
        if not loans:
            story.append(Paragraph("No loans to report", body_style))
            return
        if principal_only:
            headers = ["Loan ID", "Beginning Principal", "Ending Principal"]
        else:
            headers = ["Loan ID", "Deferred Amount", "Cumulative Deferred"]
        data = [headers]
        total_a = 0.0
        total_b = 0.0
        for loan in loans[:100]:
            if principal_only:
                a = loan.beginning_principal or 0.0
                b = loan.ending_principal or 0.0
            else:
                a = loan.deferred_amount or 0.0
                b = loan.cumulative_deferred or 0.0
            total_a += a
            total_b += b
            data.append([loan.loan_id, _fmt_currency(a), _fmt_currency(b)])
        if count > 100:
            data.append([f"... {count - 100} more loans ...", "", ""])
        data.append(["Total:", _fmt_currency(total_a), _fmt_currency(total_b)])
        t = Table(data, repeatRows=1, colWidths=[140, 130, 130])
        ts = _table_style_base(3)
        ts.add("FONTNAME", (0, len(data) - 1), (-1, len(data) - 1), "Helvetica-Bold")
        ts.add("BACKGROUND", (0, len(data) - 1), (-1, len(data) - 1), LIGHT_BLUE)
        t.setStyle(ts)
        story.append(t)
        story.append(Spacer(1, 6))

    _loan_table("9(a) Paid In Full", result.loans_paid_in_full)
    _loan_table("9(b) REO", result.loans_reo)
    _loan_table("9(c) Foreclosure", result.loans_foreclosure)
    _loan_table("9(d) Bankruptcy", result.loans_bankruptcy)
    _loan_table("9(e) Modifications", result.loans_modified)
    _loan_table("9(f) Forbearance", result.loans_forbearance, principal_only=False)

    # ─────────────────────────────────────────────────────────────────────
    # Footer
    # ─────────────────────────────────────────────────────────────────────
    story.append(Spacer(1, 10))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.grey))
    story.append(Paragraph(
        f"Report generated: {result.computed_at} | Net WAC: {_fmt_pct(result.net_wac)} | "
        f"Benchmark ({result.benchmark}): {_fmt_pct(result.benchmark_rate)} | "
        f"Gross WAC: {_fmt_pct(result.gross_wac)}",
        small_style,
    ))

    doc.build(story)
    pdf_bytes = buffer.getvalue()
    buffer.close()

    if output_path:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(pdf_bytes)

    return pdf_bytes
