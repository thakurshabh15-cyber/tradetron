import React from "react";
import { AlertTriangle, RefreshCw, Home } from "lucide-react";

export default class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    this.state = { hasError: false, error: null, errorInfo: null };
  }

  static getDerivedStateFromError(error) {
    return { hasError: true, error };
  }

  componentDidCatch(error, errorInfo) {
    console.error("ErrorBoundary caught runtime error:", error, errorInfo);
    this.setState({ error, errorInfo });
  }

  handleReset = () => {
    this.setState({ hasError: false, error: null, errorInfo: null });
    window.location.reload();
  };

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      return (
        <div className="min-h-[350px] flex flex-col items-center justify-center p-6 text-center glass-card border border-red-500/30 rounded-2xl m-4 bg-slate-900/95 shadow-glass-lg">
          <div className="p-3 rounded-full bg-red-500/10 border border-red-500/30 text-red-400 mb-3 animate-pulse">
            <AlertTriangle size={30} />
          </div>
          <h2 className="text-base font-bold text-white mb-1">Terminal Component Recovered</h2>
          <p className="text-xs text-slate-400 max-w-md mb-4">
            A temporary component error was isolated safely. The background execution engine and broker connection remain active.
          </p>

          {this.state.error && (
            <div className="w-full max-w-lg p-3 mb-4 rounded-lg bg-slate-950 border border-slate-800 text-[11px] font-mono text-red-300 text-left overflow-x-auto max-h-28">
              {this.state.error.toString()}
            </div>
          )}

          <div className="flex items-center gap-3">
            <button
              onClick={this.handleReset}
              className="btn-primary text-xs flex items-center gap-1.5 px-4 py-2 cursor-pointer"
            >
              <RefreshCw size={14} /> Reload Terminal
            </button>
            <a
              href="/"
              onClick={() => this.setState({ hasError: false })}
              className="btn-ghost text-xs flex items-center gap-1.5 px-4 py-2"
            >
              <Home size={14} /> Reset View
            </a>
          </div>
        </div>
      );
    }

    return this.props.children;
  }
}
