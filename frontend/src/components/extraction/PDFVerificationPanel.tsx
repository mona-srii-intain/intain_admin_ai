import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { FileText, X, ChevronLeft, ChevronRight, Search, ArrowUp, ArrowDown } from "lucide-react";
import * as pdfjsLib from "pdfjs-dist";
// Vite resolves ?url to a hashed asset URL the worker can be loaded from.
import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import "pdfjs-dist/web/pdf_viewer.css";
import type { DealConfig } from "../../types";
import { editSectionToUi, valuesForSection, type UiSectionKey } from "./pdfSectionValues";

// pdfjs needs its worker URL configured exactly once.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(pdfjsLib as any).GlobalWorkerOptions.workerSrc = workerSrc;

const SECTION_LABEL: Record<UiSectionKey, string> = {
  deal_info: "Deal information",
  certificate_classes: "Certificate classes",
  fees: "Fees & expenses",
  waterfall: "Priority of payments",
  triggers: "Triggers & loss allocation",
};

const norm = (s: string) => s.replace(/\s+/g, " ").trim().toLowerCase();

interface Props {
  pdfUrl: string | null;
  config: DealConfig;
  editingSection: string | null;
  overriddenValues: Set<string>;
  onClose: () => void;
}

export default function PDFVerificationPanel({ pdfUrl, config, editingSection, overriddenValues, onClose }: Props) {
  const uiKey = editSectionToUi(editingSection);
  const sectionPages = useMemo(() => {
    if (!uiKey) return [] as number[];
    return config.section_page_map?.[uiKey] ?? [];
  }, [uiKey, config.section_page_map]);

  const searchValues = useMemo(
    () => (uiKey ? valuesForSection(uiKey, config) : []),
    [uiKey, config],
  );

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const [pdfDoc, setPdfDoc] = useState<any>(null);
  const [loadingPdf, setLoadingPdf] = useState(false);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [activeIdx, setActiveIdx] = useState(0);

  // Find-in-PDF: user-driven text search across the rendered text layers.
  // Decoupled from the extracted-value highlighting so it gets its own color.
  const [userQuery, setUserQuery] = useState("");
  const [activeMatchIdx, setActiveMatchIdx] = useState(0);
  const [matchCount, setMatchCount] = useState(0);
  // PageView increments this whenever it re-applies highlights, so the parent
  // can re-scan the DOM for the latest set of `.pdf-hl-find` elements.
  const [highlightBump, setHighlightBump] = useState(0);

  const scrollRef = useRef<HTMLDivElement>(null);
  const pageEls = useRef<Record<number, HTMLDivElement | null>>({});
  const findInputRef = useRef<HTMLInputElement>(null);

  // Load PDF doc once per URL.
  useEffect(() => {
    if (!pdfUrl) {
      setPdfDoc(null);
      return;
    }
    let cancelled = false;
    setLoadingPdf(true);
    setPdfError(null);
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    const task = (pdfjsLib as any).getDocument({ url: pdfUrl, withCredentials: false });
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    task.promise
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .then((doc: any) => {
        if (cancelled) {
          doc.destroy();
          return;
        }
        setPdfDoc(doc);
        setLoadingPdf(false);
      })
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      .catch((err: any) => {
        if (cancelled) return;
        setPdfError(err?.message ?? "Failed to load PDF");
        setLoadingPdf(false);
      });
    return () => {
      cancelled = true;
      try { task.destroy(); } catch { /* noop */ }
    };
  }, [pdfUrl]);

  // When the active section changes, jump to its first relevant page.
  useEffect(() => {
    setActiveIdx(0);
    const first = sectionPages[0];
    if (first != null) {
      requestAnimationFrame(() => {
        const el = pageEls.current[first];
        if (el && scrollRef.current) {
          scrollRef.current.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: "smooth" });
        }
      });
    }
  }, [editingSection, sectionPages]);

  // Reset the find input when the user switches sections — otherwise a stale
  // query from a previous section silently affects the new pages' highlights.
  useEffect(() => {
    setUserQuery("");
    setActiveMatchIdx(0);
  }, [editingSection]);

  // Whenever the query changes, reset the navigated match index to 0.
  useEffect(() => {
    setActiveMatchIdx(0);
  }, [userQuery]);

  // After every highlight pass, re-scan the DOM, update the count, and move
  // the visual "active" marker. useLayoutEffect so we don't see a frame where
  // an old match is still styled as active.
  useLayoutEffect(() => {
    const container = scrollRef.current;
    if (!container) {
      setMatchCount(0);
      return;
    }
    container.querySelectorAll(".pdf-hl-find-active").forEach((el) => el.classList.remove("pdf-hl-find-active"));
    if (!userQuery.trim()) {
      setMatchCount(0);
      return;
    }
    const els = Array.from(container.querySelectorAll<HTMLElement>(".pdf-hl-find"));
    setMatchCount(els.length);
    if (els.length === 0) return;
    const safe = ((activeMatchIdx % els.length) + els.length) % els.length;
    const target = els[safe];
    target.classList.add("pdf-hl-find-active");
    target.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [highlightBump, activeMatchIdx, userQuery]);

  const goPrev = () => {
    if (matchCount === 0) return;
    setActiveMatchIdx((i) => (i - 1 + matchCount) % matchCount);
  };
  const goNext = () => {
    if (matchCount === 0) return;
    setActiveMatchIdx((i) => (i + 1) % matchCount);
  };

  // Stable reference for the highlight-bump callback so PageView's effect deps
  // don't churn every parent render (would loop the highlight effect).
  const bumpHighlights = useCallback(() => {
    setHighlightBump((v) => v + 1);
  }, []);

  const idle = !editingSection || !uiKey;
  const noPages = !idle && sectionPages.length === 0;
  const noPdf = !idle && !pdfUrl;

  return (
    <div className="h-full flex flex-col bg-gradient-to-br from-slate-50 to-white border border-slate-200 rounded-xl overflow-hidden shadow-sm">
      <PanelStyles />

      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-slate-200 bg-white">
        <div className="flex items-center gap-2 text-sm font-semibold text-slate-700 min-w-0">
          <FileText size={14} className="text-primary-600 flex-shrink-0" />
          <span className="truncate">
            {idle ? "Source PDF" : `Source — ${SECTION_LABEL[uiKey!] ?? uiKey}`}
          </span>
        </div>
        <button
          onClick={onClose}
          className="text-slate-400 hover:text-slate-700 p-1 rounded hover:bg-slate-100 transition-colors"
          title="Collapse panel"
        >
          <X size={14} />
        </button>
      </div>

      {/* Find-in-PDF bar — Ctrl+F style search across rendered text layers. */}
      {!idle && pdfDoc && sectionPages.length > 0 && (
        <div className="flex items-center gap-2 px-3 py-2 border-b border-slate-100 bg-white">
          <Search size={13} className="text-slate-400 flex-shrink-0" />
          <input
            ref={findInputRef}
            type="text"
            value={userQuery}
            onChange={(e) => setUserQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                if (e.shiftKey) goPrev();
                else goNext();
              } else if (e.key === "Escape") {
                e.preventDefault();
                setUserQuery("");
              }
            }}
            placeholder="Find in these pages…"
            className="flex-1 min-w-0 text-xs px-1.5 py-1 bg-transparent border-0 focus:outline-none placeholder-slate-400 text-slate-700"
          />
          {userQuery && (
            <span className="text-[11px] font-mono text-slate-500 whitespace-nowrap">
              {matchCount === 0
                ? "0 matches"
                : `${(((activeMatchIdx % matchCount) + matchCount) % matchCount) + 1} / ${matchCount}`}
            </span>
          )}
          <div className="flex items-center gap-0.5">
            <button
              onClick={goPrev}
              disabled={matchCount === 0}
              className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed"
              title="Previous match (Shift+Enter)"
            >
              <ArrowUp size={13} />
            </button>
            <button
              onClick={goNext}
              disabled={matchCount === 0}
              className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100 disabled:opacity-30 disabled:cursor-not-allowed"
              title="Next match (Enter)"
            >
              <ArrowDown size={13} />
            </button>
            {userQuery && (
              <button
                onClick={() => { setUserQuery(""); findInputRef.current?.focus(); }}
                className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-100"
                title="Clear (Esc)"
              >
                <X size={13} />
              </button>
            )}
          </div>
        </div>
      )}

      {/* Page chips */}
      {!idle && sectionPages.length > 0 && (
        <div className="flex items-center gap-1.5 px-3 py-2 border-b border-slate-100 bg-slate-50 overflow-x-auto">
          <span className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold pr-1">Pages:</span>
          {sectionPages.map((p, i) => (
            <button
              key={p}
              onClick={() => {
                setActiveIdx(i);
                const el = pageEls.current[p];
                if (el && scrollRef.current) {
                  scrollRef.current.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: "smooth" });
                }
              }}
              className={`text-xs px-2.5 py-1 rounded-full font-mono font-semibold whitespace-nowrap transition-colors ${
                i === activeIdx
                  ? "bg-primary-600 text-white shadow-sm"
                  : "bg-white border border-slate-200 text-slate-600 hover:bg-slate-100"
              }`}
            >
              p.{p}
            </button>
          ))}
          <NavArrows
            disabled={sectionPages.length < 2}
            onPrev={() => {
              const next = Math.max(0, activeIdx - 1);
              setActiveIdx(next);
              const el = pageEls.current[sectionPages[next]];
              if (el && scrollRef.current) scrollRef.current.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: "smooth" });
            }}
            onNext={() => {
              const next = Math.min(sectionPages.length - 1, activeIdx + 1);
              setActiveIdx(next);
              const el = pageEls.current[sectionPages[next]];
              if (el && scrollRef.current) scrollRef.current.scrollTo({ top: Math.max(0, el.offsetTop - 8), behavior: "smooth" });
            }}
          />
        </div>
      )}

      {/* Body */}
      <div ref={scrollRef} className="flex-1 overflow-y-auto p-3 space-y-3 bg-slate-100/40">
        {idle && (
          <div className="h-full min-h-[260px] flex flex-col items-center justify-center text-center px-6">
            <FileText size={36} className="text-slate-300 mb-2" />
            <p className="text-sm text-slate-500 max-w-xs">
              Click <span className="font-semibold">Edit</span> on any section to highlight its source pages in the indenture PDF.
            </p>
          </div>
        )}

        {noPdf && (
          <div className="h-full min-h-[260px] flex flex-col items-center justify-center text-center px-6">
            <FileText size={36} className="text-slate-300 mb-2" />
            <p className="text-sm text-slate-500 max-w-xs">
              No source PDF stored for this deal. Re-upload the indenture to enable side-by-side verification.
            </p>
          </div>
        )}

        {noPages && pdfUrl && (
          <div className="h-full min-h-[260px] flex flex-col items-center justify-center text-center px-6">
            <FileText size={36} className="text-slate-300 mb-2" />
            <p className="text-sm text-slate-500 max-w-xs">No relevant pages identified for this section.</p>
          </div>
        )}

        {loadingPdf && !pdfDoc && (
          <p className="text-xs text-slate-400 text-center py-12">Loading PDF…</p>
        )}
        {pdfError && (
          <p className="text-xs text-red-500 text-center py-12">{pdfError}</p>
        )}

        {!idle && pdfDoc && sectionPages.map((p) => (
          <PageView
            key={`${editingSection}-${p}-${searchValues.length}`}
            doc={pdfDoc}
            pageNum={p}
            searchValues={searchValues}
            overriddenValues={overriddenValues}
            userQuery={userQuery}
            onHighlights={bumpHighlights}
            onMount={(el) => { pageEls.current[p] = el; }}
          />
        ))}
      </div>

      {/* Footer legend */}
      {!idle && pdfDoc && sectionPages.length > 0 && (
        <div className="px-3 py-2 border-t border-slate-100 bg-white flex items-center gap-4 text-[11px] text-slate-500">
          <span className="inline-flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-sm" style={{ backgroundColor: "rgba(255, 215, 0, 0.55)" }} />
            Extracted value
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-sm" style={{ backgroundColor: "rgba(156, 163, 175, 0.55)" }} />
            Manually overridden
          </span>
          <span className="inline-flex items-center gap-1.5">
            <span className="w-3 h-3 rounded-sm" style={{ backgroundColor: "rgba(249, 115, 22, 0.85)" }} />
            Find match
          </span>
        </div>
      )}
    </div>
  );
}

function NavArrows({ onPrev, onNext, disabled }: { onPrev: () => void; onNext: () => void; disabled: boolean }) {
  return (
    <span className="ml-auto flex items-center gap-1">
      <button
        onClick={onPrev}
        disabled={disabled}
        className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
        title="Previous page"
      >
        <ChevronLeft size={14} />
      </button>
      <button
        onClick={onNext}
        disabled={disabled}
        className="p-1 rounded text-slate-400 hover:text-slate-700 hover:bg-slate-200 disabled:opacity-30 disabled:cursor-not-allowed"
        title="Next page"
      >
        <ChevronRight size={14} />
      </button>
    </span>
  );
}

interface PageViewProps {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  doc: any;
  pageNum: number;
  searchValues: string[];
  overriddenValues: Set<string>;
  userQuery: string;
  onHighlights: () => void;
  onMount: (el: HTMLDivElement | null) => void;
}

function PageView({ doc, pageNum, searchValues, overriddenValues, userQuery, onHighlights, onMount }: PageViewProps) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const textLayerRef = useRef<HTMLDivElement | null>(null);
  const [rendered, setRendered] = useState(false);

  // Render canvas + text layer.
  useEffect(() => {
    let cancelled = false;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let renderTask: any = null;
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    let textLayer: any = null;

    (async () => {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const page: any = await doc.getPage(pageNum);
      if (cancelled) return;

      const canvas = canvasRef.current;
      const textContainer = textLayerRef.current;
      const wrap = wrapRef.current;
      if (!canvas || !textContainer || !wrap) return;

      const wrapWidth = wrap.clientWidth || 640;
      const unscaled = page.getViewport({ scale: 1 });
      const scale = Math.min(2.0, Math.max(0.6, wrapWidth / unscaled.width));
      const viewport = page.getViewport({ scale });

      canvas.width = Math.ceil(viewport.width);
      canvas.height = Math.ceil(viewport.height);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      textContainer.style.width = `${viewport.width}px`;
      textContainer.style.height = `${viewport.height}px`;
      textContainer.innerHTML = "";
      // pdfjs v3+ uses CSS vars to scale the text layer to the canvas.
      textContainer.style.setProperty("--scale-factor", String(scale));

      const ctx = canvas.getContext("2d");
      if (!ctx) return;
      renderTask = page.render({ canvasContext: ctx, viewport });
      try {
        await renderTask.promise;
      } catch {
        return; // cancelled
      }
      if (cancelled) return;

      const textContent = await page.getTextContent();
      if (cancelled) return;

      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const TextLayer = (pdfjsLib as any).TextLayer;
      if (TextLayer) {
        textLayer = new TextLayer({
          textContentSource: textContent,
          container: textContainer,
          viewport,
        });
        await textLayer.render();
      }

      if (cancelled) return;
      setRendered(true);
    })().catch((err) => {
      console.error(`[PDFVerificationPanel] page ${pageNum} render failed:`, err);
    });

    return () => {
      cancelled = true;
      try { renderTask?.cancel?.(); } catch { /* noop */ }
      try { textLayer?.cancel?.(); } catch { /* noop */ }
    };
  }, [doc, pageNum]);

  // Apply / update highlights whenever search values or the user query change.
  useEffect(() => {
    if (!rendered) return;
    const container = textLayerRef.current;
    if (!container) return;

    // Clear previous highlights — including any from prior user-query passes.
    container.querySelectorAll(".pdf-hl, .pdf-hl-overridden, .pdf-hl-find, .pdf-hl-find-active").forEach((el) => {
      el.classList.remove("pdf-hl", "pdf-hl-overridden", "pdf-hl-find", "pdf-hl-find-active");
    });

    const spans = Array.from(container.querySelectorAll("span")) as HTMLSpanElement[];
    if (spans.length === 0) {
      onHighlights();
      return;
    }
    const spanTexts = spans.map((s) => norm(s.textContent ?? ""));

    // Helper: paint matches for one search string with a given class.
    // Tries single-span first, falls back to multi-span join for phrases.
    const paint = (raw: string, cls: string) => {
      const target = norm(raw);
      if (target.length < 2) return;
      let matchedSingle = false;
      for (let i = 0; i < spans.length; i++) {
        if (spanTexts[i].includes(target)) {
          spans[i].classList.add(cls);
          matchedSingle = true;
        }
      }
      if (!matchedSingle && target.length >= 6 && target.includes(" ")) {
        for (let i = 0; i < spans.length; i++) {
          let joined = spanTexts[i];
          if (joined.length >= target.length) continue;
          for (let j = i + 1; j < Math.min(i + 8, spans.length); j++) {
            joined = (joined + " " + spanTexts[j]).trim();
            if (joined.includes(target)) {
              for (let k = i; k <= j; k++) spans[k].classList.add(cls);
              break;
            }
            if (joined.length > target.length * 2) break;
          }
        }
      }
    };

    // Extracted-value / overridden highlights from the parent.
    for (const value of searchValues) {
      paint(value, overriddenValues.has(value) ? "pdf-hl-overridden" : "pdf-hl");
    }

    // User-driven find-in-PDF highlight (orange, parent handles active).
    const q = userQuery.trim();
    if (q.length >= 2) paint(q, "pdf-hl-find");

    onHighlights();
  }, [rendered, searchValues, overriddenValues, userQuery, onHighlights]);

  return (
    <div
      ref={(el) => { wrapRef.current = el; onMount(el); }}
      className="bg-white border border-slate-200 rounded-lg shadow-sm overflow-hidden"
    >
      <div className="text-[10px] uppercase tracking-wider text-slate-400 font-semibold bg-slate-50 px-3 py-1 border-b border-slate-100 font-mono">
        Page {pageNum}
      </div>
      <div className="relative" style={{ lineHeight: 0 }}>
        <canvas ref={canvasRef} className="block max-w-full" />
        <div
          ref={textLayerRef}
          className="textLayer"
          style={{ position: "absolute", inset: 0, overflow: "hidden", opacity: 1 }}
        />
      </div>
    </div>
  );
}

// Inject highlight styles once. Using a single React-rendered <style> tag keeps
// the component self-contained without adding a new CSS file to maintain.
function PanelStyles() {
  return (
    <style>{`
      .textLayer .pdf-hl {
        background-color: rgba(255, 215, 0, 0.45);
        border-radius: 2px;
        box-shadow: 0 0 0 1px rgba(202, 138, 4, 0.35);
      }
      .textLayer .pdf-hl-overridden {
        background-color: rgba(156, 163, 175, 0.55);
        border-radius: 2px;
        text-decoration: line-through;
        text-decoration-color: rgba(55, 65, 81, 0.85);
      }
      /* User find-in-PDF matches (orange, like Chrome's Ctrl+F). */
      .textLayer .pdf-hl-find {
        background-color: rgba(255, 165, 0, 0.45);
        border-radius: 2px;
      }
      .textLayer .pdf-hl-find-active {
        background-color: rgba(249, 115, 22, 0.85) !important;
        box-shadow: 0 0 0 2px rgba(194, 65, 12, 0.7);
        border-radius: 2px;
      }
    `}</style>
  );
}
