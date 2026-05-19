import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "react-hot-toast";
import Sidebar from "./components/layout/Sidebar";
import DealExtractionPage from "./pages/DealExtractionPage";
import DealConfigReviewPage from "./pages/DealConfigReviewPage";
import LoanTapePage from "./pages/LoanTapePage";
import ReportsPage from "./pages/ReportsPage";
import ErrorBoundary from "./components/shared/ErrorBoundary";

const queryClient = new QueryClient();

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <div className="flex h-full bg-surface overflow-hidden">
          <Sidebar />

          {/* Main content — offset for sidebar, fills remaining height */}
          <div className="flex-1 ml-16 lg:ml-60 flex flex-col h-full overflow-hidden">
            <Routes>
              <Route path="/" element={<Navigate to="/extraction" replace />} />
              <Route path="/extraction"    element={<ErrorBoundary><DealExtractionPage /></ErrorBoundary>} />
              <Route path="/config-review" element={<ErrorBoundary><DealConfigReviewPage /></ErrorBoundary>} />
              <Route path="/loantape"      element={<ErrorBoundary><LoanTapePage /></ErrorBoundary>} />
              <Route path="/reports"       element={<ErrorBoundary><ReportsPage /></ErrorBoundary>} />
            </Routes>
          </div>
        </div>

        <Toaster
          position="top-right"
          toastOptions={{
            duration: 4000,
            style: {
              background: "#fff",
              color: "#1a1a2e",
              border: "1px solid #e5e7eb",
              borderRadius: "10px",
              fontSize: "13px",
              fontWeight: "500",
              boxShadow: "0 4px 24px rgba(0,0,0,0.10)",
            },
            success: {
              iconTheme: { primary: "#1B5E45", secondary: "#fff" },
            },
            error: {
              iconTheme: { primary: "#dc2626", secondary: "#fff" },
            },
          }}
        />
      </BrowserRouter>
    </QueryClientProvider>
  );
}
