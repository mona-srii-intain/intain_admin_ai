"""
LLM Agent + Smart PDF Extraction Pipeline for ABS/MBS deal indentures.

PDF extraction (Pass 1): PyMuPDF + Gemini Vision for scanned/pseudo-table pages.
Deal mapping (Passes 2+): regex pre-scan, pdfplumber cert table, Azure GPT retrieval.

INSTALL: pip install pymupdf pillow google-genai pandas tabulate pdfplumber
"""

from __future__ import annotations

import asyncio
import fitz
import pdfplumber
import itertools
import json
import logging
import os
import re
import threading
import time
import pandas as pd

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from dotenv import load_dotenv
from langchain_openai import AzureChatOpenAI
from langchain_core.messages import HumanMessage, SystemMessage
from google import genai

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

try:
    from models.deal import (
        CertificateClass,
        DealConfig,
        FeeConfig,
        ServicerConfig,
        TriggerTest,
        WaterfallStep,
        ReserveAccount,
    )
    _MODELS_AVAILABLE = True
except ImportError:
    _MODELS_AVAILABLE = False

logger = logging.getLogger("uvicorn.error")

# =====================================================
# CONFIG
# =====================================================

PDF_PATH = "/aianalytics/Vishal/Pdf/JPMMT 2023-HE1 - [AS PRINTED] Final Private Placement Memorandum.pdf"

GEMINI_API_KEY = (
    os.getenv("GEMINI_API_KEY", "").strip()
    or os.getenv("GOOGLE_API_KEY", "").strip()
)
GEMINI_MODEL_NAME = os.getenv("GEMINI_MODEL_NAME", "gemini-2.5-flash").strip()

OUTPUT_JSON = "/aianalytics/Vishal/Pdf/extracted_output.json"

MAX_WORKERS = 5

GEMINI_MAX_RETRIES = 5

GEMINI_RETRY_SLEEP = 8

# =====================================================
# GEMINI CLIENT
# =====================================================

# One client per thread — extract_pdf runs process_page in a ThreadPoolExecutor;
# creating a new Client per request races the underlying httpx pool and yields
# "Cannot send a request, as the client has been closed".
_gemini_client_local = threading.local()


def _get_gemini_client() -> genai.Client:
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "GEMINI_API_KEY (or GOOGLE_API_KEY) is not set. "
            "Add it to .env for PDF vision extraction."
        )
    client = getattr(_gemini_client_local, "client", None)
    if client is None:
        _gemini_client_local.client = genai.Client(api_key=GEMINI_API_KEY)
        client = _gemini_client_local.client
    return client


# =====================================================
# DOCUMENT AI LAYOUT PARSER (Pass-1 pseudo-table replacement)
# =====================================================
#
# Why DocAI instead of Gemini Vision for pseudo-tables:
#   • Structural detection using bounding-box spatial analysis — no hallucination
#   • One batch call per 15-page chunk vs one Gemini call per page
#   • Latency: ~70 pseudo pages → 5 parallel DocAI chunks (~15s) vs ~150s Gemini sequential
#   • Preserves row/column cell structure as proper markdown tables
#
# Credentials: service-account JSON in the same folder as this file.
# Processor: auto-discovered (or auto-created) at startup.
# =====================================================

_DOCAI_CREDENTIALS_PATH = Path(__file__).parent / "credentials.json"
_DOCAI_PROJECT_ID = "burnished-treat-275605"
_DOCAI_LOCATION = os.getenv("DOCAI_LOCATION", "us")
# Override via env-var; empty → auto-discover on first use
_DOCAI_PROCESSOR_ID = os.getenv("DOCAI_PROCESSOR_ID", "").strip()
# v1.5-2025-08-25 = Gemini-powered RC — visually detects tables (even space-aligned pseudo-tables)
# v1.0-2024-06-03 = GA stable — only reads text layer, misses pseudo-tables
_DOCAI_PROCESSOR_VERSION = os.getenv(
    "DOCAI_PROCESSOR_VERSION",
    "pretrained-layout-parser-v1.5-2025-08-25",
)
# v1.5 online limit = 15 pages per request; use 14 to stay within limit
_DOCAI_PAGE_CHUNK = 14
_docai_processor_lock = threading.Lock()


def _get_docai_client():
    """Return a DocumentProcessorServiceClient authenticated via service-account JSON."""
    from google.cloud import documentai
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        str(_DOCAI_CREDENTIALS_PATH),
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    return documentai.DocumentProcessorServiceClient(
        client_options={"api_endpoint": f"{_DOCAI_LOCATION}-documentai.googleapis.com"},
        credentials=creds,
    )


def _ensure_docai_processor_id() -> str:
    """Return the Layout Parser processor ID, discovering or creating it if needed."""
    global _DOCAI_PROCESSOR_ID
    if _DOCAI_PROCESSOR_ID:
        return _DOCAI_PROCESSOR_ID
    with _docai_processor_lock:
        if _DOCAI_PROCESSOR_ID:
            return _DOCAI_PROCESSOR_ID
        try:
            from google.cloud import documentai
            client = _get_docai_client()
            parent = f"projects/{_DOCAI_PROJECT_ID}/locations/{_DOCAI_LOCATION}"
            for p in client.list_processors(parent=parent):
                if "LAYOUT_PARSER" in (p.type_ or ""):
                    _DOCAI_PROCESSOR_ID = p.name.split("/")[-1]
                    logger.info(f"DocAI: found existing Layout Parser processor {_DOCAI_PROCESSOR_ID}")
                    return _DOCAI_PROCESSOR_ID
            # None found — create one
            proc = client.create_processor(
                parent=parent,
                processor=documentai.Processor(
                    display_name="intain-admin-layout-parser",
                    type_="LAYOUT_PARSER_PROCESSOR",
                ),
            )
            _DOCAI_PROCESSOR_ID = proc.name.split("/")[-1]
            logger.info(f"DocAI: created Layout Parser processor {_DOCAI_PROCESSOR_ID}")
            return _DOCAI_PROCESSOR_ID
        except Exception as e:
            logger.warning(f"DocAI processor discovery failed: {e}")
            return ""


def _docai_tables_to_markdown(page, doc_text: str) -> List[str]:
    """Convert DocumentAI Page.tables to a list of markdown strings."""
    results = []
    for table in page.tables:
        def _cell_text(text_anchor) -> str:
            if not text_anchor or not text_anchor.text_segments:
                return ""
            return "".join(
                doc_text[s.start_index:s.end_index]
                for s in text_anchor.text_segments
            ).strip().replace("\n", " ")

        header_rows = [
            [_cell_text(c.layout.text_anchor) for c in row.cells]
            for row in table.header_rows
        ]
        body_rows = [
            [_cell_text(c.layout.text_anchor) for c in row.cells]
            for row in table.body_rows
        ]
        if not header_rows and not body_rows:
            continue
        try:
            cols = header_rows[0] if header_rows else None
            df = pd.DataFrame(body_rows, columns=cols)
            md = df.to_markdown(index=False)
            if md:
                results.append(md)
        except Exception:
            pass
    return results


def _docai_layout_blocks_to_page_tables(document) -> Dict[int, List[str]]:
    """
    Parse document.document_layout.blocks recursively — v1.5 Gemini Layout Parser format.
    Tables are nested inside text_block.blocks (under section headings), not at the top level.
    Cell text lives in cell.blocks[0].text_block.text (NOT cell.text).
    """
    page_tables: Dict[int, List[str]] = {}
    layout = getattr(document, "document_layout", None)
    if not layout:
        return page_tables

    def _cell_text(cell) -> str:
        """Extract text from a LayoutTableCell whose content is in cell.blocks[i].text_block.text"""
        parts = []
        for blk in getattr(cell, "blocks", []):
            tb = getattr(blk, "text_block", None)
            if tb and getattr(tb, "text", ""):
                parts.append(tb.text.strip().replace("\n", " "))
        return " ".join(parts)

    def _process(block, inherited_page: Optional[int]) -> None:
        page_span = getattr(block, "page_span", None)
        page_num = (getattr(page_span, "page_start", None) if page_span else None) or inherited_page

        table_block = getattr(block, "table_block", None)
        if table_block and (table_block.header_rows or table_block.body_rows):
            header_rows = [
                [_cell_text(c) for c in row.cells]
                for row in getattr(table_block, "header_rows", [])
            ]
            body_rows = [
                [_cell_text(c) for c in row.cells]
                for row in getattr(table_block, "body_rows", [])
            ]
            if (header_rows or body_rows) and page_num:
                try:
                    cols = header_rows[0] if header_rows else None
                    if cols and body_rows:
                        n = len(cols)
                        body_rows = [(r + [""] * n)[:n] for r in body_rows]
                    df = pd.DataFrame(body_rows, columns=cols)
                    md = df.to_markdown(index=False)
                    if md:
                        page_tables.setdefault(page_num, []).append(md)
                except Exception:
                    pass

        text_block = getattr(block, "text_block", None)
        if text_block:
            for nested in getattr(text_block, "blocks", []):
                _process(nested, page_num)

    for block in getattr(layout, "blocks", []):
        _process(block, None)

    return page_tables


def _process_docai_chunk(
    sub_pdf_bytes: bytes,
    page_map: Dict[int, int],   # sub-PDF page index (1-based) → orig page number
) -> Dict[int, List[str]]:
    """Process one sub-PDF chunk with DocAI. Returns {orig_page_num: [markdown_tables]}.

    Handles both response formats:
      • v1.0 stable  → document.pages[i].tables  (header_rows/body_rows + text anchors)
      • v1.5 Gemini  → document.document_layout.blocks  (table_block with direct .text)
    """
    try:
        from google.cloud import documentai
        proc_id = _ensure_docai_processor_id()
        if not proc_id:
            return {}
        client = _get_docai_client()
        name = (
            f"projects/{_DOCAI_PROJECT_ID}/locations/{_DOCAI_LOCATION}"
            f"/processors/{proc_id}"
            f"/processorVersions/{_DOCAI_PROCESSOR_VERSION}"
        )
        request = documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(
                content=sub_pdf_bytes,
                mime_type="application/pdf",
            ),
            # No layout_config needed for v1.0 stable; table detection is on by default.
            # If using v1.5 Gemini version, add:
            #   process_options=documentai.ProcessOptions(
            #       layout_config=documentai.ProcessOptions.LayoutConfig(enable_table_annotation=True)
            #   )
        )
        result = client.process_document(request=request)
        document = result.document
        doc_text = document.text

        # --- Path A: v1.5 Gemini — document_layout.blocks (document.pages is EMPTY in v1.5) ---
        layout_page_tables = _docai_layout_blocks_to_page_tables(document)

        out: Dict[int, List[str]] = {}

        # v1.5: document.pages is empty; map directly from layout_page_tables sub-page → orig
        if layout_page_tables:
            for sub_page_num, tables in layout_page_tables.items():
                orig = page_map.get(sub_page_num)
                if orig and tables:
                    out[orig] = tables

        # --- Path B: v1.0 stable — document.pages[i].tables (text anchors) ---
        for page in document.pages:
            orig = page_map.get(page.page_number)
            if orig is None or orig in out:
                continue
            tables = _docai_tables_to_markdown(page, doc_text)
            if tables:
                out[orig] = tables

        return out
    except Exception as e:
        logger.warning(f"DocAI chunk processing error: {e}")
        return {}


def docai_extract_pseudo_pages(
    doc,                        # fitz.Document (open)
    pseudo_page_nums: List[int],  # 1-indexed original page numbers
) -> Dict[int, List[str]]:
    """
    Send pseudo-table pages to Document AI Layout Parser (v1.5 Gemini) in 14-page chunks.
    v1.5 online limit = 15 pages; chunks run in parallel threads.
    Returns {orig_page_num: [markdown_table_str, ...]} for pages where tables were found.
    """
    if not pseudo_page_nums or not _DOCAI_CREDENTIALS_PATH.exists():
        return {}

    # Build per-chunk sub-PDFs + page maps
    chunks_data: List[tuple] = []
    sorted_pages = sorted(pseudo_page_nums)
    for i in range(0, len(sorted_pages), _DOCAI_PAGE_CHUNK):
        chunk_pages = sorted_pages[i: i + _DOCAI_PAGE_CHUNK]
        sub_doc = fitz.open()
        page_map: Dict[int, int] = {}
        for sub_idx, orig_num in enumerate(chunk_pages, start=1):
            sub_doc.insert_pdf(doc, from_page=orig_num - 1, to_page=orig_num - 1)
            page_map[sub_idx] = orig_num
        chunks_data.append((sub_doc.tobytes(), page_map))

    result: Dict[int, List[str]] = {}
    with ThreadPoolExecutor(max_workers=min(len(chunks_data), 8)) as ex:
        futures = [ex.submit(_process_docai_chunk, b, m) for b, m in chunks_data]
        for fut in futures:
            try:
                result.update(fut.result())
            except Exception as e:
                logger.warning(f"DocAI chunk future error: {e}")

    logger.info(
        f"DocAI Layout Parser: tables found on {len(result)}/{len(pseudo_page_nums)} pseudo pages"
    )
    return result


# =====================================================
# AZURE OPENAI — RETRIEVAL LLM (GPT-5.5)
# =====================================================

def _get_retrieval_llm() -> AzureChatOpenAI:
    """Returns the high-accuracy LLM used for structured extraction (GPT-5.5).
    Uses a dedicated endpoint/key separate from the GPT-4.1 chat deployment.
    """
    return AzureChatOpenAI(
        azure_endpoint=os.getenv("AZURE_OPENAI_RETRIEVAL_ENDPOINT", "").strip(),
        api_key=os.getenv("AZURE_OPENAI_RETRIEVAL_API_KEY", "").strip(),
        api_version=os.getenv("AZURE_OPENAI_RETRIEVAL_API_VERSION", "2024-02-15-preview").strip(),
        azure_deployment=os.getenv("AZURE_OPENAI_RETRIEVAL_DEPLOYMENT_NAME", "gpt-5.5").strip(),
        max_tokens=8192,
    )


# =====================================================
# FAST TEXT EXTRACTION
# =====================================================

def extract_fast_text(page):

    try:
        return page.get_text("text")

    except Exception:
        return ""

# =====================================================
# TABLE QUALITY CHECK
# =====================================================

def table_quality_good(table_data):

    if not table_data:
        return False

    total_cells = 0
    empty_cells = 0

    for row in table_data:

        for cell in row:

            total_cells += 1

            if cell is None or str(cell).strip() == "":
                empty_cells += 1

    if total_cells == 0:
        return False

    empty_ratio = empty_cells / total_cells

    # too many empty cells
    if empty_ratio > 0.4:
        return False

    # tiny tables
    if len(table_data) < 2:
        return False

    return True

# =====================================================
# PSEUDO TABLE DETECTION
# =====================================================
#
# Two-stage design:
#
# 1. compute_page_features(page)
#    Per-page signals (pseudo flag, dominant column
#    x-positions, edge-row signals).
#
# 2. apply_buffer_continuation(features)
#    Buffer / neighbour rule that catches tables
#    spanning multiple pages: a non-pseudo page is
#    promoted to pseudo if a neighbour is pseudo and
#    this page's adjacent edge looks like a continuation
#    of the same column layout.
# =====================================================

EDGE_SAMPLE_LINES = 5
COLUMN_OVERLAP_MIN = 2


def _column_signature(words):

    if not words:
        return Counter()

    x_positions = [
        round(w[0] / 10) * 10
        for w in words
    ]

    return Counter(x_positions)


def _looks_tabular(sample_lines):

    if not sample_lines:
        return False

    short = sum(
        1 for l in sample_lines
        if len(l.split()) <= 6
    )

    numeric = sum(
        1 for l in sample_lines
        if re.search(r"\d", l)
    )

    return (
        short / len(sample_lines) >= 0.5
        and numeric >= 1
    )


def compute_page_features(page):

    empty = {
        "pseudo": False,
        "columns": set(),
        "starts_mid_row": False,
        "ends_mid_row": False,
        "num_lines": 0,
    }

    try:
        text = page.get_text("text")
        words = page.get_text("words")
    except Exception:
        return empty

    lines = [
        l.strip()
        for l in text.split("\n")
        if l.strip()
    ]

    if not lines or not words:
        empty["num_lines"] = len(lines)
        return empty

    # -----------------------------------------
    # ratios
    # -----------------------------------------

    short_lines = sum(
        1 for l in lines
        if len(l.split()) <= 6
    )

    short_ratio = short_lines / len(lines)

    numeric_lines = sum(
        1 for l in lines
        if re.search(r"\d", l)
    )

    numeric_ratio = numeric_lines / len(lines)

    spacing_lines = sum(
        1 for l in lines
        if "   " in l or "\t" in l
    )

    spacing_ratio = spacing_lines / len(lines)

    # -----------------------------------------
    # x-coordinate alignment
    # -----------------------------------------

    freq = _column_signature(words)

    aligned_columns = {
        x
        for x, v in freq.items()
        if v > 15
    }

    # -----------------------------------------
    # single-page pseudo-table heuristic
    # -----------------------------------------

    pseudo = (
        len(lines) >= 5
        and short_ratio > 0.45
        and (
            numeric_ratio > 0.20
            or spacing_ratio > 0.25
            or len(aligned_columns) >= 3
        )
    )

    # -----------------------------------------
    # edge-row signals for continuation
    # -----------------------------------------

    top = lines[:EDGE_SAMPLE_LINES]
    bottom = lines[-EDGE_SAMPLE_LINES:]

    starts_mid_row = (
        _looks_tabular(top)
        and len(aligned_columns) >= 2
    )

    ends_mid_row = (
        _looks_tabular(bottom)
        and len(aligned_columns) >= 2
    )

    return {
        "pseudo": pseudo,
        "columns": aligned_columns,
        "starts_mid_row": starts_mid_row,
        "ends_mid_row": ends_mid_row,
        "num_lines": len(lines),
    }


def apply_buffer_continuation(features):

    n = len(features)

    extended = [f["pseudo"] for f in features]

    for i in range(n):

        if features[i]["pseudo"]:
            continue

        cur_cols = features[i]["columns"]

        # ----- previous neighbour -----
        if i > 0 and features[i - 1]["pseudo"]:

            overlap = len(
                cur_cols & features[i - 1]["columns"]
            )

            if (
                features[i]["starts_mid_row"]
                and overlap >= COLUMN_OVERLAP_MIN
            ):
                extended[i] = True
                continue

        # ----- next neighbour -----
        if i < n - 1 and features[i + 1]["pseudo"]:

            overlap = len(
                cur_cols & features[i + 1]["columns"]
            )

            if (
                features[i]["ends_mid_row"]
                and overlap >= COLUMN_OVERLAP_MIN
            ):
                extended[i] = True

    return extended

# =====================================================
# REAL TABLE EXTRACTION
# =====================================================

def extract_tables(page):

    extracted_tables = []

    try:

        tables = page.find_tables()

        # no tables
        if not tables.tables:
            return []

        for table in tables.tables:

            try:

                data = table.extract()

                # bad extraction
                if not table_quality_good(data):
                    return None

                df = pd.DataFrame(data)

                markdown = df.to_markdown(index=False)

                extracted_tables.append(markdown)

            except Exception:

                return None

        return extracted_tables

    except Exception:

        return []

# =====================================================
# PAGE PROCESSING
# =====================================================

def process_page(
    page_num,
    page,
    is_pseudo=False,
    is_continuation=False,
    docai_cache: Optional[Dict[int, List[str]]] = None,
):
    """
    Extract content from a single PDF page.

    Pass-1 strategy (in priority order):
      1. DocAI Layout Parser — if pre-computed tables exist in docai_cache (fast, structured)
      2. PyMuPDF table extraction — for pages with real PDF grid tables
      3. PyMuPDF fast-text — for plain-text pages
      4. Gemini Vision — fallback only when DocAI found no tables on a pseudo/broken page
    """
    print(f"PAGE {page_num}")

    result = {
        "page": page_num,
        "method": "",
        "content": ""
    }

    # -------------------------------------------------
    # FAST TEXT EXTRACTION (always run — used as prefix)
    # -------------------------------------------------

    text = extract_fast_text(page)

    # -------------------------------------------------
    # PSEUDO TABLE / CONTINUATION
    # Prefer DocAI over Gemini Vision; fall back to Gemini only if DocAI
    # found no tables on this page.
    # -------------------------------------------------

    if is_pseudo:
        docai_tables = (docai_cache or {}).get(page_num, [])
        if docai_tables:
            combined = text + "\n\n" + "\n\n".join(docai_tables)
        else:
            # DocAI found no structured tables on this page — use fast text only.
            # (Was: Gemini Vision fallback. Removed; DocAI is the sole visual extractor.)
            combined = text
        result["method"] = (
            "docai_pseudo_table_continuation"
            if is_continuation
            else "docai_pseudo_table"
        )
        result["content"] = combined
        return result

    # -------------------------------------------------
    # REAL TABLE EXTRACTION (PyMuPDF pdfplumber grid)
    # -------------------------------------------------

    tables = extract_tables(page)

    # -------------------------------------------------
    # CASE 1 — NO TABLES FOUND
    # -------------------------------------------------

    if tables == []:
        result["method"] = "fast_text"
        result["content"] = text
        return result

    # -------------------------------------------------
    # CASE 2 — GOOD TABLE EXTRACTION (PyMuPDF)
    # -------------------------------------------------

    elif tables is not None:
        combined = text + "\n\n" + "\n\n".join(tables)
        result["method"] = "pymupdf_tables"
        result["content"] = combined
        return result

    # -------------------------------------------------
    # CASE 3 — TABLE EXTRACTION FAILED
    # Try DocAI first; fall back to Gemini only if DocAI has nothing.
    # -------------------------------------------------

    else:
        docai_tables = (docai_cache or {}).get(page_num, [])
        if docai_tables:
            combined = text + "\n\n" + "\n\n".join(docai_tables)
            result["method"] = "docai_table"
            result["content"] = combined
            return result

        # DocAI found no tables and PyMuPDF failed — use fast text only.
        result["method"] = "fast_text"
        result["content"] = text
        return result

# =====================================================
# PDF EXTRACTION
# =====================================================

def extract_pdf(pdf_path: Optional[str] = None):

    _path = pdf_path or PDF_PATH
    # Derive output JSON path from input so API calls never clobber the dev default
    _output = (
        _path.rsplit(".pdf", 1)[0] + "_extracted.json"
        if pdf_path else OUTPUT_JSON
    )

    doc = fitz.open(_path)

    # -------------------------------------------------
    # PASS 1
    # per-page features (sequential, fitz-safe)
    # -------------------------------------------------

    print("COMPUTING PAGE FEATURES")

    features = [
        compute_page_features(doc[i])
        for i in range(len(doc))
    ]

    # -------------------------------------------------
    # PASS 2
    # buffer rule for cross-page table continuation
    # -------------------------------------------------

    pseudo_flags = apply_buffer_continuation(features)

    continuation_count = sum(
        1
        for i, p in enumerate(pseudo_flags)
        if p and not features[i]["pseudo"]
    )

    print(
        f"PSEUDO PAGES: {sum(pseudo_flags)} "
        f"(CONTINUATION: {continuation_count})"
    )

    # -------------------------------------------------
    # PASS 2.5 — Document AI Layout Parser pre-pass
    # Send ALL pseudo-table pages to DocAI in one shot
    # (chunked into 14-page groups, processed in parallel).
    # This replaces the majority of per-page Gemini Vision calls,
    # cutting latency by ~80% for pseudo-table heavy documents.
    # -------------------------------------------------

    pseudo_page_nums = [i + 1 for i, p in enumerate(pseudo_flags) if p]
    docai_cache: Dict[int, List[str]] = {}

    if pseudo_page_nums and _DOCAI_CREDENTIALS_PATH.exists():
        try:
            print(
                f"DOCAI PRE-PASS: processing {len(pseudo_page_nums)} pseudo pages "
                f"in {((len(pseudo_page_nums) - 1) // _DOCAI_PAGE_CHUNK) + 1} chunks..."
            )
            t_docai = time.time()
            docai_cache = docai_extract_pseudo_pages(doc, pseudo_page_nums)
            elapsed = time.time() - t_docai
            docai_hit = len(docai_cache)
            docai_miss = len(pseudo_page_nums) - docai_hit
            print(
                f"DOCAI PRE-PASS DONE in {elapsed:.1f}s  "
                f"(tables found: {docai_hit}, no-table pages (fast_text): {docai_miss})"
            )
        except Exception as exc:
            print(f"DOCAI PRE-PASS FAILED ({exc}) — Gemini Vision fallback for all pseudo pages")

    # -------------------------------------------------
    # PASS 3 — parallel page processing
    # Workers call process_page(); pseudo-table pages use
    # docai_cache first, Gemini only when DocAI found nothing.
    # Non-pseudo pages use PyMuPDF (unchanged).
    # -------------------------------------------------

    results = []

    with ThreadPoolExecutor(
        max_workers=max(MAX_WORKERS, 15)  # more workers now pseudo pages no longer bottleneck on Gemini
    ) as executor:

        futures = []

        for i in range(len(doc)):

            page = doc[i]

            is_pseudo = pseudo_flags[i]

            is_continuation = (
                is_pseudo
                and not features[i]["pseudo"]
            )

            futures.append(
                executor.submit(
                    process_page,
                    i + 1,
                    page,
                    is_pseudo,
                    is_continuation,
                    docai_cache,    # DocAI pre-computed tables (or {} if DocAI unavailable)
                )
            )

        for future in futures:

            try:

                results.append(
                    future.result()
                )

            except Exception as e:

                print("ERROR:", e)

    # sort pages
    results.sort(
        key=lambda x: x["page"]
    )

    # save output
    with open(
        _output,
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            results,
            f,
            indent=2,
            ensure_ascii=False
        )

    print("\nEXTRACTION COMPLETE")

    return results

# =====================================================
# LOAD EXTRACTION
# =====================================================

def load_extraction():

    with open(
        _output,
        "r",
        encoding="utf-8"
    ) as f:

        return json.load(f)

# =====================================================
# MAPPING LOGIC (ported from file.py)
# =====================================================

# Pass 1: each page uses exactly one extractor (PyMuPDF or DocAI). Labels for GPT 5.5.
_DOCAI_PASS1_METHOD_PREFIXES = ("docai_",)

_RETRIEVAL_PASS1_FORMAT_SYSTEM = (
    "Page headers show [pass1: pymupdf_text] or [pass1: docai_table]. "
    "PyMuPDF = native PDF text (class name and $ amount may be on separate lines). "
    "DocAI = Document AI Layout Parser — clean structured markdown tables, very accurate. "
    "Parse BOTH formats; do not skip pages. "
)


def _methods_by_page(raw_results: Optional[List[Dict]]) -> Dict[int, str]:
    if not raw_results:
        return {}
    return {
        int(r["page"]): str(r.get("method") or "unknown")
        for r in raw_results
        if r.get("page") is not None
    }


def _format_extraction_method_label(method: str) -> str:
    if not method or method == "unknown":
        return "unknown"
    if method == "fast_text":
        return "pymupdf_text"
    if method == "pymupdf_tables":
        return "pymupdf_text+tables"
    if method.startswith("docai_pseudo"):
        return "docai_pseudo_table"
    if method.startswith("docai"):
        return "docai_table"
    return method


def _format_page_for_mapping(page_num: int, content: str, method: str = "") -> str:
    label = _format_extraction_method_label(method)
    return f"=== PAGE {page_num} [pass1: {label}] ===\n{content}"


def _join_pages_for_mapping(
    page_nums: List[int],
    pages: List[Tuple[int, str]],
    methods: Dict[int, str],
) -> str:
    page_dict = {p: t for p, t in pages}
    parts = []
    for p in sorted(page_nums):
        if p in page_dict:
            parts.append(_format_page_for_mapping(p, page_dict[p], methods.get(p, "")))
    return "\n\n".join(parts)


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

    # ── Step 7: Servicer extraction ───────────────────────────────────────
    # "XYZ LLC will service HELOCs related to approximately 60.53% of the Mortgage Loans"
    _svc_company_pat = re.compile(
        r'(?:^|[\n.])\s*'
        r'([A-Z][^\n."(]{2,60}?(?:LLC|Inc\.?|Corp\.?|L\.P\.|LLP|FSB|NA|N\.A\.|Bank|Trust))'
        r'(?:\s+\([^)]{1,30}\))?\s+will\s+service\s+HELOCs?\s+related\s+to\s+approximately\s+([\d.]+)\s*%',
        re.IGNORECASE | re.MULTILINE,
    )
    # "loanDepot will own the mortgage servicing rights for HELOCs related to approximately 39.47%"
    _svc_rights_pat = re.compile(
        r'(?:^|[.]\s+)((?:[A-Z][\w]*(?:\.com)?(?:,?\s+[A-Z][\w]*(?:\.com)?){0,4}))\s+'
        r'will\s+own\s+the\s+mortgage\s+servicing\s+rights\s+for\s+'
        r'HELOCs?\s+related\s+to\s+approximately\s+([\d.]+)\s*%',
        re.IGNORECASE | re.MULTILINE,
    )
    # Advance obligation: false if the doc says servicer makes no advances
    _no_advance_pat = re.search(
        r'neither\s+the\s+(?:related\s+)?servicer\s+nor\s+any\s+other\s+party\s+'
        r'(?:participating|will\s+be\s+obligated)\s+to\s+make\s+any\s+(?:monthly\s+)?advances',
        full_text, re.IGNORECASE,
    )
    advance_obligation = not bool(_no_advance_pat)

    regex_servicers: List[Dict] = []
    seen_svc_names: set = set()
    for m in list(_svc_company_pat.finditer(full_text)) + list(_svc_rights_pat.finditer(full_text)):
        raw_name = re.sub(r'\s+', ' ', m.group(1)).strip().rstrip(',')
        pct = float(m.group(2)) / 100
        norm_name = re.sub(r'\W', '', raw_name).upper()
        if norm_name in seen_svc_names or len(raw_name) < 3 or len(raw_name) > 80:
            continue
        seen_svc_names.add(norm_name)
        # Fee rate: look for "X% per annum" or "X basis points" near servicer mention
        window_start = max(0, m.start() - 200)
        window = full_text[window_start: m.start() + 2000]
        fee_pct_m = re.search(r'(\d+\.\d+)\s*%\s+per\s+annum', window, re.IGNORECASE)
        fee_bps_m = re.search(r'(\d+)\s+basis\s+points\s+per\s+annum', window, re.IGNORECASE)
        if fee_pct_m:
            fee_rate = float(fee_pct_m.group(1)) / 100
        elif fee_bps_m:
            fee_rate = float(fee_bps_m.group(1)) / 10000
        else:
            fee_rate = 0.005  # default 50bps for HELOC
        regex_servicers.append({
            "servicer_name": raw_name,
            "servicing_fee_rate": round(fee_rate, 6),
            "advance_obligation": advance_obligation,
            "portfolio_pct": round(pct, 4),
        })
    if regex_servicers:
        found["regex_servicers"] = regex_servicers
        logger.info(f"Regex found servicers: {[s['servicer_name'] for s in regex_servicers]}")

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
        "order of priority", "following order of priority",
        "Class Principal Amount thereof is reduced to zero",
        "Class X Distribution Amount", "Cap Carryover Reserve Account",
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


def _find_priority_of_payments_pages(
    pages: List[Tuple[int, str]],
    max_pages: int = 12,
) -> List[int]:
    """
    Find pages that contain the actual numbered Priority of Distributions section.
    Many indentures use '(1) to the Class A-1' rather than 'first, to pay'.
    """
    ranked: List[Tuple[int, int]] = []
    for page_num, text in pages:
        tu = text.upper()
        has_numbered_steps = bool(re.search(
            r"\(\d+\)\s+(?:concurrently,\s+)?to\s+(?:the\s+)?(?:Class\s+)?[A-Z]",
            text,
            re.IGNORECASE,
        ))
        has_prose_steps = bool(re.search(
            r"(?:first|second|third|fourth|fifth),\s+to\s+(?:pay|the\s+(?:Class|Trustee|Securities))",
            text,
            re.IGNORECASE,
        ))
        score = 0
        if has_numbered_steps:
            score += 8
        if has_prose_steps:
            score += 6
        if "INTEREST REMITTANCE AMOUNT" in tu and "PRINCIPAL REMITTANCE AMOUNT" in tu:
            score += 3
        if "PRIORITY OF DISTRIBUTIONS" in tu or "ORDER OF PRIORITY" in tu:
            score += 2
        if "MONTHLY EXCESS CASHFLOW" in tu and re.search(r"\(\d+\)\s+to\s+", text):
            
            score += 2
        # Require actual steps — not just mention of keywords
        if (has_numbered_steps or has_prose_steps) and score >= 6:
            ranked.append((page_num, score))
    ranked.sort(key=lambda x: x[1], reverse=True)
    if not ranked:
        return []
    page_dict = {p: t for p, t in pages}
    chosen: List[int] = []
    seen: set = set()
    for pnum, _ in ranked[:3]:
        for pg in range(pnum - 3, pnum + 4):
            if pg in page_dict and pg not in seen:
                seen.add(pg)
                chosen.append(pg)
    return sorted(chosen)


def get_best_pages_text(
    pages: List[Tuple[int, str]],
    scores: Dict[str, List[Tuple[int, int]]],
    section: str,
    window: int = 8,
    max_chars: int = 18000,
    top_n: int = 5,
    methods_by_page: Optional[Dict[int, str]] = None,
) -> str:
    """
    Get combined text of the top-scored pages for a section,
    including a window of surrounding pages for context.
    Each page header includes Pass-1 extraction method (pymupdf vs gemini).
    Pages are added in score order (highest-ranked windows first), not lowest page number first.
    """
    methods_by_page = methods_by_page or {}
    page_dict = {pnum: text for pnum, text in pages}
    top_pages = [p for p, _ in scores.get(section, [])[:top_n]]
    if not top_pages:
        return ""

    combined = ""
    seen: set = set()
    for top_p in top_pages:
        for offset in range(-2, window + 1):
            pg = top_p + offset
            if pg not in page_dict or pg in seen:
                continue
            seen.add(pg)
            block = _format_page_for_mapping(
                pg, page_dict[pg], methods_by_page.get(pg, "")
            )
            if len(combined) + len(block) > max_chars:
                return combined[:max_chars]
            combined += f"\n\n{block}"
    return combined[:max_chars]


def _build_waterfall_text(
    pages: List[Tuple[int, str]],
    scores: Dict[str, List[Tuple[int, int]]],
    methods: Dict[int, str],
    max_chars: int = 22000,
) -> str:
    """Prefer pages with the numbered Priority of Distributions, then scored windows."""
    pom_pages = _find_priority_of_payments_pages(pages)
    if pom_pages:
        primary = _join_pages_for_mapping(pom_pages, pages, methods)
    else:
        primary = ""
    scored = get_best_pages_text(
        pages, scores, "priority_of_payments",
        window=12, max_chars=max_chars, top_n=4, methods_by_page=methods,
    )
    if primary and scored:
        return (primary + "\n\n" + scored)[:max_chars]
    return (primary or scored)[:max_chars]


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
- Cleanup call: Usually "Optional Redemption" or "Cleanup Call" at 10% of original balance
- Servicers: Look for "THE SERVICERS" section or a fee schedule table. Each servicer entry needs:
    servicer_name   — the company name e.g. "Specialized Loan Servicing LLC" or "loanDepot"
    servicing_fee_rate — convert to decimal: "0.50000% per annum" → 0.005; "25 basis points" → 0.0025
    advance_obligation — false if the document says "neither the servicer... will be obligated to make any monthly advances"
    portfolio_pct — the fraction of loans they service, e.g. "60.53% of the Mortgage Loans" → 0.6053
  TESTH101 example: SLS services ~60.53%, loanDepot ~39.47%, both at 0.50000% per annum (= 0.005)

Page headers show [pass1: pymupdf_text] or [pass1: gemini_vision_*] — same deal, two layouts.
PyMuPDF: line-broken class rows; Gemini: markdown tables. Extract from both.

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

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TWO PASS-1 EXTRACTION FORMATS (you will see both)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Each page header shows [pass1: ...] — that page used ONE extractor only:

FORMAT A — pymupdf_text / pymupdf_text+tables
  Native PDF text. Rows may be line-broken:
    Class A-1
     $126,186,000
  Or inline: Class A-1  $126,186,000  6.81655%  Senior/Floater ...

FORMAT B — docai_pseudo_table / docai_table
  Document AI Layout Parser: clean structured markdown tables (most accurate).
  | Class | Initial Principal | Pass-Through Rate | Type | ... |
  | A-1   | $126,186,000      | 6.81655%          | ...  | ... |

Do NOT skip a page because the format differs. Extract from BOTH.

━━━━━━━━━━━━━━━━━━━━━━━━━━━
HOW TO FIND THE TABLE
━━━━━━━━━━━━━━━━━━━━━━━━━━━
Look for "THE OFFERED CERTIFICATES", "SUMMARY OF THE CERTIFICATES",
or "INITIAL CLASS PRINCIPAL AMOUNT". Examples:

PyMuPDF: Class A-1  $126,186,000  6.81655%  ...  OR  Class A-1\\n $126,186,000
DocAI:   | A-1 | $126,186,000 | 6.81655% | Senior | ...  (clean structured table)
Gemini:  | Class A-1 | $126,186,000 | 6.81655% | Senior | ...

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
    "fee_type": "percentage",       // "percentage" | "fixed"
    "fee_rate": 0.0025,             // annual decimal rate — null if fixed-dollar
    "fixed_amount": null,           // annual dollar amount — null if percentage
    "priority": 1,                  // 1=highest priority (servicing fee), increment for each subsequent fee
    "fee_cap": null,                // annual cap in dollars — null if none
    "applies_to": "pool_balance",   // "pool_balance" | "class_balance" | "issuing_entity"
    "servicer_name": null,          // servicer name if servicer-specific, else null
    "category": "fee",              // "fee" for regular ongoing fees | "expense" for capped/irregular trust expenses
    "payee": "Servicer Name"        // who receives the payment
  }
]

CATEGORY RULES — this is important:
- category = "fee"     : recurring fees paid BEFORE the waterfall (servicing fee, SA fee, trustee fee, custodian fee)
- category = "expense" : trust expenses subject to the Annual Expense Cap ($250,000 or stated cap)

COMMON FEES TO LOOK FOR (with typical rates):
- Servicing Fee: 0.25%–0.50% per annum on pool balance (category: "fee")
- Securities Administrator / SA Fee: 0.01%–0.03% per annum (category: "fee")
- Owner Trustee / Indenture Trustee Fee: fixed dollar amount per year or 0.01% (category: "fee")
- Custodian Fee: fixed dollar amount (e.g. $12 per loan per year) or 0.01% (category: "fee")
- Participation Owner Trustee / Participation Registrar Fee: fixed or bps (category: "fee")
- Loan Data Agent Fee: fixed or bps (category: "fee")
- Rating Agency Fee: fixed annual amount if recurring (category: "expense")
- Trust Expenses / Issuing Entity Expenses: capped at Annual Expense Cap (category: "expense")
- Annual Expense Cap: record as a separate expense entry with fixed_amount = the cap dollar amount

CONVERSION: "X basis points" = X/10000. "X bps" = X/10000. "$250,000 per year" → fixed_amount=250000.
For fees stated as "$X per loan": multiply by estimated loan count, or use the exact dollar amount if stated.
Priority 1 = paid first; increment sequentially.

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
      "concurrent_with": ["M-1", "M-2"]
    }
  ],
  "principal_waterfall": [ ...same format... ],
  "excess_cashflow_waterfall": [ ...same format... ]
}

EXTRACTION RULES:
1. Follow the numbered list — each "(1)", "(2)", "first", "second" etc. is a step
2. interest_waterfall: from Interest Remittance Amount → pay interest to each class in order
3. principal_waterfall: from Principal Remittance Amount → pay principal to each class
4. excess_cashflow_waterfall: the "Monthly Excess Cashflow" distribution steps

payment_type values: "interest", "principal", "reserve", "excess", "fee", "loss_reimbursement"
source_bucket values: "interest_remittance", "principal_remittance", "excess_cashflow", "available_funds"
condition values: "always", "trigger_failure", "trigger_pass", null

CRITICAL — concurrent_with MUST be a JSON ARRAY, never a string:
  CORRECT: "concurrent_with": ["M-1", "M-2", "M-3"]
  WRONG:   "concurrent_with": "M-1, M-2, M-3"
  If not concurrent: "concurrent_with": null

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
        # Infer fee_type when the model omits it
        fee_rate = _safe_float(raw.get("fee_rate"))
        fixed_amount = _safe_float(raw.get("fixed_amount"))
        raw_type = raw.get("fee_type")
        if raw_type in ("percentage", "fixed"):
            fee_type = raw_type
        elif fee_rate is not None:
            fee_type = "percentage"
        elif fixed_amount is not None:
            fee_type = "fixed"
        else:
            fee_type = "percentage"

        # Infer category: known expense keywords → "expense"
        _EXPENSE_KEYWORDS = (
            "trust expense", "issuing entity expense", "indemnif",
            "rating agency", "annual expense cap",
        )
        raw_cat = str(raw.get("category") or "fee").lower()
        if raw_cat == "expense" or any(kw in fee_name.lower() for kw in _EXPENSE_KEYWORDS):
            category = "expense"
        else:
            category = "fee"

        return FeeConfig(
            fee_name=fee_name,
            fee_rate=fee_rate,
            fixed_amount=fixed_amount,
            fee_type=fee_type,
            priority=int(raw.get("priority") or 1),
            fee_cap=_safe_float(raw.get("fee_cap")),
            applies_to=str(raw.get("applies_to", "pool_balance")),
            servicer_name=raw.get("servicer_name"),
            category=category,
            payee=raw.get("payee"),
        )
    except Exception as e:
        logger.warning(f"Could not build FeeConfig from {raw}: {e}")
        return None


def _build_waterfall_step(raw: Dict) -> Optional[WaterfallStep]:
    try:
        cw = raw.get("concurrent_with")
        # GPT sometimes returns a comma string instead of a list; normalise here
        if isinstance(cw, str):
            cw = [s.strip() for s in cw.split(",") if s.strip()] or None
        return WaterfallStep(
            step=int(raw.get("step") or 0),
            description=str(raw.get("description", "")),
            class_name=raw.get("class_name"),
            payment_type=str(raw.get("payment_type", "interest")),
            source_bucket=str(raw.get("source_bucket", "available_funds")),
            condition=raw.get("condition"),
            amount_formula=raw.get("amount_formula"),
            concurrent_with=cw,
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
        elif (
            raw_cls.get("is_residual")
            or raw_cls.get("is_notional")
            or raw_cls.get("is_exchangeable")
            or str(raw_cls.get("interest_rate_type", "")).lower() in ("residual", "excess_cashflow", "exchangeable")
        ):
            # Residual / notional / exchangeable classes never have a dollar principal
            # in the PDF so regex won't find them — always keep them.
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

def _deterministic_formula(step: Dict) -> str:
    """
    Derive a Python formula expression for a waterfall step purely from its
    structured fields — no LLM involved.

    Rules (applied in order):
      1. interest step with class  → min(interest_due.get("CLS", 0), available_funds)
      2. principal step with class → min(balances.get("CLS", 0), available_funds)
      3. reserve/OC fill           → min(max(0, reserve_target - reserve_balance), available_funds)
      4. fee step                  → min(fee_amounts.get("FEE", 0), available_funds)
      5. realized-loss language    → min(realized_loss, available_funds)
      6. excess / residual         → available_funds
    """
    ptype = (step.get("payment_type") or "").lower()
    cname = (step.get("class_name") or "").strip()
    desc  = (step.get("description") or "").lower()
    fee   = (step.get("fee_name") or "").strip()

    if ptype == "interest" and cname:
        return f'min(interest_due.get("{cname}", 0), available_funds)'

    if ptype == "principal" and cname:
        return f'min(balances.get("{cname}", 0), available_funds)'

    if ptype == "reserve" or (
        not cname and ("reserve" in desc or "oc account" in desc or "overcollateral" in desc)
    ):
        return 'min(max(0, reserve_target - reserve_balance), available_funds)'

    if ptype == "fee":
        key = fee if fee else (cname if cname else "fee")
        return f'min(fee_amounts.get("{key}", 0), available_funds)'

    if any(kw in desc for kw in ("realized loss", "loss reimburs", "write-down")):
        return 'min(realized_loss, available_funds)'

    # excess / residual / pass-through
    return 'available_funds'


def _apply_formulas_to_steps(steps_raw: List[Dict], _unused=None) -> List[Dict]:
    """Assign deterministic formulas to every waterfall step in-place."""
    for s in steps_raw:
        s["amount_formula"] = _deterministic_formula(s)
    return steps_raw


# ---------------------------------------------------------------------------
# Gemini structured JSON extraction
# ---------------------------------------------------------------------------

def gemini_extract_json(prompt_text, expect_list=False):
    """Call Gemini and parse the response as JSON."""

    for attempt in range(1, GEMINI_MAX_RETRIES + 1):

        try:

            response = _get_gemini_client().models.generate_content(
                model=GEMINI_MODEL_NAME,
                contents=prompt_text
            )

            content = response.text.strip()

            # strip markdown code fences
            fence_m = re.search(
                r"```(?:json)?\s*([\s\S]+?)\s*```",
                content,
                re.DOTALL,
            )
            if fence_m:
                content = fence_m.group(1).strip()

            # direct parse
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                pass

            # find embedded object / array
            for pat in (
                r"(\{[\s\S]+\})",
                r"(\[[\s\S]+\])",
            ):
                m = re.search(pat, content)
                if m:
                    try:
                        return json.loads(m.group(1))
                    except json.JSONDecodeError:
                        pass

            # repair truncated JSON
            for suffix in ("]}", "}]", "}", "]"):
                try:
                    return json.loads(content + suffix)
                except Exception:
                    pass

            return [] if expect_list else {}

        except Exception as e:

            err = str(e)

            is_retryable = (
                "502" in err or "503" in err
                or "500" in err or "429" in err
                or "overloaded" in err.lower()
                or "rate" in err.lower()
            )

            if is_retryable and attempt < GEMINI_MAX_RETRIES:

                print(
                    f"gemini_extract_json retry "
                    f"{attempt} ({err[:50]})..."
                )

                time.sleep(GEMINI_RETRY_SLEEP)

            else:

                print(f"gemini_extract_json error: {err[:80]}")

                return [] if expect_list else {}


# ---------------------------------------------------------------------------
# Main mapping pipeline
# ---------------------------------------------------------------------------
def map_deal_config(results):
    """
    Takes the list of extracted page dicts from extract_pdf()
    and produces a structured deal configuration dict.

    Steps:
      1. Convert results → [(page_num, text)]
      2. Regex pre-scan (dates, amounts, classes, margins)
      3. Score pages per section
      4. Parallel Gemini calls for deal_info / classes /
         fees / waterfall / triggers
      5. Override LLM values with authoritative regex values
      6. Reconcile / hallucination guard
      7. Validation pass for missing critical fields
      8. Return structured dict + section_page_map
    """

    # ---- 1. build pages list ----
    pages = [
        (r["page"], r["content"])
        for r in results
        if r.get("content")
    ]
    total_pages = len(pages)

    if not pages:
        print("MAP: no page content to map")
        return {}

    print(f"MAP: {total_pages} pages")

    methods = _methods_by_page(results)

    # ---- 2. regex pre-scan ----
    print("MAP: regex pre-scan")
    regex_data = regex_prescan(pages)
    print(
        f"MAP: regex found: "
        f"{[k for k in regex_data if not k.startswith('regex_')]}"
    )

    # ---- 3. score pages ----
    scores = score_pages(pages)

    # ---- 4a. build text windows for each section ----
    # deal info: first 40 pages + top scored deal pages
    deal_pages_set = set(range(1, min(41, total_pages + 1)))
    for pnum, _ in scores.get("deal_summary", [])[:8]:
        for offset in range(-2, 5):
            deal_pages_set.add(pnum + offset)
    deal_info_text = _join_pages_for_mapping(
        sorted(deal_pages_set), pages, methods
    )[:20000]

    cert_text = get_best_pages_text(
        pages, scores, "certificate_table",
        window=10, max_chars=22000, top_n=6, methods_by_page=methods,
    )
    early_text = _join_pages_for_mapping(
        list(range(1, min(21, total_pages + 1))), pages, methods
    )
    cert_text = (cert_text + "\n\n" + early_text)[:22000]

    fee_text = get_best_pages_text(
        pages, scores, "fees_expenses",
        window=8, max_chars=18000, top_n=5, methods_by_page=methods,
    )
    if not fee_text:
        fee_pages = [
            p for p, t in pages
            if "basis point" in t.lower() or "servicing fee" in t.lower()
        ][:10]
        fee_text = _join_pages_for_mapping(sorted(fee_pages), pages, methods)[:18000]

    wf_text = _build_waterfall_text(pages, scores, methods, max_chars=20000)

    trigger_text = get_best_pages_text(
        pages, scores, "loss_allocation",
        window=8, max_chars=16000, top_n=4, methods_by_page=methods,
    ) or wf_text[:16000]

    # ---- 4b. parallel Gemini calls ----
    print("MAP: running Gemini structured extraction")

    def _call(prompt_template, text, expect_list=False):
        return gemini_extract_json(
            prompt_template.replace("{text}", text),
            expect_list=expect_list,
        )

    with ThreadPoolExecutor(max_workers=3) as ex:

        f_deal = ex.submit(
            _call, DEAL_INFO_PROMPT, deal_info_text
        )
        f_classes = ex.submit(
            _call, CLASSES_PROMPT, cert_text, True
        )
        f_fees = ex.submit(
            _call, FEES_PROMPT, fee_text, True
        )
        f_wf = ex.submit(
            _call, WATERFALL_PROMPT, wf_text
        )
        f_triggers = ex.submit(
            _call, TRIGGERS_PROMPT, trigger_text
        )

        deal_info     = f_deal.result()
        classes_raw   = f_classes.result()
        fees_raw      = f_fees.result()
        waterfall_raw = f_wf.result()
        triggers_raw  = f_triggers.result()

    # normalise shapes
    if isinstance(deal_info, list):
        deal_info = {}
    if not isinstance(classes_raw, list):
        classes_raw = []
    if not isinstance(fees_raw, list):
        fees_raw = []
    if not isinstance(waterfall_raw, dict):
        waterfall_raw = {}

    # fill deal_info gaps with regex
    for field, value in regex_data.items():
        if (
            not field.startswith("regex_")
            and not deal_info.get(field)
        ):
            deal_info[field] = value

    print(
        f"MAP: LLM extracted "
        f"{len(classes_raw)} classes, "
        f"{len(fees_raw)} fees"
    )

    # ---- 5. pdfplumber direct table: add missing classes only (no field overrides) ----
    direct_classes: List[Dict] = []
    if PDF_PATH and os.path.exists(PDF_PATH):
        try:
            direct_classes = _extract_certificate_table_direct(PDF_PATH, pages)
        except Exception as e:
            logger.warning(f"Direct table extraction in map_deal_config failed: {e}")
    existing_norms = {_normalize_class_name(r.get("class_name", "")) for r in classes_raw}
    for dc in (direct_classes or []):
        if _normalize_class_name(dc.get("class_name", "")) not in existing_norms:
            classes_raw.append(dc)
            logger.info(f"Direct-table added missing class: {dc['class_name']}")

    # ---- filter waterfall by known classes ----
    valid_names = {
        r.get("class_name")
        for r in classes_raw
        if r.get("class_name")
    }
    for wf_key in (
        "interest_waterfall",
        "principal_waterfall",
        "excess_cashflow_waterfall",
    ):
        raw_steps = waterfall_raw.get(wf_key) or []
        waterfall_raw[wf_key] = (
            _filter_waterfall_steps_by_classes(
                raw_steps, valid_names
            )
        )

    # ---- 7. validation pass ----
    missing = []
    if not deal_info.get("cut_off_date"):
        missing.append("cut_off_date")
    if not deal_info.get("closing_date"):
        missing.append("closing_date")
    if not deal_info.get("original_pool_balance"):
        missing.append("original_pool_balance")

    zero_principal = [
        r.get("class_name") for r in classes_raw
        if not _safe_float(r.get("initial_principal"))
        and not r.get("is_residual")
    ]
    if zero_principal:
        missing.append(
            f"initial_principal for: {zero_principal}"
        )

    if missing:

        print(f"MAP: validation pass for {missing}")

        val_pages_set = set(
            range(1, min(51, total_pages + 1))
        )
        for section in (
            "certificate_table",
            "deal_summary",
        ):
            for pnum, _ in scores.get(section, [])[:5]:
                for offset in range(-2, 6):
                    val_pages_set.add(pnum + offset)

        val_text = _join_pages_for_mapping(
            sorted(p for p in val_pages_set if p in page_dict),
            pages, methods,
        )[:22000]

        val_result = gemini_extract_json(
            VALIDATION_PROMPT
            .replace(
                "{missing_fields}",
                "\n".join(f"- {m}" for m in missing),
            )
            .replace("{text}", val_text)
        )

        if isinstance(val_result, dict):
            for field in (
                "cut_off_date", "closing_date",
                "first_payment_date",
                "original_pool_balance",
            ):
                if (
                    not deal_info.get(field)
                    and val_result.get(field)
                ):
                    deal_info[field] = val_result[field]

    # ---- 8. assemble final dict ----
    section_page_map = _build_section_page_map(
        scores, total_pages
    )

    deal_config = {
        "deal_info": deal_info,
        "classes": classes_raw,
        "fees": fees_raw,
        "waterfall": waterfall_raw,
        "triggers": triggers_raw,
        "section_page_map": section_page_map,
        "regex_found": regex_data,
    }

    print(
        f"MAP: complete — "
        f"{len(classes_raw)} classes, "
        f"{len(fees_raw)} fees, "
        f"waterfall steps: "
        f"{len(waterfall_raw.get('interest_waterfall', []))}"
        f" interest / "
        f"{len(waterfall_raw.get('principal_waterfall', []))}"
        f" principal"
    )

    return deal_config

# =====================================================
# AZURE OPENAI RETRIEVAL CALL (GPT-5.5)
# =====================================================

async def _call_gemini(
    system: str,
    user: str,
    expect_list: bool = False,
) -> Any:
    """
    Async LLM call for structured retrieval/extraction using Azure OpenAI GPT-5.5.
    Named _call_gemini for API compatibility; internally uses AzureChatOpenAI.
    PDF extraction (raw page text) still uses Gemini — only the structured
    extraction prompts (deal_info, classes, fees, waterfall, etc.) use GPT-5.5.
    """
    llm = _get_retrieval_llm()
    full_system = _RETRIEVAL_PASS1_FORMAT_SYSTEM + system
    messages = [SystemMessage(content=full_system), HumanMessage(content=user)]

    for attempt in range(1, 4):
        try:
            resp = await llm.ainvoke(messages)
            content = resp.content.strip()

            # Strip markdown code fences
            fence_m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", content, re.DOTALL)
            if fence_m:
                content = fence_m.group(1).strip()

            # Direct parse
            try:
                result = json.loads(content)
            except json.JSONDecodeError:
                result = None
                for pat in (r"(\{[\s\S]+\})", r"(\[[\s\S]+\])"):
                    m = re.search(pat, content)
                    if m:
                        try:
                            result = json.loads(m.group(1))
                            break
                        except json.JSONDecodeError:
                            pass

            if result is None:
                # Attempt repair of truncated JSON
                for suffix in ("]}", "}]", "}", "]"):
                    try:
                        result = json.loads(content + suffix)
                        break
                    except Exception:
                        pass

            if result is None:
                result = [] if expect_list else {}

            if expect_list:
                if isinstance(result, list):
                    return result
                if isinstance(result, dict):
                    for key in ("classes", "certificates", "tranches", "bonds",
                                "fees", "expenses", "fee_schedule", "steps", "waterfall"):
                        if isinstance(result.get(key), list):
                            return result[key]
                return []
            return result if result is not None else {}

        except Exception as e:
            err = str(e)
            is_retryable = (
                "429" in err or "503" in err or "502" in err
                or "500" in err or "rate" in err.lower()
                or "overloaded" in err.lower()
            )
            if is_retryable and attempt < 3:
                logger.warning(f"_call_gemini retry {attempt}: {err[:80]}")
                await asyncio.sleep(8)
            else:
                logger.warning(f"_call_gemini failed: {err[:120]}")
                return [] if expect_list else {}


async def extract_deal_config_from_pdf(
    pdf_path: str,
    deal_id: str,
    progress_callback=None,
) -> "DealConfig":
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


    # === PASS 1: Gemini bulk extraction of the PDF (unchanged pipeline) ===
    await _progress("Extracting PDF text and tables via Gemini", 5)
    _t0 = time.perf_counter()
    raw_results: List[Dict] = await asyncio.to_thread(extract_pdf, pdf_path)
    logger.info(f"PDF extraction complete in {time.perf_counter() - _t0:.1f}s")
    pages: List[Tuple[int, str]] = [
        (r["page"], r.get("content", ""))
        for r in raw_results
        if r.get("content")
    ]
    total_pages = len(pages)
    logger.info(f"Total pages extracted: {total_pages}")

    methods = _methods_by_page(raw_results)

    # === PASS 2: Regex pre-scan across ALL pages ===
    await _progress("Scanning all pages for key values", 12)
    regex_data = regex_prescan(pages)

    # === PASS 3: Score pages for each section ===
    await _progress("Identifying best pages per section", 18)
    scores = score_pages(pages)
    for section, top in scores.items():
        logger.info(f"  {section}: top pages = {[p for p, _ in top[:5]]}")

    # === Prepare inputs for Group A (Pass 4 + Pass 5b LLM) ===
    deal_info_pages = set(range(1, min(41, total_pages + 1)))
    # deal_summary top pages
    for pnum, _ in scores.get("deal_summary", [])[:8]:
        for offset in range(-2, 5):
            deal_info_pages.add(pnum + offset)
    # Also include fee pages — servicer names and fee rates often appear in the fee schedule section
    for pnum, _ in scores.get("fees_expenses", [])[:5]:
        for offset in range(-3, 6):
            deal_info_pages.add(pnum + offset)
    deal_info_text = _join_pages_for_mapping(
        sorted(p for p in deal_info_pages if 1 <= p <= total_pages),
        pages, methods,
    )[:24000]

    cert_text = get_best_pages_text(
        pages, scores, "certificate_table",
        window=10, max_chars=22000, top_n=6, methods_by_page=methods,
    )
    early_pages_text = _join_pages_for_mapping(
        list(range(1, min(21, total_pages + 1))), pages, methods,
    )
    cert_text = (cert_text + "\n\n" + early_pages_text)[:22000]

    # === GROUP A: Pass 4 LLM + Pass 5b LLM + Pass 5a (direct table parse) all in parallel ===
    # Pass 5a is blocking pdfplumber work, so we hand it off to a thread; the two LLM
    # calls run on the event loop. All three results are required before the override step.
    await _progress("Extracting deal info + cert classes + direct table (parallel)", 25)
    _grp_a_t0 = time.perf_counter()
    deal_info, classes_raw, direct_classes = await asyncio.gather(
        _call_gemini(
            "You are an expert ABS/MBS structured finance analyst. Extract exact field values as JSON. NEVER fabricate values.",
            # Use replace() instead of .format() so that braces in PDF text don't cause KeyError
            DEAL_INFO_PROMPT.replace("{text}", deal_info_text),
        ),
        _call_gemini(
            (
                "You are an expert ABS/MBS analyst. Extract ALL certificate classes from the "
                "'THE OFFERED CERTIFICATES' table. Return ONLY a JSON array. "
                "Pages mix pymupdf_text (line-broken Class/$ rows) and gemini_vision (markdown tables) — "
                "parse both. CRITICAL: copy initial_principal EXACTLY — never round or estimate."
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

    if not isinstance(deal_info, dict):
        deal_info = {}
    # Pass 4 post-processing: merge regex findings as fallback
    for field, value in regex_data.items():
        if not field.startswith("regex_") and not deal_info.get(field):
            deal_info[field] = value
            logger.info(f"  Regex fallback: {field} = {value}")

    # Servicer fallback: if LLM returned no servicers, use regex-extracted ones
    if not deal_info.get("servicers") and regex_data.get("regex_servicers"):
        deal_info["servicers"] = regex_data["regex_servicers"]
        logger.info(
            f"  Regex servicers applied: {[s['servicer_name'] for s in deal_info['servicers']]}"
        )

    logger.info(f"Extracted deal info: deal_name={deal_info.get('deal_name')}, cut_off_date={deal_info.get('cut_off_date')}, servicers={len(deal_info.get('servicers') or [])}")

    # Pass 5b post-processing: normalize shape
    if isinstance(classes_raw, dict):
        for key in ("classes", "certificates", "tranches", "bonds"):
            if isinstance(classes_raw.get(key), list):
                classes_raw = classes_raw[key]
                break
    if not isinstance(classes_raw, list):
        classes_raw = []
    logger.info(f"LLM extracted {len(classes_raw)} certificate classes (before override)")

    # Add classes found by pdfplumber that LLM completely missed (no field overrides)
    existing_norms = {_normalize_class_name(r.get("class_name", "")) for r in classes_raw}
    for dc in (direct_classes or []):
        if _normalize_class_name(dc.get("class_name", "")) not in existing_norms:
            classes_raw.append(dc)
            logger.info(f"Direct-table added missing class: {dc['class_name']}")

    # === PASS 6: Extract fees (scan fee sections + broad scan) ===
    # === Prepare inputs for Group B (Pass 6 + Pass 7 + Pass 8 in parallel) ===
    # Pass 6 input: fee pages
    fee_text = get_best_pages_text(
        pages, scores, "fees_expenses",
        window=8, max_chars=18000, top_n=5, methods_by_page=methods,
    )
    if not fee_text:
        fee_pages_fallback = [
            p for p, t in pages
            if "basis point" in t.lower() or "servicing fee" in t.lower()
        ][:10]
        fee_text = _join_pages_for_mapping(sorted(fee_pages_fallback), pages, methods)[:18000]

    # Pass 7 input: waterfall pages (numbered Priority of Distributions section)
    wf_text_1 = _build_waterfall_text(pages, scores, methods, max_chars=22000)

    # Pass 8 input: trigger pages (falls back to waterfall text)
    trigger_text = get_best_pages_text(
        pages, scores, "loss_allocation",
        window=8, max_chars=16000, top_n=4, methods_by_page=methods,
    )
    if not trigger_text:
        trigger_text = wf_text_1[:16000]

    # === GROUP B: Pass 6 (fees) + Pass 7 (waterfall) + Pass 8 (triggers) in parallel ===
    await _progress("Extracting fees + waterfall + triggers (parallel)", 48)
    _grp_b_t0 = time.perf_counter()
    fees_raw, waterfall_raw, loss_triggers_raw = await asyncio.gather(
        _call_gemini(
            "You are an expert ABS/MBS analyst. Extract ALL fees. Return ONLY a JSON array.",
            FEES_PROMPT.replace("{text}", fee_text),
            expect_list=True,
        ),
        _call_gemini(
            "You are an expert ABS/MBS analyst. Extract the complete Priority of Payments. Follow numbered steps exactly.",
            WATERFALL_PROMPT.replace("{text}", wf_text_1),
        ),
        _call_gemini(
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

    if not isinstance(waterfall_raw, dict):
        waterfall_raw = {}

    # Retry waterfall if LLM returned empty but numbered Priority section exists in PDF
    if not any(waterfall_raw.get(k) for k in (
        "interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall"
    )):
        pom_pages = _find_priority_of_payments_pages(pages)
        if pom_pages:
            logger.info(
                f"Waterfall empty — re-querying on Priority of Distributions pages {pom_pages}"
            )
            pom_text = _join_pages_for_mapping(pom_pages, pages, methods)
            retry_wf = await _call_gemini(
                "You are an expert ABS/MBS analyst. Extract the complete Priority of Payments. "
                "Return a JSON object with interest_waterfall, principal_waterfall, "
                "and excess_cashflow_waterfall arrays.",
                WATERFALL_PROMPT.replace("{text}", pom_text[:22000]),
            )
            if isinstance(retry_wf, dict) and any(retry_wf.get(k) for k in (
                "interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall"
            )):
                waterfall_raw = retry_wf

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
        val_text = _join_pages_for_mapping(
            sorted(p for p in val_pages if 1 <= p <= total_pages),
            pages, methods,
        )[:22000]

        val_result = await _call_gemini(
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

    # === PASS 10: Assign deterministic math formulas to every waterfall step ===
    await _progress("Generating waterfall computation formulas", 88)
    for wf_key in ("interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall"):
        bucket = waterfall_raw.get(wf_key) or []
        if bucket:
            waterfall_raw[wf_key] = _apply_formulas_to_steps(bucket)
    total_formulas = sum(
        len(waterfall_raw.get(k) or [])
        for k in ("interest_waterfall", "principal_waterfall", "excess_cashflow_waterfall")
    )
    logger.info(f"Generated {total_formulas} step formulas")

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
        extraction_source="gemini_extracted",
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


# ---------------------------------------------------------------------------
# Natural-language trigger condition -> Python expression
#
# Used by the trigger edit UI when a user does not know the engine's variable
# names. We give the LLM a curated catalog (see ``services.trigger_variables``)
# and ask it to emit a Python boolean expression that uses ONLY those names.
# ---------------------------------------------------------------------------


TRIGGER_NL_TO_EXPR_SYSTEM_PROMPT = """You translate plain-English ABS/MBS trigger descriptions into Python boolean expressions that the waterfall engine can evaluate.

You MUST output a JSON object with these keys:
  "condition":   string - a single Python boolean expression. References ONLY the variable names from the catalog below. No imports, no calls, no lambdas.
  "action":      string - SCREAMING_SNAKE_CASE flag name set to True when the condition fires. Reuse one of the known action names if it fits.
  "explanation": string - one short sentence describing what the expression checks, for the user to sanity-check.

HARD RULES
1. Use ONLY the variable names listed in the catalog. Inventing names (e.g., ``oc_ratio``, ``trigger_value``) is forbidden - those will fail at evaluation time.
2. Percentages in the catalog are decimal fractions: 5% -> 0.05, 12.5% -> 0.125.
3. Dollar amounts are absolute (no scaling).
4. The expression must be self-contained - no statements, no semicolons.
5. If the description is ambiguous, pick the most defensible mapping and call out the assumption inside ``explanation``.
6. If no listed variable matches the description, return condition="" and explanation describing which variable would be needed."""


def _build_trigger_nl_to_expr_user_prompt(description: str, test_name: str = "") -> str:
    """Compose the LLM user message with the curated variable catalog."""
    # Local import to avoid creating a circular dep at module-load time
    from services.trigger_variables import (
        action_catalog_for_prompt,
        variable_catalog_for_prompt,
    )

    test_name_block = (
        f"TEST NAME (for context):\n{test_name.strip()}\n\n"
        if test_name.strip() else ""
    )
    return (
        f"{test_name_block}"
        f"PLAIN-ENGLISH DESCRIPTION:\n{description.strip()}\n\n"
        f"AVAILABLE VARIABLES (use ONLY these names - no others):\n"
        f"{variable_catalog_for_prompt()}\n\n"
        f"KNOWN ACTION FLAG NAMES (reuse if applicable, else coin a new "
        f"SCREAMING_SNAKE_CASE name):\n"
        f"{action_catalog_for_prompt()}\n\n"
        f"Return JSON with keys: condition, action, explanation."
    )


async def generate_trigger_expression(
    description: str,
    test_name: str = "",
) -> Dict[str, str]:
    """
    Translate a plain-English trigger description into a Python boolean
    expression that uses only the curated waterfall trigger variables.

    Returns ``{"condition": str, "action": str, "explanation": str}``. On
    failure, returns empty strings so the caller can fall back to manual entry.
    """
    if not description or not description.strip():
        return {"condition": "", "action": "", "explanation": ""}

    user_prompt = _build_trigger_nl_to_expr_user_prompt(description, test_name)

    raw = await _call_gemini(
        system=TRIGGER_NL_TO_EXPR_SYSTEM_PROMPT,
        user=user_prompt,
        expect_list=False,
    )

    if not isinstance(raw, dict):
        return {"condition": "", "action": "", "explanation": ""}

    condition = str(raw.get("condition") or "").strip()
    action = str(raw.get("action") or "").strip()
    explanation = str(raw.get("explanation") or "").strip()

    # Hard validation: refuse to return a broken expression. We do three checks
    # against exactly the constraints _evaluate_config_trigger enforces at
    # waterfall-compute time, so anything that would silently fail there fails
    # loudly here instead.
    if condition:
        diag = _validate_trigger_expression(condition)
        if diag:
            logger.warning(
                f"Trigger NL->expr rejected: {diag}. Raw expression={condition!r}"
            )
            # Drop the condition - the frontend will see an empty field plus a
            # diagnostic explanation, so the user can rephrase or fill in
            # manually rather than save a no-op trigger.
            explanation = (
                f"Could not produce a valid expression: {diag} "
                f"The model proposed `{condition}`. Try rephrasing or enter "
                f"the expression manually."
            )
            condition = ""

    return {"condition": condition, "action": action, "explanation": explanation}


def _validate_trigger_expression(expression: str) -> Optional[str]:
    """
    Validate that ``expression`` will evaluate cleanly inside
    ``_evaluate_config_trigger``.

    Three checks, mirroring what the engine does:
      1. The string must parse as a Python expression (catches SyntaxError).
      2. Every bare identifier must be either an allowed variable, a safe
         builtin (min/max/abs), or a Python keyword/literal.
      3. The expression must execute against a dummy context with all known
         variables set, with the same restricted ``{"__builtins__": {}}``
         globals the engine uses (catches NameError, AttributeError, etc.).

    Returns None when the expression is safe; otherwise returns a short
    human-readable diagnostic.
    """
    from services.trigger_variables import ALLOWED_VARIABLE_NAMES

    # Check 1: parseable as a Python expression
    try:
        compile(expression, "<trigger>", "eval")
    except SyntaxError as e:
        return f"syntax error ({e.msg})"

    # Check 2: identifier whitelist. The identifier regex requires a leading
    # letter/underscore, so numeric literals (0.05, 1_000_000) are skipped
    # naturally. Strings inside the expression are not expected for triggers
    # and would only widen the false-positive surface, so we don't strip them.
    idents = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", expression))
    _SAFE_NAMES = {
        "and", "or", "not", "True", "False", "None", "in", "is",
        "min", "max", "abs",
    }
    unknown = idents - ALLOWED_VARIABLE_NAMES - _SAFE_NAMES
    if unknown:
        return (
            f"references unknown name(s): {', '.join(sorted(unknown))}. "
            f"Allowed variables: {', '.join(sorted(ALLOWED_VARIABLE_NAMES))}"
        )

    # Check 3: actually evaluate against a dummy context. Catches the long
    # tail (e.g. calls on non-callable objects, attribute access, division by
    # something we didn't anticipate).
    dummy_ctx = {name: 1.0 for name in ALLOWED_VARIABLE_NAMES}
    safe_globals = {
        "__builtins__": {},
        "min": min, "max": max, "abs": abs,
    }
    try:
        eval(expression, safe_globals, dummy_ctx)  # noqa: S307
    except NameError as e:
        return f"references undefined name: {e}"
    except Exception as e:
        return f"would raise {type(e).__name__} at evaluation time: {e}"

    return None


# =====================================================
# MAIN
# =====================================================

DEAL_CONFIG_JSON = OUTPUT_JSON.replace(
    "extracted_output", "deal_config"
)


if __name__ == "__main__":

    # -------------------------------------------------
    # STEP 1 — PDF extraction (Gemini bulk)
    # -------------------------------------------------

    if os.path.exists(OUTPUT_JSON):

        print("LOADING CACHED EXTRACTION")

        results = load_extraction()

    else:

        results = extract_pdf()

    # -------------------------------------------------
    # STEP 2 — structured mapping
    # -------------------------------------------------

    if os.path.exists(DEAL_CONFIG_JSON):

        print("LOADING CACHED DEAL CONFIG")

        with open(
            DEAL_CONFIG_JSON, "r", encoding="utf-8"
        ) as f:
            deal_config = json.load(f)

    else:

        deal_config = map_deal_config(results)

        with open(
            DEAL_CONFIG_JSON, "w", encoding="utf-8"
        ) as f:
            json.dump(
                deal_config, f, indent=2,
                ensure_ascii=False
            )

        print(f"DEAL CONFIG SAVED → {DEAL_CONFIG_JSON}")
