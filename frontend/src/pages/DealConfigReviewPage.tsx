import { useCallback, useEffect, useState } from "react";
import { ClipboardList, RefreshCw, Trash2 } from "lucide-react";
import Header from "../components/layout/Header";
import LoadingSpinner from "../components/shared/LoadingSpinner";
import ExtractedFields from "../components/extraction/ExtractedFields";
import { deleteDealConfig, getDeal, listDeals, updateDealConfig } from "../services/api";
import toast from "react-hot-toast";
import type { DealConfig } from "../types";

interface DealSummary {
  deal_id: string;
  deal_name?: string;
  asset_type?: string;
  manually_verified?: boolean;
}

const toSummaries = (payload: unknown): DealSummary[] => {
  const raw =
    Array.isArray(payload)
      ? payload
      : (payload as { deals?: unknown[]; deal_ids?: unknown[] })?.deals
      ?? (payload as { deals?: unknown[]; deal_ids?: unknown[] })?.deal_ids
      ?? [];

  return raw
    .map((item) => (typeof item === "string" ? { deal_id: item } : item as DealSummary))
    .filter((item) => !!item.deal_id);
};

export default function DealConfigReviewPage() {
  const [dealSummaries, setDealSummaries] = useState<DealSummary[]>([]);
  const [selectedDeal, setSelectedDeal] = useState("");
  const [config, setConfig] = useState<DealConfig | null>(null);
  const [loading, setLoading] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const loadDeals = useCallback(async (preferredDealId?: string) => {
    try {
      const data = await listDeals();
      const summaries = toSummaries(data);
      setDealSummaries(summaries);

      const preferred = preferredDealId ?? selectedDeal;
      if (preferred && summaries.some((s) => s.deal_id === preferred)) {
        setSelectedDeal(preferred);
        return;
      }

      if (summaries.length === 1) {
        setSelectedDeal(summaries[0].deal_id);
      } else if (preferred) {
        setSelectedDeal("");
      }
    } catch {
      toast.error("Failed to load deal list");
    }
  }, [selectedDeal]);

  useEffect(() => {
    loadDeals();
  }, [loadDeals]);

  useEffect(() => {
    if (!selectedDeal) {
      setConfig(null);
      return;
    }

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
    getDeal(selectedDeal)
      .then((d) => setConfig(d as DealConfig))
      .catch(() => toast.error("Refresh failed"))
      .finally(() => setLoading(false));
  };

  const persistConfig = async (nextConfig: DealConfig): Promise<DealConfig> => {
    await updateDealConfig(nextConfig.deal_id, nextConfig);
    const latest = await getDeal(nextConfig.deal_id);
    return latest as DealConfig;
  };

  const handleDeleteDeal = async () => {
    if (!selectedDeal || deleting) return;
    const ok = window.confirm(
      `Delete deal configuration '${selectedDeal}'?\n\nThis removes the saved config used for waterfall calculations.`,
    );
    if (!ok) return;

    setDeleting(true);
    try {
      await deleteDealConfig(selectedDeal);
      toast.success(`Deleted ${selectedDeal}`);
      setConfig(null);
      setSelectedDeal("");
      await loadDeals();
    } catch {
      toast.error("Failed to delete deal configuration");
    } finally {
      setDeleting(false);
    }
  };

  return (
    <div className="flex flex-col h-full overflow-hidden">
      <Header
        title="Deal Config Review"
        subtitle="Edit and save full deal configurations used by waterfall calculations"
      />

      <main className="flex-1 overflow-y-auto p-6 space-y-5">
        <div className="card">
          <div className="flex flex-wrap items-end gap-4">
            <div className="flex-1 min-w-[220px]">
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
                    {s.deal_id}
                    {s.deal_name ? ` — ${s.deal_name}` : ""}
                    {s.manually_verified ? " ✓" : " (draft)"}
                  </option>
                ))}
              </select>
            </div>

            {selectedDeal && (
              <div className="flex items-center gap-2">
                <button
                  className="btn-secondary h-[38px]"
                  onClick={refresh}
                  disabled={loading}
                  title="Refresh"
                >
                  <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
                  Refresh
                </button>
                <button
                  className="h-[38px] px-3 rounded-lg border border-red-200 text-red-600 hover:bg-red-50 text-sm font-medium transition-colors disabled:opacity-50"
                  onClick={handleDeleteDeal}
                  disabled={deleting || loading}
                  title="Delete deal config"
                >
                  <span className="flex items-center gap-1.5">
                    <Trash2 size={14} />
                    {deleting ? "Deleting..." : "Delete Deal"}
                  </span>
                </button>
              </div>
            )}
          </div>
        </div>

        {loading && (
          <div className="card flex items-center justify-center py-12">
            <LoadingSpinner text={`Loading ${selectedDeal} config...`} />
          </div>
        )}

        {!loading && dealSummaries.length === 0 && (
          <div className="card text-center py-12 text-gray-400">
            <ClipboardList size={36} className="mx-auto mb-3 opacity-40" />
            <p className="font-medium">No deal configs saved yet.</p>
            <p className="text-xs mt-1">Extract a deal indenture PDF from the Deal Indenture tab first.</p>
          </div>
        )}

        {!loading && !config && dealSummaries.length > 0 && !selectedDeal && (
          <div className="card text-center py-10 text-gray-500">
            Select a deal to review and edit its configuration.
          </div>
        )}

        {config && !loading && (
          <div className="fade-in">
            <ExtractedFields
              key={`${config.deal_id}:${config.updated_at ?? "draft"}`}
              config={config}
              showPdfPanel={false}
              saveSuccessMessage="Deal configuration saved."
              onPersist={persistConfig}
              onSaved={(updated) => {
                setConfig(updated);
                setDealSummaries((prev) => prev.map((s) =>
                  s.deal_id === updated.deal_id
                    ? {
                      ...s,
                      deal_name: updated.deal_name,
                      asset_type: updated.asset_type,
                      manually_verified: updated.manually_verified,
                    }
                    : s));
              }}
            />
          </div>
        )}
      </main>
    </div>
  );
}
