import { Component, type ReactNode } from "react";

/**
 * T-0602 (T-0588d, F3) — minimal app-level error boundary.
 *
 * One class component (React has no hook for this) wrapping the route tree in
 * App.tsx, so a thrown render error anywhere shows a plain "something broke +
 * reload" card instead of a white screen. No per-route boundaries, no
 * telemetry — minimal by design.
 */

/**
 * The fallback card. Split out so it can be render-tested without triggering
 * a real error (SSR renderers don't support error boundaries).
 */
export function ErrorFallback({ error }: { error: unknown }) {
  return (
    <div className="container py-4">
      <div className="alert alert-danger">
        <div style={{ fontWeight: 600, marginBottom: "0.35rem" }}>
          Something broke in the UI.
        </div>
        <div
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.78rem",
            marginBottom: "0.75rem",
          }}
        >
          {String(error)}
        </div>
        <button
          type="button"
          className="btn btn-sm btn-outline-secondary"
          onClick={() => window.location.reload()}
        >
          Reload
        </button>
      </div>
    </div>
  );
}

type State = { error: unknown | null };

export class ErrorBoundary extends Component<{ children: ReactNode }, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: unknown): State {
    return { error };
  }

  render() {
    if (this.state.error !== null) return <ErrorFallback error={this.state.error} />;
    return this.props.children;
  }
}
