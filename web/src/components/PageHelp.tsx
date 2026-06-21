import { ReactNode, useState } from "react";

/**
 * PageHelp — collapsible explainer for what a page does and how to use it.
 *
 * Renders a small toggle button by default. Click to reveal the panel.
 * The toggle sits in the page flow so it doesn't fight with the page title.
 */
export function PageHelp({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mc-page-help-wrap">
      {/* T-0366 #1: icon-only — the permanent "what is this page?" text link was
          onboarding clutter for a long-active operator. Keep the ? affordance
          (tooltip + aria-label carry the meaning); the panel still toggles. */}
      <button
        type="button"
        className="mc-page-help-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label={open ? "Hide page help" : "What is this page?"}
        title={open ? "Hide page help" : "What is this page?"}
      >
        <span className="mc-page-help-icon">?</span>
      </button>
      {open && <div className="mc-page-help">{children}</div>}
    </div>
  );
}
