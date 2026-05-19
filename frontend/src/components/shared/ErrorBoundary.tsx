import { Component, type ReactNode } from "react";
import { AlertTriangle, RefreshCw } from "lucide-react";

interface Props { children: ReactNode; }
interface State { error: Error | null; }

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex flex-col items-center justify-center min-h-[60vh] gap-4 p-8">
          <div className="w-14 h-14 bg-red-50 rounded-full flex items-center justify-center">
            <AlertTriangle size={24} className="text-red-500" />
          </div>
          <div className="text-center">
            <h3 className="text-base font-semibold text-gray-800 mb-1">Something went wrong</h3>
            <p className="text-sm text-gray-500 font-mono bg-gray-50 px-3 py-2 rounded-lg max-w-lg break-words">
              {this.state.error.message}
            </p>
          </div>
          <button
            className="btn-primary"
            onClick={() => { this.setState({ error: null }); window.location.reload(); }}
          >
            <RefreshCw size={14} /> Reload Page
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
