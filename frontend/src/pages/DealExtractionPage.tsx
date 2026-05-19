import { useState } from "react";
import { Sparkles, AlertTriangle, Info, CheckCircle, FileSearch, Layers, DollarSign, GitBranch, ShieldAlert } from "lucide-react";
import Header from "../components/layout/Header";
import UploadZone from "../components/extraction/UploadZone";
import ExtractionProgress from "../components/extraction/ExtractionProgress";
import ExtractedFields from "../components/extraction/ExtractedFields";
import type { DealConfig } from "../types";
import { extractDeal } from "../services/api";
import toast from "react-hot-toast";

type Status = "idle" | "extracting" | "done" | "error";

export default function DealExtractionPage() {
  const [file, setFile] = useState<File | null>(null);
  const [dealId, setDealId] = useState("");
  const [overwrite, setOverwrite] = useState(false);
  const [status, setStatus] = useState<Status>("idle");
  const [progress, setProgress] = useState(0);
  const [progressMsg, setProgressMsg] = useState("");
  const [result, setResult] = useState<DealConfig | null>(null);
  const [error, setError] = useState("");

  // Simulate progress steps while LLM is running
  const simulateProgress = () => {
    const steps = [
      { pct: 10, msg: "Reading PDF pages…" },
      { pct: 25, msg: "Identifying key sections…" },
      { pct: 40, msg: "Extracting deal information…" },
      { pct: 55, msg: "Extracting certificate classes…" },
      { pct: 70, msg: "Extracting fees & waterfall rules…" },
      { pct: 85, msg: "Extracting triggers & loss allocation…" },
      { pct: 95, msg: "Assembling deal configuration…" },
    ];
    let i = 0;
    const interval = setInterval(() => {
      if (i < steps.length) {
        setProgress(steps[i].pct);
        setProgressMsg(steps[i].msg);
        i++;
      } else {
        clearInterval(interval);
      }
    }, 4500);
    return () => clearInterval(interval);
  };

  const handleExtract = async () => {
    if (!file) { toast.error("Please select a PDF file."); return; }
    if (!dealId.trim()) { toast.error("Please enter a Deal ID."); return; }

    setStatus("extracting");
    setProgress(5);
    setProgressMsg("Uploading PDF…");
    setError("");
    const cleanup = simulateProgress();

    try {
      const data = await extractDeal(file, dealId.trim(), overwrite);
      cleanup();
      setProgress(100);
      setProgressMsg("Extraction complete — draft auto-saved!");
      setResult(data.deal_config as DealConfig);
      setStatus("done");
      toast.success(
        `Extracted ${data.extraction_summary?.classes_found ?? 0} classes — draft saved. Review and click "Save & Verify".`
      );
    } catch (err: unknown) {
      cleanup();
      const msg = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail ?? "Extraction failed.";
      setError(msg);
      setStatus("error");
      setProgressMsg("Extraction failed");
      toast.error("Extraction failed — check the error below.");
    }
  };

  const extractionItems = [
    { icon: FileSearch,   text: "Deal parties, dates & collateral details" },
    { icon: Layers,       text: "Certificate classes with rates & priorities" },
    { icon: DollarSign,   text: "Fees, expenses & servicer configuration" },
    { icon: GitBranch,    text: "Priority of payments waterfall steps" },
    { icon: ShieldAlert,  text: "Trigger tests & loss allocation order" },
  ];

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <Header
        title="Deal Indenture Extraction"      />

      <main className="flex-1 overflow-y-auto p-6 space-y-6">

        {/* ── Upload card — always visible ─────────────────────────────── */}
        <div className="card max-w-3xl mx-auto">
          <h2 className="section-title">
            <Sparkles size={16} className="text-primary-600" />
            Upload & Extract
          </h2>

          <div className="grid md:grid-cols-2 gap-6">
            {/* Left: Drop zone */}
            <UploadZone file={file} onFile={setFile} />

            {/* Right: Config + button */}
            <div className="space-y-4">
              <div>
                <label className="label">Deal ID *</label>
                <input
                  className="input"
                  placeholder="e.g. TESTH101"
                  value={dealId}
                  onChange={(e) => setDealId(e.target.value.toUpperCase())}
                />
                <p className="mt-1 text-xs text-gray-400 flex items-center gap-1">
                  <Info size={11} /> Must match the <code className="font-mono">DEAL_ID</code> in your Snowflake loantape.
                </p>
              </div>

              <div className="flex items-center gap-3">
                <input
                  type="checkbox"
                  id="overwrite"
                  checked={overwrite}
                  onChange={(e) => setOverwrite(e.target.checked)}
                  className="w-4 h-4 text-primary-600 rounded border-gray-300 focus:ring-primary-500"
                />
                <label htmlFor="overwrite" className="text-sm text-gray-600 cursor-pointer">
                  Overwrite if deal already exists
                </label>
              </div>

              <button
                className="btn-primary w-full justify-center py-3 text-sm"
                onClick={handleExtract}
                disabled={!file || !dealId.trim() || status === "extracting"}
              >
                <Sparkles size={15} />
                {status === "extracting" ? "Extracting…" : "Extract with AI"}
              </button>
            </div>
          </div>
        </div>

        {/* ── "What gets extracted" — shown ONLY after button is clicked ── */}
        {status === "extracting" && (
          <div className="max-w-3xl mx-auto fade-in">
            {/* Progress bar */}
            <ExtractionProgress status={status} progress={progress} message={progressMsg} />

            {/* Info grid */}
            <div className="mt-6 card">
              <p className="text-sm font-semibold text-gray-700 mb-4 flex items-center gap-2">
                <Sparkles size={15} className="text-primary-600" />
                What the AI is extracting for you…
              </p>
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                {extractionItems.map(({ icon: Icon, text }) => (
                  <div key={text} className="flex items-start gap-3 bg-primary-50 rounded-lg px-4 py-3">
                    <Icon size={16} className="text-primary-600 flex-shrink-0 mt-0.5" />
                    <span className="text-xs text-primary-800 font-medium leading-snug">{text}</span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}

        {/* ── Progress for non-extracting states ─────────────────────────── */}
        {status === "done" && (
          <div className="max-w-3xl mx-auto fade-in">
            <ExtractionProgress status={status} progress={progress} message={progressMsg} />
          </div>
        )}

        {/* ── Error ──────────────────────────────────────────────────────── */}
        {status === "error" && (
          <div className="max-w-3xl mx-auto fade-in">
            <ExtractionProgress status={status} progress={progress} message={progressMsg} />
            {error && (
              <div className="mt-4 card border-red-100 bg-red-50">
                <div className="flex gap-3">
                  <AlertTriangle size={18} className="text-red-500 flex-shrink-0 mt-0.5" />
                  <div>
                    <p className="text-sm font-semibold text-red-700 mb-1">Extraction Failed</p>
                    <p className="text-xs text-red-600 font-mono break-words">{error}</p>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── Extracted Fields ────────────────────────────────────────────── */}
        {result && status === "done" && (
          <div className="fade-in">
            <div className="flex items-center gap-2 mb-4 max-w-none">
              <CheckCircle size={18} className="text-green-600" />
              <h2 className="text-base font-semibold text-gray-800">Extracted Configuration — Review &amp; Verify</h2>
            </div>
            <ExtractedFields
              config={result}
              onSaved={(updated) => setResult(updated)}
            />
          </div>
        )}

      </main>
    </div>
  );
}
