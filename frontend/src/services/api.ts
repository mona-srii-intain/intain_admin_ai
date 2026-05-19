import axios from "axios";

const BASE = "http://localhost:8000";

const api = axios.create({ baseURL: BASE });

// ─── Health ─────────────────────────────────────────────────────────────────
export const checkHealth = () => api.get("/health").then((r) => r.data);

// ─── Deal Extraction ─────────────────────────────────────────────────────────
export const extractDeal = (file: File, dealId: string, overwrite = false) => {
  const form = new FormData();
  form.append("file", file);
  form.append("deal_id", dealId);
  form.append("overwrite", String(overwrite));
  return api.post("/api/deals/extract", form, {
    headers: { "Content-Type": "multipart/form-data" },
    timeout: 300_000,
  }).then((r) => r.data);
};

export const submitReview = (payload: {
  deal_id: string;
  reviewed_config: object;
  corrections: object[];
  reviewer_name?: string;
  notes?: string;
}) => api.post("/api/deals/review", payload).then((r) => r.data);

export const getAuditAnnotations = (dealId: string) =>
  api.get(`/api/deals/${dealId}/audit`).then((r) => r.data);

export const addAuditEntry = (dealId: string, payload: { row_key: string; sender: string; content: string }) =>
  api.post(`/api/deals/${dealId}/audit/entry`, payload).then((r) => r.data);

// Source PDF used during extraction. Returned by the backend as application/pdf.
export const dealPdfUrl = (dealId: string) => `${BASE}/api/deals/${dealId}/pdf`;

export const getSectionPages = (dealId: string) =>
  api.get(`/api/deals/${dealId}/section-pages`).then((r) => r.data as {
    deal_id: string;
    section_page_map: Record<string, number[]>;
    has_pdf: boolean;
  });

// ─── Deal Config ─────────────────────────────────────────────────────────────
export const listDeals = () =>
  api.get("/api/deals").then((r) => r.data);

export const getDeal = (dealId: string) =>
  api.get(`/api/deals/${dealId}`).then((r) => r.data);

// ─── Loantape ────────────────────────────────────────────────────────────────
export const listLoanDeals = () =>
  api.get("/api/loantape/deals").then((r) => r.data);

export const getPaymentDates = (dealId: string) =>
  api.get(`/api/loantape/${dealId}/payment-dates`).then((r) => r.data);

export const getLoanSummary = (dealId: string, paymentDate: string) =>
  api.get(`/api/loantape/${dealId}/summary`, { params: { payment_date: paymentDate } }).then((r) => r.data);

export const getDelinquencyHistory = (dealId: string) =>
  api.get(`/api/loantape/${dealId}/delinquency-history`).then((r) => r.data);

// ─── Waterfall ───────────────────────────────────────────────────────────────
export const computeWaterfall = (payload: {
  deal_id: string;
  payment_date: string;
  sofr_rate?: number;
  override_beginning_balances?: Record<string, number>;
  notes?: string;
}) => api.post("/api/waterfall/compute", payload, { timeout: 120_000 }).then((r) => r.data);

export const getWaterfallResult = (dealId: string, paymentDate: string) =>
  api.get(`/api/waterfall/${dealId}/${paymentDate}`).then((r) => r.data);

export const listWaterfallResults = (dealId: string) =>
  api.get(`/api/waterfall/${dealId}`).then((r) => r.data);

// ─── Reports ─────────────────────────────────────────────────────────────────
export const generateReport = (dealId: string, paymentDate: string) =>
  api.post("/api/reports/generate", { deal_id: dealId, payment_date: paymentDate }).then((r) => r.data);

export const getReport = (dealId: string, paymentDate: string) =>
  api.get(`/api/reports/${dealId}/${paymentDate}`).then((r) => r.data);

export const downloadReport = (dealId: string, paymentDate: string) =>
  `${BASE}/api/reports/${dealId}/${paymentDate}/download`;

export const listReports = () =>
  api.get("/api/reports").then((r) => r.data);
