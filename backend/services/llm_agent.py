"""
LLM Agent for extracting structured deal configuration fields from ABS/MBS deal indentures.

Extraction Strategy (multi-pass, high accuracy):
  1. Extract text AND tables from all PDF pages using pdfplumber.
  2. Regex pre-scan across ALL pages to find exact values for dates, amounts, rates.
  3. Smart section scoring to find the BEST pages for each section type.
  4. Multi-chunk LLM extraction — send the highest-scoring page windows to the LLM.
  5. Merge and consolidate results, filling gaps with regex-found values.
  6. Validation pass — re-query LLM for any critical fields still missing.
  7. Assemble final DealConfig.
"""
from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF — ~10x faster than pdfplumber for plain-text extraction
import pdfplumber
from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage

from models.deal import (
    CertificateClass,
    DealConfig,
    FeeConfig,
    ServicerConfig,
    TriggerTest,
    WaterfallStep,
    ReserveAccount,
)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

logger = logging.getLogger("uvicorn.error")

# ---------------------------------------------------------------------------
# LLM setup
# ---------------------------------------------------------------------------

def _get_llm(temperature: float = 0.0) -> AzureChatOpenAI:
    return AzureChatOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT", "").strip(),
        api_key=os.getenv("AZURE_OPENAI_API_KEY", "").strip(),
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-15-preview").strip(),
        azure_deployment=os.getenv("AZURE_OPENAI_CHAT_DEPLOYMENT_NAME", "gpt-4.1").strip(),
        temperature=temperature,
        max_tokens=8192,
    )


# ---------------------------------------------------------------------------
# PDF extraction — text + tables
# ---------------------------------------------------------------------------

def extract_pdf_pages(pdf_path: str) -> List[Tuple[int, str]]:
    """
    Extract text from all PDF pages, with table rows appended for the first
    50 pages (where the certificate table lives).

    Two-stage extraction:
      1. PyMuPDF (fitz) for fast text extraction across ALL pages.
      2. pdfplumber for table extraction on first 50 pages only — its table
         detector is more reliable than fitz's for the certificate table.
    """
    page_texts: List[str] = []
    with fitz.open(pdf_path) as doc:
        for page in doc:
            page_texts.append(page.get_text("text") or "")

    # Append pdfplumber table rows for the first 50 pages
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for i in range(min(50, len(pdf.pages))):
                try:
                    tables = pdf.pages[i].extract_tables()
                    if not tables:
                        continue
                    extra = []
                    for table in tables:
                        for row in table:
                            if row and any(cell for cell in row if cell):
                                row_text = " | ".join(str(c or "").strip() for c in row)
                                if row_text.strip(" |"):
                                    extra.append(row_text)
                    if extra:
                        page_texts[i] = page_texts[i] + "\n" + "\n".join(extra)
                except Exception:
                    pass
    except Exception as e:
        logger.warning(f"pdfplumber table-extraction pass failed: {e}")

    pages = [(i + 1, t.strip()) for i, t in enumerate(page_texts)]
    logger.info(f"Extracted text from {len(pages)} pages of {pdf_path}")
    return pages


# ---------------------------------------------------------------------------
# Regex pre-scan — extract critical values directly from text
# ---------------------------------------------------------------------------

def regex_prescan(pages: List[Tuple[int, str]]) -> Dict[str, Any]:
    """
    Scan ALL pages with regex to extract critical fields as a fallback.
    Returns a dict of field values extracted directly from text.
    """
    full_text = "\n".join(text for _, text in pages)
    found: Dict[str, Any] = {}

    months = {
        "january": "01", "february": "02", "march": "03", "april": "04",
        "may": "05", "june": "06", "july": "07", "august": "08",
        "september": "09", "october": "10", "november": "11", "december": "12",
    }

    def parse_date(s: str) -> Optional[str]:
        s = s.strip()
        m = re.match(r"(\w+)\s+(\d{1,2}),?\s+(\d{4})", s)
        if m:
            mon_name = m.group(1).lower()
            day = m.group(2).zfill(2)
            year = m.group(3)
            mon = months.get(mon_name)
            if mon:
                return f"{year}-{mon}-{day}"
        return None

    # ---- Dates ----
    # Handles: "Cut-off Date: August 1, 2023"
    # Also handles the 2-column PDF layout format: "CUT-OFF DATE\nThe close of business on May 31, 2023"
    date_field_patterns: Dict[str, List[str]] = {
        "cut_off_date": [
            # 2-column PDF layout: "CUT-OFF DATE\n...\nThe close of business on May 31, 2023"
            r"CUT.OFF\s+DATE(?:\s+[\w\s,]+?)?\s+[Tt]he\s+close\s+of\s+business\s+on\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            # Direct: "cut-off date: August 1, 2023"
            r"[Cc]ut.?off\s+[Dd]ate[:\s]+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            # Phrase: "cut-off date is [the close of business on] Month DD, YYYY"
            r"[Cc]ut.?off\s+[Dd]ate\s+is\s+(?:the\s+close\s+of\s+business\s+on\s+)?((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            # Standalone: "close of business on May 31, 2023" (on a page that mentions cut-off)
            r"[Cc]lose\s+of\s+business\s+on\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
        ],
        "closing_date": [
            r"[Cc]losing\s+[Dd]ate[:\s]+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            r"CLOSING\s+DATE\s+(?:will\s+be\s+|is\s+)?((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
        ],
        "first_payment_date": [
            r"[Ff]irst\s+(?:[Dd]istribution|[Pp]ayment)\s+[Dd]ate[:\s]+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            # "commencing in July 2023" for payment dates
            r"commencing\s+in\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
        ],
        "pricing_date": [
            r"[Pp]ricing\s+[Dd]ate[:\s]+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
        ],
        "legal_maturity_date": [
            r"(?:[Ll]egal\s+[Ff]inal|[Ff]inal\s+(?:[Mm]aturity|[Ss]cheduled))\s+[Dd]ate[:\s]+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
            # "distribution date in November 2053"
            r"[Ff]inal\s+scheduled\s+distribution\s+date.*?distribution\s+date\s+in\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4})",
        ],
    }

    for field, patterns in date_field_patterns.items():
        for pattern in patterns:
            m = re.search(pattern, full_text, re.IGNORECASE | re.DOTALL)
            if m:
                raw_date = m.group(1)
                # Handle "July 2023" (month year only)
                m2 = re.match(r"(\w+)\s+(\d{4})$", raw_date.strip())
                if m2:
                    mon = months.get(m2.group(1).lower())
                    if mon:
                        found[field] = f"{m2.group(2)}-{mon}-20"
                        break
                parsed = parse_date(raw_date)
                if parsed:
                    found[field] = parsed
                    break

    # ---- Pool balance ----
    pool_patterns = [
        r"aggregate\s+(?:initial\s+)?certificate\s+principal\s+amount\s+of\s+\$([\d,]+(?:\.\d+)?)",
        r"aggregate\s+(?:unpaid\s+)?principal\s+balance\s+of\s+the\s+(?:mortgage\s+)?(?:loans|pool)[^$\n]{0,60}\$([\d,]+(?:\.\d+)?)",
        r"initial\s+(?:pool|aggregate)\s+(?:principal\s+)?balance[^$\n]{0,40}\$([\d,]+(?:\.\d+)?)",
        r"\$([\d,]+(?:\.\d+)?)\s+as\s+of\s+the\s+clos(?:ing|e)",
    ]
    for pattern in pool_patterns:
        m = re.search(pattern, full_text, re.IGNORECASE)
        if m:
            val = float(m.group(1).replace(",", ""))
            if val > 1_000_000:
                found["original_pool_balance"] = val
                break

    # ---- Cleanup call ----
    cleanup_m = re.search(
        r"(?:[Oo]ptional\s+)?[Cc]leanup\s+[Cc]all[^%\n]{0,120}(10|5|1)\s*%",
        full_text,
    )
    if cleanup_m:
        found["cleanup_call_pct"] = float(cleanup_m.group(1)) / 100

    # ---- Benchmark ----
    if re.search(r"\bSOFR\b", full_text):
        found["benchmark"] = "SOFR"
    if re.search(
        r"1.month\s+(?:[Tt]erm\s+)?SOFR|1M\s+SOFR|[Oo]ne.month\s+(?:[Tt]erm\s+)?SOFR",
        full_text,
    ):
        found["benchmark_tenor"] = "1M"
    elif re.search(
        r"3.month\s+(?:[Tt]erm\s+)?SOFR|3M\s+SOFR|[Tt]hree.month\s+(?:[Tt]erm\s+)?SOFR",
        full_text,
    ):
        found["benchmark_tenor"] = "3M"

    # ---- Day count ----
    if re.search(r"[Aa]ctual/360", full_text):
        found["interest_day_count"] = "actual/360"
    elif re.search(r"30/360", full_text):
        found["interest_day_count"] = "30/360"

    # ---- Lien position ----
    second_lien = len(re.findall(r"[Ss]econd\s+[Ll]ien|2nd\s+[Ll]ien", full_text))
    first_lien = len(re.findall(r"[Ff]irst\s+[Ll]ien|1st\s+[Ll]ien", full_text))
    if second_lien > first_lien:
        found["lien_position"] = "Second Lien"
    elif first_lien > 0:
        found["lien_position"] = "First Lien"

    # ---- Asset type ----
    if re.search(r"\bHELOC\b|[Hh]ome\s+[Ee]quity\s+[Ll]ine\s+of\s+[Cc]redit", full_text):
        found["asset_type"] = "HELOC"
    elif re.search(r"[Hh]ome\s+[Ee]quity\s+[Ll]oan", full_text):
        found["asset_type"] = "Home Equity Loan"

    # ---- Class-level regex: extract certificate table rows directly ----
    #
    # STRATEGY: first try to isolate the certificate table section, then extract
    # class rows only from within it. This avoids picking up the same class name
    # from a different table (e.g., credit-support calculation) that may have a
    # different dollar amount and cause LLM-overriding errors.
    #
    # We fall back to full-document search if the section is not found.

    # Step 1 — isolate the certificate table section from the full text
    cert_section_text = full_text  # fallback: whole document
    cert_section_match = re.search(
        r"(?:THE\s+OFFERED\s+CERTIFICATES?|SUMMARY\s+OF\s+(?:THE\s+)?CERTIFICATES?"
        r"|INITIAL\s+CLASS\s+PRINCIPAL\s+AMOUNT|APPROXIMATE\s+INITIAL\s+PASS.THROUGH\s+RATE)"
        r"(.{200,60000}?)"
        r"(?=THE\s+SERVICERS?|DESCRIPTION\s+OF\s+THE\s+(?:NOTES|CERTIFICATES)"
        r"|RISK\s+FACTORS|THE\s+MORTGAGE\s+POOL|THE\s+TRUST\s+FUND"
        r"|SERVICING\s+OF\s+THE\s+MORTGAGE|CREDIT\s+ENHANCEMENT|PRIORITY\s+OF\s+PAYMENTS)",
        full_text,
        re.DOTALL | re.IGNORECASE,
    )
    if cert_section_match:
        cert_section_text = cert_section_match.group(0)
        logger.info(
            f"Regex anchored to certificate table section "
            f"({len(cert_section_text)} chars)"
        )

    # Step 2 — extract class rows from text.
    #
    # IMPORTANT: Do NOT rely solely on the cert_section isolation above —
    # "THE OFFERED CERTIFICATES" can appear as a regulatory disclaimer early in the
    # document and the lazy regex will latch onto that short false match.
    # Instead, search the FULL document text (which guarantees we never miss a page)
    # and use frequency-voting in Step 4 to pick the most reliable value per class.
    #
    # Pattern allows optional footnote markers between class name and dollar amount,
    # e.g. "Class B-1(5) $6,057,000" where (5) is a footnote number.
    class_row_pattern = re.compile(
        r"Class\s+([\w\-]+)(?:\(\d+\))?\s+\$([\d,]+(?:\.\d+)?)(?:\(\d+\))?",
        re.IGNORECASE,
    )
    regex_classes_all: Dict[str, List[float]] = {}

    # Search cert_section first (more reliable if found correctly), then full doc
    for search_text in [cert_section_text, full_text]:
        for m in class_row_pattern.finditer(search_text):
            cname = m.group(1).strip()
            amount = float(m.group(2).replace(",", ""))
            if amount > 1000:  # filter noise
                regex_classes_all.setdefault(cname, []).append(amount)
        if len(regex_classes_all) >= 4:
            # Found enough classes — no need to search wider
            break

    # Step 3 — also scan pipe-delimited rows (pdfplumber table output format)
    #  "Class A-1 | $126,186,000 | 6.81655% | ..."  or
    #  "A-1 | 46656UAA1 | $126,186,000 | ..."
    pipe_row_pattern = re.compile(
        r"(?:^|\n)\s*(?:[Cc]lass\s+)?([\w\-]+)(?:\(\d+\))?\s*\|\s*\$([\d,]+(?:\.\d+)?)(?:\(\d+\))?",
        re.IGNORECASE | re.MULTILINE,
    )
    for m in pipe_row_pattern.finditer(full_text):
        cname = m.group(1).strip()
        # Only accept names that look like certificate classes (not generic words)
        if not re.match(r"^[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*$", cname):
            continue
        if cname.upper() in {"CLASS", "TYPE", "NOTE", "TRANCHE", "CERTIFICATE"}:
            continue
        amount = float(m.group(2).replace(",", ""))
        if amount > 1000:
            regex_classes_all.setdefault(cname, []).append(amount)

    # Step 4 — for each class, use the value that appears MOST OFTEN in the
    # certificate section. If still tied, take the maximum. This defeats the
    # "one spurious lower value in another table" problem.
    regex_classes: Dict[str, float] = {}
    for cname, amounts in regex_classes_all.items():
        if not amounts:
            continue
        # Most-frequent first, break ties by taking largest
        freq = Counter(amounts)
        best_val = max(freq.keys(), key=lambda v: (freq[v], v))
        regex_classes[cname] = best_val

    if regex_classes:
        found["regex_class_principals"] = regex_classes
        logger.info(f"Regex found class principals (cert-section anchored): {regex_classes}")

    # ---- Class margins regex ----
    # "applicable margin for the Class A-1, ... Certificates will be 1.75000 %, 2.50000 %, ..."
    margin_sentence_m = re.search(
        r"applicable\s+margin\s+for\s+the\s+Class\s+([\w\-,\s\n]+?)\s+[Cc]ertificates\s+will\s+be\s+([\d.,\s%\n]+)",
        full_text,
        re.IGNORECASE | re.DOTALL,
    )
    if margin_sentence_m:
        class_list_raw = re.sub(r"\s+", " ", margin_sentence_m.group(1))
        margins_raw = margin_sentence_m.group(2)
        class_names_clean = [
            re.sub(r"^[Cc]lass\s+", "", c).strip()
            for c in re.split(r",\s*(?:and\s+)?|(?:\s+and\s+)", class_list_raw)
            if c.strip()
        ]
        margin_vals = re.findall(r"([\d.]+)\s*%", margins_raw)
        regex_margins: Dict[str, float] = {}
        for i, cname in enumerate(class_names_clean):
            cname_clean = re.sub(r"\s+", "", cname).strip()  # remove internal whitespace
            if i < len(margin_vals):
                regex_margins[cname_clean] = float(margin_vals[i]) / 100
        if regex_margins:
            found["regex_class_margins"] = regex_margins
            logger.info(f"Regex found class margins: {regex_margins}")

    # ---- Page-level date extraction for 2-column PDFs ----
    # Search each page that mentions "CUT-OFF" for a date on the same page
    if "cut_off_date" not in found:
        for page_num, page_text in pages:
            if re.search(r"CUT.OFF\s+DATE", page_text, re.IGNORECASE):
                # Search this page for any close-of-business date or month/day/year
                date_m = re.search(
                    r"[Cc]lose\s+of\s+business\s+on\s+((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s*\d{4})",
                    page_text,
                    re.IGNORECASE,
                )
                if not date_m:
                    date_m = re.search(
                        r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4})",
                        page_text,
                        re.IGNORECASE,
                    )
                if date_m:
                    parsed = parse_date(date_m.group(1))
                    if parsed:
                        found["cut_off_date"] = parsed
                        logger.info(f"Page-level cut_off_date found on page {page_num}: {parsed}")
                        break

    # ── Step 5: Deterministic margin extraction ────────────────────────────
    # Parse "The applicable margin for Class A-1, M-1 ... will be 1.75%, 2.50%, ..."
    margin_map = _extract_class_margins_from_text(full_text)
    if margin_map:
        # Strip the _fixed suffix entries (those are IO fixed rates stored separately)
        float_margins = {k: v for k, v in margin_map.items() if not k.endswith("_fixed")}
        io_fixed      = {k[:-6]: v for k, v in margin_map.items() if k.endswith("_fixed")}
        found["regex_class_margins"] = float_margins
        if io_fixed:
            found["regex_io_fixed_rates"] = io_fixed
        logger.info(f"Extracted SOFR margins for: {list(float_margins.keys())}")

    # ── Step 6: Special rate type detection ───────────────────────────────
    special_types = _extract_special_rate_types_from_text(full_text)
    if special_types:
        found["special_rate_types"] = special_types
        logger.info(f"Detected special rate types: {list(special_types.keys())}")

    logger.info(f"Regex pre-scan found fields: {[k for k in found if not k.startswith('regex_')]}")
    return found


def _normalize_class_name(name: str) -> str:
    """Normalize class name for lookup: remove spaces and uppercase."""
    return re.sub(r"\s+", "", name).upper()


# ---------------------------------------------------------------------------
# Deterministic certificate table extractor (pdfplumber-based)
# ---------------------------------------------------------------------------

_CERT_TABLE_HEADERS = re.compile(
    r"(?:THE\s+OFFERED\s+CERTIFICATES|SUMMARY\s+OF\s+(?:THE\s+)?CERTIFICATES"
    r"|INITIAL\s+CLASS\s+PRINCIPAL|APPROXIMATE\s+INITIAL\s+PASS.THROUGH"
    r"|CLASS\s+PRINCIPAL\s+AMOUNT|OFFERED\s+NOTES|THE\s+CERTIFICATES)",
    re.IGNORECASE,
)

_CERT_TABLE_END = re.compile(
    r"(?:THE\s+SERVICERS?|DESCRIPTION\s+OF\s+THE\s+(?:NOTES|CERTIFICATES)"
    r"|RISK\s+FACTORS|TABLE\s+OF\s+CONTENTS|THE\s+MORTGAGE\s+POOL"
    r"|THE\s+TRUST\s+FUND|SERVICING\s+OF\s+THE\s+MORTGAGE)",
    re.IGNORECASE,
)


def _find_cert_table_pages(pages: List[Tuple[int, str]]) -> List[int]:
    """Return page numbers (1-indexed) most likely to contain the certificate table."""
    candidates = []
    for page_num, text in pages[:60]:  # table is always in first 60 pages
        score = 0
        tu = text.upper()
        for kw in [
            "INITIAL CLASS PRINCIPAL", "APPROXIMATE INITIAL PASS",
            "OFFERED CERTIFICATES", "CLASS PRINCIPAL AMOUNT",
            "PASS-THROUGH RATE", "PASS THROUGH RATE",
            "CLASS A-1", "CLASS M-1", "RULE 144A CUSIP",
        ]:
            if kw in tu:
                score += 1
        if score >= 2:
            candidates.append((page_num, score))
    candidates.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in candidates[:8]]


def _parse_amount(raw: str) -> Optional[float]:
    """Extract a dollar amount from a string like '$126,186,000' or '126,186,000(12)'."""
    if not raw:
        return None
    raw = re.sub(r"\(\d+\)", "", raw)   # strip footnote markers
    raw = raw.replace(",", "").replace("$", "").strip()
    try:
        val = float(raw)
        return val if val > 1000 else None
    except ValueError:
        return None


def _parse_pdfplumber_table(table: List[List]) -> List[Dict]:
    """
    Parse one pdfplumber table into a list of class dicts.
    Dynamically detects where header rows end and data rows begin.
    """
    if not table or len(table) < 3:
        return []

    # ── Dynamically find where data rows begin ───────────────────────────
    # A row is a "data row" if its first non-empty cell looks like a class name
    # (e.g., "Class A-1", "A-1", "M-1", "BX") — NOT generic header words.
    _HEADER_WORDS = {"CLASS", "TRANCHE", "TYPE", "NAME", "CERTIFICATE", "NOTE", "NOTES"}
    _CLASS_RE = re.compile(r"^(?:[Cc]lass\s+)?[A-Z][A-Z0-9]*[-.]?\d*[A-Z]?$")

    first_data_row = len(table)
    for i, row in enumerate(table):
        if not row:
            continue
        first_cell = str(row[0] or "").strip()
        clean = re.sub(r"^[Cc]lass\s+", "", first_cell).strip()
        if clean and _CLASS_RE.match(clean) and clean.upper() not in _HEADER_WORDS:
            first_data_row = i
            break

    # Fallback: assume 2-row header if no class name found
    if first_data_row == len(table):
        first_data_row = min(2, len(table) - 1)

    MAX_HDR = max(first_data_row, 1)   # rows 0..MAX_HDR-1 are headers

    n_cols = max((len(r) for r in table if r), default=0)

    def col_header(col_idx: int) -> str:
        parts = []
        for r in range(MAX_HDR):
            if col_idx < len(table[r]):
                parts.append(str(table[r][col_idx] or "").strip())
        return " ".join(parts).upper()

    col_headers = [col_header(i) for i in range(n_cols)]

    # ── Map columns ───────────────────────────────────────────────────────
    class_col = principal_col = cusip_col = rate_col = type_col = -1
    fitch_col = kbra_col = moodys_col = -1
    rule144a_col = reg_s_col = -1

    for i, h in enumerate(col_headers):
        if class_col < 0 and "CLASS" in h:
            class_col = i
        elif principal_col < 0 and any(k in h for k in ("INITIAL", "PRINCIPAL", "AMOUNT", "NOTIONAL")):
            principal_col = i
        elif cusip_col < 0 and "CUSIP" in h and "144A" not in h and "REG" not in h:
            cusip_col = i
        elif rule144a_col < 0 and "144A" in h and "CUSIP" in h:
            rule144a_col = i
        elif reg_s_col < 0 and ("REG" in h or "REGULATION") and "CUSIP" in h:
            reg_s_col = i
        elif rate_col < 0 and any(k in h for k in ("RATE", "INTEREST", "PASS", "FORMULA")):
            rate_col = i
        elif type_col < 0 and "TYPE" in h:
            type_col = i
        elif fitch_col < 0 and "FITCH" in h:
            fitch_col = i
        elif kbra_col < 0 and "KBRA" in h:
            kbra_col = i
        elif moodys_col < 0 and "MOOD" in h:
            moodys_col = i

    if class_col < 0 and principal_col < 0:
        return []

    # Use rule144a as CUSIP if we have no plain CUSIP column
    if cusip_col < 0 and rule144a_col >= 0:
        cusip_col = rule144a_col

    # ── Parse data rows (skip header rows) ───────────────────────────────
    classes = []
    for row in table[first_data_row:]:
        if not row or all(c is None or str(c).strip() == "" for c in row):
            continue

        def cell(idx: int) -> str:
            if idx < 0 or idx >= len(row):
                return ""
            return str(row[idx] or "").strip()

        # Class name
        raw_name = cell(class_col)
        class_name = re.sub(r"^[Cc]lass\s+", "", raw_name).strip()
        # Must look like a real class name: letters/digits/dash — not a header word
        if not class_name or not re.match(r"^[A-Za-z][\w\-\.]*$", class_name):
            continue
        if class_name.upper() in _HEADER_WORDS:
            continue

        cls: Dict[str, Any] = {"class_name": class_name}

        # Principal / notional amount
        principal_raw = cell(principal_col)
        if re.search(r"N/A|N\.A\.|notional", principal_raw, re.IGNORECASE):
            cls["is_notional"] = True
            cls["initial_principal"] = 0.0
        else:
            amt = _parse_amount(principal_raw)
            if amt:
                cls["initial_principal"] = amt

        # CUSIP (prefer 144A)
        cusip_raw = cell(cusip_col)
        if re.match(r"[A-Z0-9]{8,9}", cusip_raw):
            cls["cusip"] = cusip_raw

        # Type classification
        type_raw = cell(type_col).lower()
        if "senior" in type_raw:
            cls["type"] = "Senior"
        elif "mezzanine" in type_raw:
            cls["type"] = "Mezzanine"
        elif "subordinate" in type_raw:
            cls["type"] = "Subordinate"
        elif "residual" in type_raw or "remic" in type_raw:
            cls["type"] = "Residual"
            cls["is_residual"] = True
        elif "exchange" in type_raw:
            cls["type"] = "Exchangeable"
            cls["is_exchangeable"] = True
        elif "notional" in type_raw or "io" in type_raw or "servicing" in type_raw:
            cls["is_notional"] = True

        # Floating vs fixed rate
        rate_raw = cell(rate_col)
        if re.search(r"N/A|N\.A\.", rate_raw, re.IGNORECASE):
            cls["interest_rate_type"] = "excess_cashflow"
        elif re.search(r"[\d.]+\s*%", rate_raw):
            pct_m = re.search(r"([\d.]+)\s*%", rate_raw)
            if pct_m:
                cls["fixed_rate"] = float(pct_m.group(1)) / 100
                cls["interest_rate_type"] = "floating"  # deal-specific — will be corrected by LLM

        # Ratings
        fitch_raw = cell(fitch_col)
        if fitch_raw and fitch_raw not in ("N/A", "NR", "Not Rated", "—"):
            cls["fitch_rating"] = fitch_raw
        kbra_raw = cell(kbra_col)
        if kbra_raw and kbra_raw not in ("N/A", "NR", "Not Rated", "—"):
            cls["kbra_rating"] = kbra_raw

        # Only keep if we have at least a class name + one useful field
        if cls.get("initial_principal") is not None or cls.get("is_residual") or cls.get("is_notional"):
            classes.append(cls)

    return classes


def _extract_certificate_table_direct(
    pdf_path: str,
    pages: List[Tuple[int, str]],
) -> List[Dict]:
    """
    Deterministic pdfplumber extraction of the certificate/tranche table.
    Tries multiple table-detection strategies on candidate pages.
    Returns list of class dicts, or [] if nothing reliable found.
    """
    candidate_page_nums = _find_cert_table_pages(pages)
    if not candidate_page_nums:
        logger.info("Certificate table direct extraction: no candidate pages found")
        return []

    # Try multiple pdfplumber table strategies in order of precision
    table_strategies = [
        {"vertical_strategy": "lines",        "horizontal_strategy": "lines"},
        {"vertical_strategy": "lines_strict",  "horizontal_strategy": "lines_strict"},
        {"vertical_strategy": "text",          "horizontal_strategy": "text"},
        {"vertical_strategy": "lines",         "horizontal_strategy": "text"},
        {},   # pdfplumber default
    ]

    best_classes: List[Dict] = []

    with pdfplumber.open(pdf_path) as pdf:
        total_pdf_pages = len(pdf.pages)
        for page_num in candidate_page_nums:
            if page_num > total_pdf_pages:
                continue
            page = pdf.pages[page_num - 1]

            # Also try the next 2 pages (table may span pages)
            page_group = []
            for pg_offset in range(3):
                pg_idx = page_num - 1 + pg_offset
                if pg_idx < total_pdf_pages:
                    page_group.append(pdf.pages[pg_idx])

            for strategy in table_strategies:
                for pg in page_group:
                    try:
                        tables = pg.extract_tables(strategy) if strategy else pg.extract_tables()
                        for table in tables:
                            if not table:
                                continue
                            parsed = _parse_pdfplumber_table(table)
                            if len(parsed) >= 3:  # at least 3 classes = real certificate table
                                if len(parsed) > len(best_classes):
                                    best_classes = parsed
                    except Exception as e:
                        logger.debug(f"Table extraction failed (page {pg.page_number}, strategy {strategy}): {e}")

            if len(best_classes) >= 3:
                break  # found good data; stop iterating candidates

    # ── Text-based fallback ───────────────────────────────────────────────
    # If pdfplumber table extraction found < 3 classes, fall back to parsing
    # the plain text of candidate pages directly (line by line).
    # This handles PDFs where the certificate table has no border lines.
    if len(best_classes) < 3:
        logger.info("pdfplumber table extraction insufficient — trying text-based fallback")
        best_classes = _extract_certificate_classes_from_text(pages, candidate_page_nums)

    if best_classes:
        logger.info(
            f"Certificate table direct extraction found {len(best_classes)} classes: "
            f"{[c.get('class_name') for c in best_classes]}"
        )
    else:
        logger.info("Certificate table direct extraction: no usable data found")

    return best_classes


def _extract_certificate_classes_from_text(
    pages: List[Tuple[int, str]],
    candidate_page_nums: List[int],
) -> List[Dict]:
    """
    Text-based certificate table parser.
    Parses the plain-text output of pdfplumber (already extracted) to build
    class dicts line by line.  Works for PDFs with no table border lines.
    """
    # Pattern: "Class A-1 $126,186,000 6.81655% (6) Senior/Floater/Pro Rata AAA(sf) AAA(sf) 46656UAA1"
    # Also handles footnote markers after class name: "Class B-1(5) $6,057,000"
    row_re = re.compile(
        r"Class\s+([\w\-]+)(?:\(\d+\))?"      # class name + optional footnote
        r"\s+(\$[\d,]+(?:\.\d+)?(?:\(\d+\))?|N/A(?:\(\d+\))?)"  # principal or N/A
        r"(?:\s+([\d.]+%|N/A(?:\(\d+\))?))?",  # optional rate
        re.IGNORECASE,
    )
    # Type keywords → type classification
    _TYPE_MAP = {
        "senior": "Senior",
        "mezzanine": "Mezzanine",
        "subordinate": "Subordinate",
        "residual": "Residual",
        "remic": "Residual",
        "exchangeable": "Exchangeable",
        "notional": "Notional",
        "excess servicing": "Notional",
    }

    classes: List[Dict] = []
    page_dict = {pnum: text for pnum, text in pages}

    # Search candidate pages and their immediate neighbours
    search_pages = set()
    for pnum in candidate_page_nums:
        for offset in range(-1, 4):
            search_pages.add(pnum + offset)

    for pnum in sorted(search_pages):
        text = page_dict.get(pnum, "")
        if not text:
            continue
        for m in row_re.finditer(text):
            cname = m.group(1).strip()
            principal_raw = m.group(2) or ""
            rest_of_line = text[m.end():m.end() + 120].replace("\n", " ")

            cls: Dict[str, Any] = {"class_name": cname}

            # Parse principal
            if re.search(r"N/A", principal_raw, re.IGNORECASE):
                cls["is_residual"] = True
                cls["initial_principal"] = 0.0
            else:
                amt_m = re.search(r"\$([\d,]+(?:\.\d+)?)", principal_raw)
                if amt_m:
                    val = float(amt_m.group(1).replace(",", ""))
                    if val > 100:
                        cls["initial_principal"] = val

            # Classify type from surrounding text
            ctx = rest_of_line.lower()
            for kw, cls_type in _TYPE_MAP.items():
                if kw in ctx:
                    cls["type"] = cls_type
                    if cls_type in ("Residual",):
                        cls["is_residual"] = True
                    elif cls_type in ("Notional",):
                        cls["is_notional"] = True
                    elif cls_type == "Exchangeable":
                        cls["is_exchangeable"] = True
                    break

            # Extract CUSIP (9-char alphanumeric)
            cusip_m = re.search(r"\b([A-Z0-9]{9})\b", rest_of_line)
            if cusip_m:
                cls["cusip"] = cusip_m.group(1)

            # Extract ratings
            ratings = re.findall(r"(AAA|AA|A|BBB|BB|B|Not Rated|NR)(?:\+|-)?\(sf\)", rest_of_line, re.IGNORECASE)
            if len(ratings) >= 1:
                cls["fitch_rating"] = ratings[0]
            if len(ratings) >= 2:
                cls["kbra_rating"] = ratings[1]

            if cls.get("initial_principal") is not None or cls.get("is_residual"):
                classes.append(cls)

    # Deduplicate — keep last seen value per class (later occurrence = more specific)
    seen: Dict[str, Dict] = {}
    for cls in classes:
        norm = _normalize_class_name(cls["class_name"])
        if norm not in seen or cls.get("initial_principal", 0) > 0:
            seen[norm] = cls

    return list(seen.values())


def _extract_class_margins_from_text(full_text: str) -> Dict[str, float]:
    """
    Deterministically extract SOFR margins for floating-rate classes from the document.

    Handles the standard sentence:
      "The applicable margin for the Class A-1, Class M-1, ... will be
       1.75000 %, 2.50000 %, ..., respectively."

    Returns {class_name: margin_as_decimal}, e.g. {"A-1": 0.0175, "M-1": 0.025, ...}
    """
    margin_map: Dict[str, float] = {}

    # ── Pattern 1: "applicable margin for Class X, Y, Z will be P%, Q%, R%" ──
    block_pattern = re.compile(
        r"applicable\s+margin\s+for\s+(?:the\s+)?(.{30,600}?)"
        r"will\s+be\s+(.{20,300}?),?\s+respectively",
        re.IGNORECASE | re.DOTALL,
    )
    for m in block_pattern.finditer(full_text):
        class_block = m.group(1)
        rate_block  = m.group(2)

        # Normalise whitespace (collapse line breaks within hyphenated class names)
        # e.g. "Class B-\n2" → "Class B-2"
        class_block_clean = re.sub(r"-\s*\n\s*(\d)", r"-\1", class_block)
        class_block_clean = re.sub(r"\s+", " ", class_block_clean)

        # Extract class names — ONLY names starting with uppercase (not "of", "such", etc.)
        cls_names = re.findall(r"Class\s+([A-Z][\w\-]*)", class_block_clean)
        # Remove generic words that look like class names
        cls_names = [
            n for n in cls_names
            if n.upper() not in {"CERTIFICATES", "PRINCIPAL", "NOTIONAL", "NOTES"}
        ]
        # Extract percentage values in order (e.g. "1.75000 %" or "1.75%")
        pct_vals  = re.findall(r"([\d]+\.[\d]+)\s*%", rate_block)

        if cls_names and pct_vals:
            for cname, pct in zip(cls_names, pct_vals):  # zip stops at shorter list
                margin_map[cname] = float(pct) / 100.0
            if len(cls_names) == len(pct_vals):
                logger.info(f"Extracted margins for {list(margin_map.keys())} from 'applicable margin' sentence")
            else:
                logger.info(f"Partial margin extraction: {len(margin_map)} classes ({len(cls_names)} names / {len(pct_vals)} values)")

    # ── Pattern 2: Per-class inline patterns "Class A-1 ... SOFR plus X.XXXXX%" ──
    inline_pattern = re.compile(
        r"Class\s+([\w\-]+)\s+[^.]{0,200}?(?:SOFR|benchmark)\s+plus\s+([\d]+\.[\d]+)\s*%",
        re.IGNORECASE | re.DOTALL,
    )
    for m in inline_pattern.finditer(full_text):
        cname = m.group(1).strip()
        margin_val = float(m.group(2)) / 100.0
        if cname not in margin_map:   # don't overwrite block-parsed values
            margin_map[cname] = margin_val

    # ── Pattern 3: Fixed rate extraction for IO/excess servicing classes ──
    # e.g. "Class A-IO-S ... 0.39948%"
    io_rate_pattern = re.compile(
        r"Class\s+(A-IO-S|AIO|A-IO[\w\-]*)\s*[^\n]{0,150}?([\d]+\.[\d]+)\s*%",
        re.IGNORECASE,
    )
    for m in io_rate_pattern.finditer(full_text):
        cname = m.group(1).strip()
        rate_val = float(m.group(2)) / 100.0
        if rate_val < 0.05:  # reasonable IO/excess servicing strip rate
            margin_map[f"{cname}_fixed"] = rate_val  # store separately

    return margin_map


def _extract_special_rate_types_from_text(full_text: str) -> Dict[str, Dict[str, Any]]:
    """
    Detect special interest rate types for non-standard classes from footnotes.
    Returns {class_name: {"interest_rate_type": ..., "fixed_rate": ...}}
    """
    special: Dict[str, Dict[str, Any]] = {}

    # Principal-only class
    po_pattern = re.compile(
        r"Class\s+([\w\-]+)\s+Certificates?\s+[^\n]{0,300}?"
        r"(?:principal\s+only|will\s+not\s+receive\s+any\s+distributions\s+of\s+interest)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in po_pattern.finditer(full_text):
        special[m.group(1)] = {"interest_rate_type": "principal_only", "fixed_rate": 0.0, "margin": 0.0}

    # Excess cashflow class (X class)
    xc_pattern = re.compile(
        r"Class\s+(X[\w\-]*|XS)\s+Certificates?\s+[^\n]{0,200}?(?:monthly\s+excess\s+cashflow|residual)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in xc_pattern.finditer(full_text):
        special.setdefault(m.group(1), {"interest_rate_type": "excess_cashflow", "margin": 0.0})

    # IO / excess servicing strip
    io_pattern = re.compile(
        r"Class\s+(A-IO-S|A-IO[\w\-]*|AIO[\w\-]*)\s+Certificates?\s+[^\n]{0,300}?"
        r"(?:excess\s+servicing|notional)",
        re.IGNORECASE | re.DOTALL,
    )
    for m in io_pattern.finditer(full_text):
        special.setdefault(m.group(1), {"interest_rate_type": "io", "margin": 0.0})

    # Exchangeable class
    exc_pattern = re.compile(
        r"Class\s+(BX|[\w\-]+X)\s+Certificates?\s+[^\n]{0,200}?exchangeable",
        re.IGNORECASE | re.DOTALL,
    )
    for m in exc_pattern.finditer(full_text):
        special.setdefault(m.group(1), {"interest_rate_type": "exchangeable", "margin": 0.0})

    return special


def apply_regex_to_classes(
    classes_raw: List[Dict],
    regex_data: Dict[str, Any],
    direct_classes: Optional[List[Dict]] = None,
) -> List[Dict]:
    """
    Merge authoritative values (from pdfplumber direct extraction and regex) into
    the LLM-extracted class list.

    Priority order (highest → lowest):
      1. pdfplumber direct extraction  (deterministic, from structured table)
      2. regex pre-scan anchored to certificate section
      3. LLM extraction  (used only for fields not captured above)

    Both pdfplumber and regex values **always override** the LLM — not just fill gaps.
    This is intentional: LLM can hallucinate dollar amounts from other tables;
    the structured/regex sources read the certificate table directly.
    """
    principals_map_raw: Dict[str, float] = regex_data.get("regex_class_principals", {})
    margins_map_raw: Dict[str, float]         = regex_data.get("regex_class_margins", {})
    special_types:   Dict[str, Dict[str, Any]] = regex_data.get("special_rate_types", {})

    # Build normalised lookup maps
    principals_map = {_normalize_class_name(k): v for k, v in principals_map_raw.items()}
    margins_map    = {_normalize_class_name(k): v for k, v in margins_map_raw.items()}
    special_map    = {_normalize_class_name(k): v for k, v in special_types.items()}

    # Build a map from direct (pdfplumber) extraction if available
    direct_map: Dict[str, Dict] = {}
    if direct_classes:
        for dc in direct_classes:
            norm = _normalize_class_name(dc.get("class_name", ""))
            if norm:
                direct_map[norm] = dc

    # ── Apply to existing LLM classes ─────────────────────────────────────
    for raw_cls in classes_raw:
        cname = str(raw_cls.get("class_name", "")).strip()
        if not cname:
            continue
        norm_name = _normalize_class_name(cname)

        # 1. Pdfplumber direct extraction wins for initial_principal, cusip, ratings
        if norm_name in direct_map:
            dc = direct_map[norm_name]
            if dc.get("initial_principal") is not None:
                old = raw_cls.get("initial_principal")
                raw_cls["initial_principal"] = dc["initial_principal"]
                if old != dc["initial_principal"]:
                    logger.info(
                        f"Direct-table override: {cname}.initial_principal "
                        f"{old} → {dc['initial_principal']}"
                    )
            for field in ("cusip", "fitch_rating", "kbra_rating", "type",
                          "is_notional", "is_residual", "is_exchangeable"):
                if dc.get(field) is not None and not raw_cls.get(field):
                    raw_cls[field] = dc[field]

        # 2. Regex principal override — always apply (not just fill gaps)
        elif norm_name in principals_map:
            old = _safe_float(raw_cls.get("initial_principal"))
            new_val = principals_map[norm_name]
            if old != new_val:
                logger.info(
                    f"Regex override: {cname}.initial_principal "
                    f"{old} → {new_val}"
                )
            raw_cls["initial_principal"] = new_val

        # 3. Margin — ALWAYS override with authoritative value (not just fill gaps)
        if norm_name in margins_map:
            old_margin = raw_cls.get("margin")
            new_margin = margins_map[norm_name]
            if old_margin != new_margin:
                logger.info(f"Margin override: {cname} {old_margin} → {new_margin}")
            raw_cls["margin"] = new_margin
            if not raw_cls.get("interest_rate_type"):
                raw_cls["interest_rate_type"] = "floating"

        # 4. Special rate types (principal_only, excess_cashflow, io, exchangeable)
        if norm_name in special_map:
            sp = special_map[norm_name]
            old_type = raw_cls.get("interest_rate_type")
            new_type = sp.get("interest_rate_type")
            if new_type and old_type != new_type:
                logger.info(f"Special rate type override: {cname} {old_type} → {new_type}")
                raw_cls["interest_rate_type"] = new_type
                raw_cls["margin"] = sp.get("margin", 0.0)
                if sp.get("fixed_rate") is not None:
                    raw_cls["fixed_rate"] = sp["fixed_rate"]

    # ── Add classes found by direct/regex that LLM missed entirely ────────
    existing_norms = {_normalize_class_name(r.get("class_name", "")) for r in classes_raw}

    # From pdfplumber direct extraction
    if direct_classes:
        for dc in direct_classes:
            norm = _normalize_class_name(dc.get("class_name", ""))
            if norm and norm not in existing_norms:
                orig_name = dc["class_name"]
                classes_raw.append({
                    **dc,
                    "interest_rate_type": dc.get("interest_rate_type", "floating"),
                    "margin": margins_map.get(norm),
                })
                existing_norms.add(norm)
                logger.info(f"Direct-table added missing class: {orig_name}")

    # From regex (only if not already covered by direct)
    for norm_cname, principal in principals_map.items():
        if norm_cname not in existing_norms:
            orig_name = next(
                (k for k in principals_map_raw if _normalize_class_name(k) == norm_cname),
                norm_cname,
            )
            classes_raw.append({
                "class_name": orig_name,
                "initial_principal": principal,
                "interest_rate_type": "floating" if norm_cname in margins_map else "fixed",
                "margin": margins_map.get(norm_cname),
                "type": (
                    "Senior"      if orig_name.upper().startswith("A") else
                    "Mezzanine"   if orig_name.upper().startswith("M") else
                    "Subordinate"
                ),
            })
            existing_norms.add(norm_cname)
            logger.info(f"Regex added missing class: {orig_name} with principal {principal}")

    return classes_raw


# ---------------------------------------------------------------------------
# Section page scoring
# ---------------------------------------------------------------------------

SECTION_KEYWORDS: Dict[str, List[str]] = {
    "certificate_table": [
        "INITIAL CLASS PRINCIPAL", "APPROXIMATE INITIAL PASS-THROUGH",
        "Class A-1", "Class A-2", "CUSIP", "initial principal balance",
        "Initial Certificate Principal", "Class | CUSIP", "Class A-1 |",
    ],
    "priority_of_payments": [
        "Priority of Distributions", "Priority of Payments",
        "Interest Remittance Amount", "Principal Remittance Amount",
        "Monthly Excess Cashflow", "On each Distribution Date",
        "Available Distribution Amount", "Available Funds",
        "first, to pay", "second, to pay", "third, to pay",
    ],
    "fees_expenses": [
        "Servicing Fee Rate", "Securities Administrator Fee",
        "Trustee Fee", "Custodian Fee", "Loan Data Agent Fee",
        "basis points", "per annum", "annual fee",
        "fees and expenses", "transaction parties",
    ],
    "deal_summary": [
        "Cut-off Date", "Closing Date", "First Distribution Date",
        "First Payment Date", "Pricing Date", "Depositor",
        "Sponsor", "Originator", "Issuing Entity",
    ],
    "loss_allocation": [
        "Realized Loss", "Applied Realized Loss", "Loss Allocation",
        "Writedown Amount", "Credit Enhancement", "Trigger Test",
        "CE Test", "OC Test", "Overcollateralization",
    ],
}


def score_pages(pages: List[Tuple[int, str]]) -> Dict[str, List[Tuple[int, int]]]:
    """
    Score each page for each section type.
    Returns {section: [(page_num, score), ...]} sorted by score desc.
    """
    scores: Dict[str, List[Tuple[int, int]]] = {k: [] for k in SECTION_KEYWORDS}
    for page_num, text in pages:
        text_upper = text.upper()
        for section, keywords in SECTION_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.upper() in text_upper)
            if score > 0:
                scores[section].append((page_num, score))
    # Sort each section by score descending
    for section in scores:
        scores[section].sort(key=lambda x: x[1], reverse=True)
    return scores


# UI section key -> scoring section key. Frontend uses the left-hand keys to
# look up which PDF pages to show when a given Edit section is active.
_UI_SECTION_TO_SCORE: Dict[str, str] = {
    "deal_info":           "deal_summary",
    "certificate_classes": "certificate_table",
    "fees":                "fees_expenses",
    "waterfall":           "priority_of_payments",
    "triggers":            "loss_allocation",
}


def _build_section_page_map(
    scores: Dict[str, List[Tuple[int, int]]],
    total_pages: int,
    top_n: int = 5,
) -> Dict[str, List[int]]:
    """Top-N 1-indexed page numbers per UI section, derived from the scoring pass."""
    result: Dict[str, List[int]] = {}
    for ui_key, score_key in _UI_SECTION_TO_SCORE.items():
        top = [p for p, _ in scores.get(score_key, [])[:top_n] if 1 <= p <= total_pages]
        if top:
            result[ui_key] = top
    return result


def get_best_pages_text(
    pages: List[Tuple[int, str]],
    scores: Dict[str, List[Tuple[int, int]]],
    section: str,
    window: int = 8,
    max_chars: int = 18000,
    top_n: int = 5,
) -> str:
    """
    Get combined text of the top-scored pages for a section,
    including a window of surrounding pages for context.
    """
    page_dict = {pnum: text for pnum, text in pages}
    top_pages = [p for p, _ in scores.get(section, [])[:top_n]]
    if not top_pages:
        return ""

    # Expand to include surrounding pages
    page_set: set = set()
    for p in top_pages:
        for offset in range(-2, window + 1):
            pg = p + offset
            if pg in page_dict:
                page_set.add(pg)

    # Sort and join
    combined = ""
    for pnum in sorted(page_set):
        text = page_dict.get(pnum, "")
        combined += f"\n\n=== PAGE {pnum} ===\n{text}"
        if len(combined) >= max_chars:
            break
    return combined[:max_chars]


# ---------------------------------------------------------------------------
# LLM call helper with JSON repair
# ---------------------------------------------------------------------------

# Diagnostic counter for per-call IDs (so parallel calls can be told apart in logs)
_llm_call_id = itertools.count(1)


def _classify_llm_error(exc: BaseException) -> str:
    """Return 'RATE_LIMIT' / 'TIMEOUT' / 'AUTH' / 'ERROR' based on the exception."""
    try:
        import openai  # local import; openai is already a transitive dep
        if isinstance(exc, openai.RateLimitError):
            return "RATE_LIMIT"
        if isinstance(exc, openai.APITimeoutError):
            return "TIMEOUT"
        if isinstance(exc, openai.AuthenticationError):
            return "AUTH"
    except Exception:
        pass
    msg = str(exc).lower()
    if "429" in msg or "rate limit" in msg or "rate_limit" in msg or "too many requests" in msg:
        return "RATE_LIMIT"
    if "timeout" in msg or "timed out" in msg:
        return "TIMEOUT"
    return "ERROR"


async def _call_llm(
    llm: AzureChatOpenAI,
    system_prompt: str,
    user_content: str,
    expect_list: bool = False,
) -> Any:
    """Call LLM, parse JSON response. Tries to repair truncated JSON."""
    cid = next(_llm_call_id)
    t0 = time.perf_counter()
    logger.info(f"[LLM #{cid}] start  in_chars={len(user_content)}")
    try:
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_content),
        ]
        try:
            response = await llm.ainvoke(messages)
        except Exception as call_exc:
            elapsed = time.perf_counter() - t0
            kind = _classify_llm_error(call_exc)
            retry_after = getattr(getattr(call_exc, "response", None), "headers", {})
            retry_after_val = retry_after.get("retry-after") if hasattr(retry_after, "get") else None
            logger.warning(
                f"[LLM #{cid}] {kind} after {elapsed:.1f}s  "
                f"retry_after={retry_after_val}  {type(call_exc).__name__}: {str(call_exc)[:200]}"
            )
            raise

        elapsed = time.perf_counter() - t0
        usage = getattr(response, "usage_metadata", None) or {}
        in_tok = usage.get("input_tokens", "?")
        out_tok = usage.get("output_tokens", "?")
        logger.info(f"[LLM #{cid}] done in {elapsed:.1f}s  in_tok={in_tok} out_tok={out_tok}")

        content = response.content.strip()

        # Strip markdown code blocks
        json_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content, re.DOTALL)
        if json_match:
            content = json_match.group(1).strip()

        # Try direct parse
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass

        # Try to find JSON object/array in the response
        for pattern in (r'(\{[\s\S]+\})', r'(\[[\s\S]+\])'):
            m = re.search(pattern, content)
            if m:
                try:
                    return json.loads(m.group(1))
                except json.JSONDecodeError:
                    pass

        # Last resort: fix truncated JSON by appending closing brackets
        for suffix in ("]}", "}]", "}", "]"):
            try:
                return json.loads(content + suffix)
            except Exception:
                pass

        logger.warning(f"[LLM #{cid}] Could not parse response as JSON. Preview: {content[:300]}")
        return [] if expect_list else {}
    except Exception as e:
        elapsed = time.perf_counter() - t0
        logger.error(f"[LLM #{cid}] failed after {elapsed:.1f}s: {e}")
        return [] if expect_list else {}


# ---------------------------------------------------------------------------
# Extraction prompts (greatly improved)
# ---------------------------------------------------------------------------

DEAL_INFO_PROMPT = """You are an expert ABS/MBS structured finance analyst. Your job is to extract specific deal configuration fields from an offering document.

CRITICAL: Read the ENTIRE text carefully. Values may appear anywhere across multiple pages.
Return ONLY a valid JSON object. Use null for truly missing values — never guess or fabricate.

Extract these fields:
{
  "deal_name": "full official deal name e.g. J.P. Morgan Mortgage Trust 2023-HE1",
  "series": "series identifier e.g. 2023-HE1",
  "issuing_entity": "name of the issuing trust/entity",
  "depositor": "depositor name",
  "sponsors": ["sponsor 1", "sponsor 2"],
  "servicers": [
    {"servicer_name": "name", "servicing_fee_rate": 0.0025, "advance_obligation": true/false, "portfolio_pct": 1.0}
  ],
  "originators": ["originator 1"],
  "custodian": "custodian name",
  "securities_administrator": "SA name",
  "owner_trustee": "trustee name",
  "underwriters": ["underwriter 1"],
  "rating_agencies": ["Fitch", "Kroll", "Moody's"],
  "closing_date": "YYYY-MM-DD",
  "cut_off_date": "YYYY-MM-DD",
  "first_payment_date": "YYYY-MM-DD",
  "legal_maturity_date": "YYYY-MM-DD",
  "pricing_date": "YYYY-MM-DD",
  "asset_class": "Residential Real Estate",
  "asset_type": "HELOC or Mortgage or Auto etc.",
  "payment_frequency": "Monthly",
  "original_pool_balance": 186391076.0,
  "lien_position": "First Lien or Second Lien",
  "revolving_period": true/false,
  "revolving_period_end_date": "YYYY-MM-DD or null",
  "benchmark": "SOFR",
  "benchmark_tenor": "1M",
  "interest_day_count": "actual/360",
  "cleanup_call_pct": 0.10
}

HINTS for finding values:
- Cut-off Date / Closing Date: Usually in first 30 pages, near the title page or summary table
- Original Pool Balance: Look for "aggregate... principal balance" or "initial pool balance" followed by a dollar amount
- Servicer fee rates: Usually expressed as "X basis points per annum" — convert to decimal (25bps = 0.0025)
- Cleanup call: Usually "Optional Redemption" or "Cleanup Call" at 10% of original balance

Document text (multiple pages):
{text}
"""


CLASSES_PROMPT = """You are an expert ABS/MBS structured finance analyst. Your ONLY job here is to
extract certificate class data from THE OFFERED CERTIFICATES (or equivalent) table in the document.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
ANTI-HALLUCINATION RULES  (read these first)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
1. NEVER invent a class that is not explicitly listed in the certificate table.
2. NEVER copy a dollar amount from any table other than the certificate/tranche table
   (e.g., do NOT use balances from a credit-enhancement table, subordination table,
   waterfall distribution table, or any other section).
3. The ONLY correct source for initial_principal is the column labelled
   "INITIAL CLASS PRINCIPAL AMOUNT", "CLASS PRINCIPAL AMOUNT", or equivalent
   in the certificate offering table. Copy the exact number — do not round or estimate.
4. If a class has "N/A" for its principal (usually IO/Notional/Residual), set
   initial_principal = 0 and the appropriate flag.
5. Return null for any field you cannot find — do not guess.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO FIND THE TABLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Look for a section headed "THE OFFERED CERTIFICATES", "SUMMARY OF THE CERTIFICATES",
or "INITIAL CLASS PRINCIPAL AMOUNT".  The table rows will look like:
  Class A-1  $126,186,000  6.81655%  Senior/Floater/Pro Rata  AAA(sf)  AAA(sf)  46656UAA1
or (pipe-delimited from PDF table extraction):
  A-1 | 46656UAA1 | $126,186,000 | 6.81655% | Senior/Floater | AAA(sf) | AAA(sf)

Return a JSON ARRAY.  Each object:
{
  "class_name": "A-1",
  "cusip": "46656UAA1",
  "type": "Senior",            // Senior | Mezzanine | Subordinate | Exchangeable | Residual
  "sub_type": "Floater",
  "is_notional": false,
  "is_residual": false,
  "is_exchangeable": false,
  "exchange_group": null,
  "initial_principal": 126186000.0,   // ← from INITIAL CLASS PRINCIPAL column ONLY
  "interest_rate_type": "floating",   // floating | fixed | excess_cashflow
  "fixed_rate": null,
  "margin": 0.0175,                   // SOFR SPREAD only — see MARGIN RULES below
  "benchmark": "SOFR",
  "rate_cap": null,
  "accrual_convention": "actual/360",
  "expected_final_date": "2027-08",
  "final_scheduled_date": "2053-08",
  "interest_priority": 1,
  "principal_priority": 1,
  "principal_method": "sequential",   // sequential | pro_rata
  "fitch_rating": "AAA(sf)",
  "kbra_rating": "AAA(sf)"
}

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MARGIN RULES (CRITICAL — read carefully)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
The "APPROXIMATE INITIAL PASS-THROUGH RATE" column (e.g., 6.81655%) shows the TOTAL rate
at issuance = SOFR + margin.  Do NOT store this column value as the margin.

The actual margin (SOFR spread) is stated in a footnote, e.g.:
  "(6) The applicable margin for Class A-1, M-1, ... will be 1.75000%, 2.50000%, ..."
or inline: "Class A-1 ... SOFR plus 1.75000% per annum"

WHAT TO SET for `margin`:
- Floating: extract the SOFR spread from the footnote text. For A-1 it is 1.75% = 0.0175
- If you cannot find the spread, set margin = null (do NOT use the approximate pass-through rate)
- Principal-only class (footnote says "principal only", no interest): set interest_rate_type = "principal_only", margin = 0
- Excess Cashflow / X class: interest_rate_type = "excess_cashflow", margin = 0, fixed_rate = null
- IO / Notional Excess Servicing (A-IO-S): interest_rate_type = "io", set fixed_rate to the stated %
- Exchangeable (BX): interest_rate_type = "exchangeable", margin = 0
- REMIC Residual (A-R): interest_rate_type = "residual", margin = 0, initial_principal = 0
- Rate formula references like "(8)" or "(13)": check what the footnote describes and set the appropriate type

WRONG:  "margin": 0.0681655  (this is the full pass-through rate, NOT the spread)
CORRECT: "margin": 0.0175   (this is the actual SOFR spread from the footnote)

FIELD NOTES:
- initial_principal: copy the EXACT dollar figure from the certificate table. $126,186,000 → 126186000.0
- IO / Notional: initial_principal = notional amount shown, is_notional = true
- A-R (Residual): initial_principal = 0, is_residual = true, interest_rate_type = "residual"
- type: Senior = A-classes (except IO); Mezzanine = M-classes; Subordinate = B-classes; Residual = R/A-R
- Exchangeable classes (BX, etc.): is_exchangeable = true, type = "Exchangeable"
- Priorities: number sequentially by seniority (A-1=1, A-2=2, M-1=3, …)
- "NR" or blank ratings → null

Document text (look for the certificate table):
{text}
"""


FEES_PROMPT = """You are an expert ABS/MBS structured finance analyst.

TASK: Extract ALL fees, costs and expenses from the document.

Return a JSON ARRAY:
[
  {
    "fee_name": "Servicing Fee",
    "fee_type": "percentage",
    "fee_rate": 0.0025,
    "fixed_amount": null,
    "priority": 1,
    "fee_cap": null,
    "applies_to": "pool_balance",
    "servicer_name": "Servicer Name or null"
  }
]

COMMON FEES TO LOOK FOR:
- Servicing Fee (usually 25bps or 0.25% per annum on pool balance)
- Backup Servicing Fee
- Securities Administrator Fee / SA Fee (often 1-3bps)
- Owner Trustee / Indenture Trustee Fee
- Custodian Fee
- Loan Data Agent Fee
- Rating Agency Fee (if recurring)
- Annual Expense Cap (if stated — add as fixed fee)

CONVERSION: "X basis points" = X/10000 as decimal rate. "X bps" = X/10000.
For fixed fees, express as annual dollar amount.
Priority 1 = paid first (usually servicing fee), higher numbers = lower priority.

Document text:
{text}
"""


WATERFALL_PROMPT = """You are an expert ABS/MBS structured finance analyst.

TASK: Extract the complete Priority of Payments (waterfall) from the document.
This section usually starts with "On each Distribution Date, the [Securities Administrator / Trustee] shall distribute Available Funds as follows..."

Return a JSON object with THREE arrays:

{
  "interest_waterfall": [
    {
      "step": 1,
      "description": "Pay Class A-1 Interest",
      "class_name": "A-1",
      "payment_type": "interest",
      "source_bucket": "interest_remittance",
      "condition": "always",
      "concurrent_with": null
    }
  ],
  "principal_waterfall": [ ...same format... ],
  "excess_cashflow_waterfall": [ ...same format... ]
}

EXTRACTION RULES:
1. Follow the numbered list in the document exactly — each "first", "second", "third" etc. is a step
2. interest_waterfall: from Interest Remittance Amount → pay interest to each class in order
3. principal_waterfall: from Principal Remittance Amount → pay principal to each class
4. excess_cashflow_waterfall: the "Monthly Excess Cashflow" distribution steps

payment_type values: "interest", "principal", "reserve", "excess", "fee", "loss_reimbursement"
source_bucket values: "interest_remittance", "principal_remittance", "excess_cashflow", "available_funds"
condition values: "always", "trigger_failure", "trigger_pass", null

For each step include the exact class name it applies to (e.g., "A-1", "M-1", "B-5").
If a step says "Class A-1, A-2, and A-3" — create separate steps for each class.

Document text:
{text}
"""


TRIGGERS_PROMPT = """You are an expert ABS/MBS structured finance analyst.

TASK: Extract loss allocation order, trigger tests, and reserve accounts from the document.

Return JSON:
{
  "loss_allocation_order": ["B-5", "B-4", "B-3", "B-2", "B-1", "M-2", "M-1"],
  "triggers": [
    {
      "test_name": "Credit Enhancement Test",
      "test_type": "ce",
      "description": "CE must be >= threshold",
      "threshold": 0.05,
      "operator": "greater_than",
      "trigger_on_failure": "Sequential principal distribution to seniors"
    }
  ],
  "reserve_accounts": [
    {
      "account_name": "Reserve Account",
      "initial_balance": 0.0,
      "target_amount": null,
      "funded_from": "excess cashflow",
      "released_to": "available funds"
    }
  ]
}

LOSS ALLOCATION: Most subordinate class absorbs losses first (usually B-5 or lowest rated).
TEST TYPES: "oc" (overcollateralization), "ce" (credit enhancement), "delinquency", "cleanup_call", "other"
THRESHOLD: Express as decimal (5% = 0.05).

Document text:
{text}
"""


VALIDATION_PROMPT = """You are an expert ABS/MBS structured finance analyst doing a VALIDATION PASS.

The following fields were NOT found in the previous extraction. Search the document carefully for them.
Return a JSON object with ONLY the fields you can find. Use null for those you cannot find.

MISSING FIELDS:
{missing_fields}

WHAT TO LOOK FOR:
- cut_off_date: Often in first few pages as "Cut-off Date: [month] [day], [year]"
- initial_principal for each class: In the certificate table, usually a dollar amount in millions
- waterfall steps: Look for numbered list starting with "first, to pay..."
- margins for floating rate classes: "SOFR + [X] basis points" or "SOFR + [X]%"

Document text:
{text}
"""


# ---------------------------------------------------------------------------
# Safe type helpers
# ---------------------------------------------------------------------------

def _safe_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, str):
        val = val.replace(",", "").replace("$", "").replace("%", "").strip()
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _build_certificate_class(raw: Dict) -> Optional[CertificateClass]:
    try:
        class_name = str(raw.get("class_name", "")).strip()
        if not class_name:
            return None
        return CertificateClass(
            class_name=class_name,
            cusip=raw.get("cusip"),
            type=str(raw.get("type", "Senior")).strip(),
            sub_type=raw.get("sub_type"),
            is_notional=bool(raw.get("is_notional", False)),
            is_exchangeable=bool(raw.get("is_exchangeable", False)),
            exchange_group=raw.get("exchange_group"),
            is_residual=bool(raw.get("is_residual", False)),
            initial_principal=_safe_float(raw.get("initial_principal")) or 0.0,
            interest_rate_type=str(raw.get("interest_rate_type", "fixed")).lower(),
            fixed_rate=_safe_float(raw.get("fixed_rate")),
            margin=_safe_float(raw.get("margin")),
            benchmark=raw.get("benchmark"),
            rate_cap=_safe_float(raw.get("rate_cap")),
            rate_floor=0.0,
            accrual_convention=str(raw.get("accrual_convention", "actual/360")),
            expected_final_date=raw.get("expected_final_date"),
            final_scheduled_date=raw.get("final_scheduled_date"),
            interest_priority=int(raw.get("interest_priority") or 0),
            principal_priority=int(raw.get("principal_priority") or 0),
            principal_method=str(raw.get("principal_method", "sequential")),
            fitch_rating=raw.get("fitch_rating"),
            kbra_rating=raw.get("kbra_rating"),
        )
    except Exception as e:
        logger.warning(f"Could not build CertificateClass from {raw}: {e}")
        return None


def _build_fee_config(raw: Dict) -> Optional[FeeConfig]:
    try:
        fee_name = str(raw.get("fee_name", "")).strip()
        if not fee_name:
            return None
        return FeeConfig(
            fee_name=fee_name,
            fee_rate=_safe_float(raw.get("fee_rate")),
            fixed_amount=_safe_float(raw.get("fixed_amount")),
            fee_type=str(raw.get("fee_type", "percentage")),
            priority=int(raw.get("priority") or 1),
            fee_cap=_safe_float(raw.get("fee_cap")),
            applies_to=str(raw.get("applies_to", "pool_balance")),
            servicer_name=raw.get("servicer_name"),
        )
    except Exception as e:
        logger.warning(f"Could not build FeeConfig from {raw}: {e}")
        return None


def _build_waterfall_step(raw: Dict) -> Optional[WaterfallStep]:
    try:
        return WaterfallStep(
            step=int(raw.get("step") or 0),
            description=str(raw.get("description", "")),
            class_name=raw.get("class_name"),
            payment_type=str(raw.get("payment_type", "interest")),
            source_bucket=str(raw.get("source_bucket", "available_funds")),
            condition=raw.get("condition"),
            amount_formula=raw.get("amount_formula"),
            concurrent_with=raw.get("concurrent_with"),
            reserve_account=raw.get("reserve_account"),
        )
    except Exception as e:
        logger.warning(f"Could not build WaterfallStep from {raw}: {e}")
        return None


def _build_steps(raw_list: Any) -> List[WaterfallStep]:
    if not isinstance(raw_list, list):
        return []
    steps = [_build_waterfall_step(r) for r in raw_list if isinstance(r, dict)]
    return sorted([s for s in steps if s], key=lambda x: x.step)


# ---------------------------------------------------------------------------
# Hallucination guard — reconcile LLM classes against regex ground truth
# ---------------------------------------------------------------------------

def _reconcile_classes_with_regex(
    classes_raw: List[Dict],
    regex_data: Dict[str, Any],
) -> List[Dict]:
    """
    If the regex pre-scan found a reliable set of class names (>= 4 classes with
    distinct principals), use that set as the authoritative list and drop any
    LLM-generated class that is NOT in it.

    This prevents hallucinated classes like A-2/A-3 from appearing when the
    document only has A-1.
    """
    principals_map: Dict[str, float] = regex_data.get("regex_class_principals", {})
    if len(principals_map) < 4:
        # Not enough regex data to be authoritative — trust LLM
        return classes_raw

    authoritative_norms = {_normalize_class_name(k) for k in principals_map}
    reconciled = []
    removed = []
    for raw_cls in classes_raw:
        cname = str(raw_cls.get("class_name", "")).strip()
        norm = _normalize_class_name(cname)
        if norm in authoritative_norms:
            reconciled.append(raw_cls)
        else:
            removed.append(cname)

    if removed:
        logger.warning(
            f"Hallucination guard removed {len(removed)} LLM-invented classes "
            f"not found in the document: {removed}"
        )

    # Make sure every regex-verified class is present (regex may have found
    # classes the LLM also missed)
    existing_norms = {_normalize_class_name(r.get("class_name", "")) for r in reconciled}
    for orig_name, principal in principals_map.items():
        if _normalize_class_name(orig_name) not in existing_norms:
            reconciled.append({"class_name": orig_name, "initial_principal": principal})
            logger.info(f"Hallucination guard added missing class {orig_name}")

    logger.info(
        f"Reconciled class list ({len(reconciled)}): "
        f"{[r.get('class_name') for r in reconciled]}"
    )
    return reconciled


def _filter_waterfall_steps_by_classes(
    steps_raw: List[Dict],
    valid_class_names: set,
) -> List[Dict]:
    """
    Remove waterfall steps whose class_name doesn't exist in the deal.
    Non-class steps (reserve, fee, excess with no class_name) are kept.
    """
    filtered = []
    for s in steps_raw:
        ptype = str(s.get("payment_type", "")).lower()
        cname = s.get("class_name")
        if ptype in ("interest", "principal") and cname:
            if _normalize_class_name(cname) in {_normalize_class_name(v) for v in valid_class_names}:
                filtered.append(s)
            else:
                logger.warning(
                    f"Removing waterfall step {s.get('step')} — class '{cname}' "
                    f"not in deal classes {sorted(valid_class_names)}"
                )
        else:
            filtered.append(s)
    return filtered


# ---------------------------------------------------------------------------
# Formula generation — convert text steps to evaluatable math expressions
# ---------------------------------------------------------------------------

FORMULA_GENERATION_PROMPT = """You are an expert ABS/MBS structured finance engineer.

Convert each waterfall step description into a Python math expression that calculates
the AMOUNT to be paid at that step.

Use ONLY these pre-defined variables (do not invent new names):
  available_funds       — float: funds remaining before this step
  interest_due          — dict: interest_due.get("CLASS", 0)   — interest owed to a class
  balances              — dict: balances.get("CLASS", 0)        — beginning principal balance
  cap_carryover         — dict: cap_carryover.get("CLASS", 0)  — cap carryover owed to a class
  fee_amounts           — dict: fee_amounts.get("FEE_NAME", 0) — fee amount due
  reserve_balance       — float: current reserve account balance
  reserve_target        — float: target reserve account balance
  realized_loss         — float: total realized losses this period
  total_interest_due    — float: sum of all class interest due

STRICT RULES — follow exactly:
1. If payment_type == "interest" and class_name is set → ALWAYS use:
     min(interest_due.get("CLASS_NAME", 0), available_funds)
2. If payment_type == "principal" and class_name is set → ALWAYS use:
     min(balances.get("CLASS_NAME", 0), available_funds)
3. If the step is a reserve/OC account fill (no class, payment_type="reserve") → use:
     min(max(0, reserve_target - reserve_balance), available_funds)
4. For realized-loss reimbursement → min(realized_loss, available_funds)
5. For fee steps → min(fee_amounts.get("FEE_NAME", 0), available_funds)
6. For excess/residual pass-through → available_funds

CRITICAL: NEVER apply a reserve formula to a class interest or principal payment step.
If the description says "Pay the Class X-1 Interest Distribution Amount",
the formula MUST be: min(interest_due.get("X-1", 0), available_funds)

Given these waterfall steps:
{steps_json}

Return a JSON array — one object per step — with ONLY these fields:
[
  {{"step": <step_number>, "amount_formula": "<python expression>"}},
  ...
]

IMPORTANT:
- Use the exact class names from the step (e.g., "A-1", "M-1")
- Use min(..., available_funds) for every step except pure excess/residual pass-through
- Do NOT add imports, assignments, or multi-line code
- Keep each formula as a single Python expression
"""


async def _generate_step_formulas(
    llm: AzureChatOpenAI,
    all_steps: List[Dict],
) -> Dict[int, str]:
    """
    Call LLM to generate a math formula for each waterfall step.
    Returns {step_number: formula_string}.
    """
    if not all_steps:
        return {}
    try:
        steps_json = json.dumps(
            [{"step": s.get("step"), "description": s.get("description"),
              "class_name": s.get("class_name"), "payment_type": s.get("payment_type")}
             for s in all_steps],
            indent=2,
        )
        result = await _call_llm(
            llm,
            "You are an ABS/MBS engineer. Return ONLY a JSON array of {step, amount_formula} objects.",
            FORMULA_GENERATION_PROMPT.replace("{steps_json}", steps_json),
            expect_list=True,
        )
        if not isinstance(result, list):
            return {}
        # Build a lookup for payment_type and class_name by step number
        step_meta = {
            s.get("step"): {"ptype": s.get("payment_type", ""), "cname": s.get("class_name", "")}
            for s in all_steps
        }

        formula_map: Dict[int, str] = {}
        for item in result:
            step_num = item.get("step")
            formula = str(item.get("amount_formula", "")).strip()
            if step_num is None or not formula:
                continue
            # Safety check — no dangerous keywords
            if any(kw in formula for kw in ("import", "exec", "eval", "open", "os.", "sys.", ";")):
                logger.warning(f"Rejected unsafe formula for step {step_num}: {formula}")
                continue

            # ── Validate formula against payment_type ────────────────────────
            # If LLM generated a reserve-fill formula for a class payment step,
            # replace it with the correct class-based formula.
            meta = step_meta.get(step_num, {})
            ptype = meta.get("ptype", "").lower()
            cname = meta.get("cname", "")
            is_reserve_formula = "reserve_target" in formula or "reserve_balance" in formula

            is_balance_formula = "balances.get" in formula and "interest_due" not in formula

            if cname and ptype == "interest" and is_reserve_formula:
                corrected = f'min(interest_due.get("{cname}", 0), available_funds)'
                logger.warning(
                    f"Step {step_num} ({cname} interest): LLM generated reserve formula "
                    f"'{formula}' — replaced with '{corrected}'"
                )
                formula = corrected

            elif cname and ptype == "interest" and is_balance_formula:
                # Used balances.get instead of interest_due.get for an interest step
                corrected = f'min(interest_due.get("{cname}", 0), available_funds)'
                logger.warning(
                    f"Step {step_num} ({cname} interest): LLM used balances formula "
                    f"'{formula}' — replaced with '{corrected}'"
                )
                formula = corrected

            elif cname and ptype == "principal" and is_reserve_formula:
                corrected = f'min(balances.get("{cname}", 0), available_funds)'
                logger.warning(
                    f"Step {step_num} ({cname} principal): LLM generated reserve formula "
                    f"'{formula}' — replaced with '{corrected}'"
                )
                formula = corrected

            formula_map[int(step_num)] = formula
        logger.info(f"Generated {len(formula_map)} step formulas")
        return formula_map
    except Exception as e:
        logger.warning(f"Formula generation failed: {e}")
        return {}


def _apply_formulas_to_steps(
    steps_raw: List[Dict],
    formula_map: Dict[int, str],
) -> List[Dict]:
    """Attach generated formulas back to step dicts."""
    for s in steps_raw:
        step_num = s.get("step")
        if step_num is not None and step_num in formula_map:
            s["amount_formula"] = formula_map[step_num]
    return steps_raw


# ---------------------------------------------------------------------------
# Main extraction pipeline
# ---------------------------------------------------------------------------

async def extract_deal_config_from_pdf(
    pdf_path: str,
    deal_id: str,
    progress_callback=None,
) -> DealConfig:
    """
    High-accuracy multi-pass extraction pipeline.
    """
    async def _progress(step: str, pct: int):
        if progress_callback:
            try:
                result = progress_callback(step, pct)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass
        logger.info(f"[{pct}%] {step}")

    llm = _get_llm(temperature=0.0)

    # === PASS 1: Extract all pages (text + tables) ===
    await _progress("Extracting PDF text and tables", 5)
    pages = extract_pdf_pages(pdf_path)
    total_pages = len(pages)
    logger.info(f"Total pages: {total_pages}")

    # === PASS 2: Regex pre-scan across ALL pages ===
    await _progress("Scanning all pages for key values", 12)
    regex_data = regex_prescan(pages)

    # === PASS 3: Score pages for each section ===
    await _progress("Identifying best pages per section", 18)
    scores = score_pages(pages)
    for section, top in scores.items():
        logger.info(f"  {section}: top pages = {[p for p, _ in top[:5]]}")

    # === Prepare inputs for Group A (Pass 4 + Pass 5b LLM) ===
    # Pass 4 input: deal info pages
    deal_info_pages = set(range(1, min(41, total_pages + 1)))
    for pnum, _ in scores.get("deal_summary", [])[:8]:
        for offset in range(-2, 5):
            deal_info_pages.add(pnum + offset)
    deal_info_text = "\n\n".join(
        f"=== PAGE {p} ===\n{pages[p-1][1]}"
        for p in sorted(deal_info_pages)
        if 1 <= p <= total_pages
    )[:20000]

    # Pass 5b input: certificate table pages
    cert_text = get_best_pages_text(pages, scores, "certificate_table",
                                     window=10, max_chars=22000, top_n=6)
    early_pages_text = "\n\n".join(
        f"=== PAGE {p} ===\n{pages[p-1][1]}" for p in range(1, min(21, total_pages + 1))
    )
    cert_text = (early_pages_text + "\n\n" + cert_text)[:22000]

    # === GROUP A: Pass 4 LLM + Pass 5b LLM + Pass 5a (direct table parse) all in parallel ===
    # Pass 5a is blocking pdfplumber work, so we hand it off to a thread; the two LLM
    # calls run on the event loop. All three results are required before the override step.
    await _progress("Extracting deal info + cert classes + direct table (parallel)", 25)
    _grp_a_t0 = time.perf_counter()
    deal_info, classes_raw, direct_classes = await asyncio.gather(
        _call_llm(
            llm,
            "You are an expert ABS/MBS structured finance analyst. Extract exact field values as JSON. NEVER fabricate values.",
            # Use replace() instead of .format() so that braces in PDF text don't cause KeyError
            DEAL_INFO_PROMPT.replace("{text}", deal_info_text),
        ),
        _call_llm(
            llm,
            (
                "You are an expert ABS/MBS analyst. Extract ALL certificate classes from the "
                "'THE OFFERED CERTIFICATES' table. Return ONLY a JSON array. "
                "CRITICAL: copy initial_principal EXACTLY from the table — never round or estimate."
            ),
            CLASSES_PROMPT.replace("{text}", cert_text),
            expect_list=True,
        ),
        asyncio.to_thread(_extract_certificate_table_direct, pdf_path, pages),
    )
    logger.info(f"[Group A] wall-clock {time.perf_counter() - _grp_a_t0:.1f}s")
    logger.info(
        f"Direct table extraction: {len(direct_classes)} classes found — "
        f"{[c.get('class_name') for c in direct_classes]}"
    )

    # Pass 4 post-processing: merge regex findings as fallback
    for field, value in regex_data.items():
        if not deal_info.get(field):
            deal_info[field] = value
            logger.info(f"  Regex fallback: {field} = {value}")
    logger.info(f"Extracted deal info: deal_name={deal_info.get('deal_name')}, cut_off_date={deal_info.get('cut_off_date')}")

    # Pass 5b post-processing: normalize shape
    if isinstance(classes_raw, dict):
        for key in ("classes", "certificates", "tranches", "bonds"):
            if isinstance(classes_raw.get(key), list):
                classes_raw = classes_raw[key]
                break
    if not isinstance(classes_raw, list):
        classes_raw = []
    logger.info(f"LLM extracted {len(classes_raw)} certificate classes (before override)")

    # === PASS 5c: Override LLM values with authoritative direct/regex values ===
    # Priority: pdfplumber direct > regex > LLM
    # initial_principal from LLM is NEVER trusted over structured extraction.
    classes_raw = apply_regex_to_classes(classes_raw, regex_data, direct_classes=direct_classes)
    logger.info(f"After authoritative override: {len(classes_raw)} classes")

    # === HALLUCINATION GUARD: reconcile LLM classes against regex-verified names ===
    classes_raw = _reconcile_classes_with_regex(classes_raw, regex_data)

    # === PASS 6: Extract fees (scan fee sections + broad scan) ===
    # === Prepare inputs for Group B (Pass 6 + Pass 7 + Pass 8 in parallel) ===
    # Pass 6 input: fee pages
    fee_text = get_best_pages_text(pages, scores, "fees_expenses",
                                    window=8, max_chars=18000, top_n=5)
    if not fee_text:
        # Fallback: scan pages with "fee" or "basis points" in them
        fee_pages_fallback = [
            p for p, t in pages
            if "basis point" in t.lower() or "servicing fee" in t.lower()
        ][:10]
        fee_text = "\n\n".join(
            f"=== PAGE {p} ===\n{pages[p-1][1]}" for p in sorted(fee_pages_fallback)
        )[:18000]

    # Pass 7 input: waterfall pages
    wf_text_1 = get_best_pages_text(pages, scores, "priority_of_payments",
                                     window=15, max_chars=20000, top_n=5)
    if not wf_text_1:
        # Fallback: pages with "first, to pay" or "distribution date"
        wf_pages_fb = [
            p for p, t in pages
            if re.search(r"first,?\s+to\s+pay|distribution date", t, re.IGNORECASE)
        ][:8]
        wf_text_1 = "\n\n".join(
            f"=== PAGE {p} ===\n{pages[p-1][1]}" for p in sorted(wf_pages_fb)
        )[:20000]

    # Pass 8 input: trigger pages (falls back to waterfall text)
    trigger_text = get_best_pages_text(pages, scores, "loss_allocation",
                                        window=8, max_chars=16000, top_n=4)
    if not trigger_text:
        trigger_text = wf_text_1[:16000]

    # === GROUP B: Pass 6 (fees) + Pass 7 (waterfall) + Pass 8 (triggers) in parallel ===
    await _progress("Extracting fees + waterfall + triggers (parallel)", 48)
    _grp_b_t0 = time.perf_counter()
    fees_raw, waterfall_raw, loss_triggers_raw = await asyncio.gather(
        _call_llm(
            llm,
            "You are an expert ABS/MBS analyst. Extract ALL fees. Return ONLY a JSON array.",
            FEES_PROMPT.replace("{text}", fee_text),
            expect_list=True,
        ),
        _call_llm(
            llm,
            "You are an expert ABS/MBS analyst. Extract the complete Priority of Payments. Follow numbered steps exactly.",
            WATERFALL_PROMPT.replace("{text}", wf_text_1),
        ),
        _call_llm(
            llm,
            "You are an expert ABS/MBS analyst. Extract loss allocation, trigger tests, and reserve accounts.",
            TRIGGERS_PROMPT.replace("{text}", trigger_text),
        ),
    )
    logger.info(f"[Group B] wall-clock {time.perf_counter() - _grp_b_t0:.1f}s")

    # Pass 6 post-processing: normalize shape
    if isinstance(fees_raw, dict):
        for key in ("fees", "expenses", "fee_schedule"):
            if isinstance(fees_raw.get(key), list):
                fees_raw = fees_raw[key]
                break
    if not isinstance(fees_raw, list):
        fees_raw = []
    logger.info(f"Extracted {len(fees_raw)} fee entries")

    # Pass 7 post-processing: filter hallucinated class names from waterfall steps
    _valid_class_names = {r.get("class_name") for r in classes_raw if r.get("class_name")}
    for wf_key in ("interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall"):
        raw_steps = waterfall_raw.get(wf_key) or []
        waterfall_raw[wf_key] = _filter_waterfall_steps_by_classes(raw_steps, _valid_class_names)

    logger.info(
        f"Extracted waterfall (after class filter): "
        f"{len(waterfall_raw.get('interest_waterfall', []))} interest, "
        f"{len(waterfall_raw.get('principal_waterfall', []))} principal, "
        f"{len(waterfall_raw.get('excess_cashflow_waterfall', []))} excess steps"
    )

    # === PASS 9: Validation — re-query for critical missing fields ===
    await _progress("Validation pass — re-querying missing fields", 80)
    missing = []
    if not deal_info.get("cut_off_date"):
        missing.append("cut_off_date: The date the mortgage pool was valued (usually 'Cut-off Date: Month Day, Year')")
    if not deal_info.get("closing_date"):
        missing.append("closing_date: The date the deal closed/settled")
    if not deal_info.get("first_payment_date"):
        missing.append("first_payment_date: First distribution/payment date")
    if not deal_info.get("original_pool_balance"):
        missing.append("original_pool_balance: Total aggregate principal balance of the mortgage pool (dollar amount)")
    classes_with_zero = [r.get("class_name") for r in classes_raw
                         if _safe_float(r.get("initial_principal")) in (None, 0.0)
                         and not r.get("is_residual") and r.get("interest_rate_type") != "excess_cashflow"]
    if classes_with_zero:
        missing.append(f"initial_principal for classes: {classes_with_zero} — find dollar amounts in the certificate table")
    classes_floating_no_margin = [r.get("class_name") for r in classes_raw
                                   if r.get("interest_rate_type") == "floating" and not r.get("margin")]
    if classes_floating_no_margin:
        missing.append(f"margin (SOFR spread) for floating classes: {classes_floating_no_margin} — look for 'SOFR + X basis points'")

    if missing:
        logger.info(f"Validation: re-querying for {len(missing)} missing fields")
        # Broader search — scan pages 1-50 and top scored pages
        val_pages = set(range(1, min(51, total_pages + 1)))
        for section in ("certificate_table", "deal_summary", "fees_expenses"):
            for pnum, _ in scores.get(section, [])[:5]:
                for offset in range(-2, 6):
                    val_pages.add(pnum + offset)
        val_text = "\n\n".join(
            f"=== PAGE {p} ===\n{pages[p-1][1]}"
            for p in sorted(val_pages) if 1 <= p <= total_pages
        )[:22000]

        val_result = await _call_llm(
            llm,
            "You are an expert ABS/MBS analyst doing a validation pass. Extract ONLY the specifically requested fields. Return JSON.",
            VALIDATION_PROMPT
                .replace("{missing_fields}", "\n".join(f"- {m}" for m in missing))
                .replace("{text}", val_text),
        )

        if isinstance(val_result, dict):
            # Fill in missing deal_info fields
            for field in ("cut_off_date", "closing_date", "first_payment_date",
                           "original_pool_balance", "pricing_date", "legal_maturity_date"):
                if not deal_info.get(field) and val_result.get(field):
                    deal_info[field] = val_result[field]
                    logger.info(f"  Validation filled: {field} = {val_result[field]}")

            # Fill in missing class principals and margins
            for key in ("classes", "class_updates", "certificate_classes"):
                updated = val_result.get(key)
                if isinstance(updated, list):
                    update_map = {r.get("class_name"): r for r in updated if r.get("class_name")}
                    for i, raw_cls in enumerate(classes_raw):
                        cname = raw_cls.get("class_name")
                        if cname in update_map:
                            upd = update_map[cname]
                            if upd.get("initial_principal"):
                                classes_raw[i]["initial_principal"] = upd["initial_principal"]
                                logger.info(f"  Validation filled: {cname}.initial_principal = {upd['initial_principal']}")
                            if upd.get("margin"):
                                classes_raw[i]["margin"] = upd["margin"]
                    break

    # === PASS 10: Generate math formulas for waterfall steps ===
    await _progress("Generating waterfall computation formulas", 88)
    all_wf_steps_raw = (
        (waterfall_raw.get("interest_waterfall") or []) +
        (waterfall_raw.get("principal_waterfall") or []) +
        (waterfall_raw.get("excess_cashflow_waterfall") or [])
    )
    if all_wf_steps_raw:
        formula_map = await _generate_step_formulas(llm, all_wf_steps_raw)
        if formula_map:
            # Apply formulas per-bucket (step numbers restart per bucket)
            offset = 0
            for wf_key in ("interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall"):
                bucket = waterfall_raw.get(wf_key) or []
                waterfall_raw[wf_key] = _apply_formulas_to_steps(bucket, formula_map)

    # === PASS 11: Assemble DealConfig ===
    await _progress("Assembling deal configuration", 92)

    # Build servicers
    servicers = []
    for svc in (deal_info.get("servicers") or []):
        if isinstance(svc, dict) and svc.get("servicer_name"):
            servicers.append(ServicerConfig(
                servicer_name=str(svc["servicer_name"]),
                servicing_fee_rate=_safe_float(svc.get("servicing_fee_rate")) or 0.0025,
                advance_obligation=bool(svc.get("advance_obligation", False)),
                portfolio_pct=_safe_float(svc.get("portfolio_pct")),
            ))

    # Build classes
    classes = [c for c in (_build_certificate_class(r) for r in classes_raw) if c]

    # Assign default interest/principal priorities if not extracted
    if classes and all(c.interest_priority == 0 for c in classes):
        type_order = {"senior": 1, "mezzanine": 50, "subordinate": 100,
                      "io": 200, "exchangeable": 210, "residual": 220}
        class_counters: Dict[str, int] = {}
        for cls in classes:
            t = cls.type.lower()
            base = type_order.get(t, 150)
            class_counters[t] = class_counters.get(t, 0) + 1
            cls.interest_priority = base + class_counters[t]
            cls.principal_priority = base + class_counters[t]

    # Build fees
    fees = [f for f in (_build_fee_config(r) for r in fees_raw) if f]

    # Build waterfall
    interest_wf = _build_steps(waterfall_raw.get("interest_waterfall", []))
    principal_wf = _build_steps(waterfall_raw.get("principal_waterfall", []))
    excess_wf    = _build_steps(waterfall_raw.get("excess_cashflow_waterfall", []))

    # Build triggers
    triggers = []
    for t in (loss_triggers_raw.get("triggers") or []):
        try:
            triggers.append(TriggerTest(
                test_name=t.get("test_name", ""),
                test_type=t.get("test_type", "other"),
                description=t.get("description", ""),
                threshold=float(t.get("threshold") or 0.0),
                operator=t.get("operator", "greater_than"),
                trigger_on_failure=t.get("trigger_on_failure"),
            ))
        except Exception:
            pass

    # Build reserve accounts
    reserve_accounts = []
    for r in (loss_triggers_raw.get("reserve_accounts") or []):
        try:
            reserve_accounts.append(ReserveAccount(
                account_name=r.get("account_name", ""),
                initial_balance=float(r.get("initial_balance") or 0.0),
                target_amount=_safe_float(r.get("target_amount")),
                funded_from=r.get("funded_from"),
                released_to=r.get("released_to"),
            ))
        except Exception:
            pass

    # Loss allocation
    loss_order = loss_triggers_raw.get("loss_allocation_order") or []
    if not loss_order and classes:
        sub_classes = [c.class_name for c in reversed(classes) if "subordinate" in c.type.lower()]
        mez_classes = [c.class_name for c in reversed(classes) if "mezzanine" in c.type.lower()]
        loss_order = sub_classes + mez_classes

    import datetime
    now = datetime.datetime.utcnow().isoformat()

    deal_config = DealConfig(
        deal_id=deal_id,
        deal_name=deal_info.get("deal_name") or deal_id,
        issuing_entity=deal_info.get("issuing_entity") or "",
        series=deal_info.get("series"),
        depositor=deal_info.get("depositor"),
        sponsors=[s for s in (deal_info.get("sponsors") or []) if s],
        servicers=servicers,
        originators=[o for o in (deal_info.get("originators") or []) if o],
        custodian=deal_info.get("custodian"),
        securities_administrator=deal_info.get("securities_administrator"),
        owner_trustee=deal_info.get("owner_trustee"),
        underwriters=[u for u in (deal_info.get("underwriters") or []) if u],
        rating_agencies=[r for r in (deal_info.get("rating_agencies") or []) if r],
        closing_date=deal_info.get("closing_date"),
        cut_off_date=deal_info.get("cut_off_date"),
        first_payment_date=deal_info.get("first_payment_date"),
        legal_maturity_date=deal_info.get("legal_maturity_date"),
        pricing_date=deal_info.get("pricing_date"),
        asset_class=deal_info.get("asset_class") or "Residential Real Estate",
        asset_type=deal_info.get("asset_type") or "HELOC",
        payment_frequency=deal_info.get("payment_frequency") or "Monthly",
        original_pool_balance=_safe_float(deal_info.get("original_pool_balance")) or 0.0,
        lien_position=deal_info.get("lien_position"),
        revolving_period=bool(deal_info.get("revolving_period", False)),
        revolving_period_end_date=deal_info.get("revolving_period_end_date"),
        benchmark=deal_info.get("benchmark") or "SOFR",
        benchmark_tenor=deal_info.get("benchmark_tenor") or "1M",
        interest_day_count=deal_info.get("interest_day_count") or "actual/360",
        cleanup_call_pct=_safe_float(deal_info.get("cleanup_call_pct")) or 0.10,
        classes=classes,
        fees=fees,
        interest_waterfall=interest_wf,
        principal_waterfall=principal_wf,
        excess_cashflow_waterfall=excess_wf,
        triggers=triggers,
        reserve_accounts=reserve_accounts,
        loss_allocation_order=loss_order,
        extraction_source="llm_extracted",
        extraction_confidence=0.85 if all([
            deal_info.get("cut_off_date"),
            classes and any(c.initial_principal > 0 for c in classes),
            fees,
        ]) else 0.60,
        raw_extraction={
            "deal_info": deal_info,
            "classes": classes_raw,
            "fees": fees_raw,
            "waterfall": waterfall_raw,
            "loss_triggers": loss_triggers_raw,
            "regex_found": regex_data,
        },
        section_page_map=_build_section_page_map(scores, total_pages),
        created_at=now,
        updated_at=now,
    )

    await _progress("Extraction complete", 100)
    logger.info(
        f"Extraction complete: {len(classes)} classes, {len(fees)} fees, "
        f"{len(interest_wf)} interest steps, {len(principal_wf)} principal steps, "
        f"confidence={deal_config.extraction_confidence}"
    )
    return deal_config
