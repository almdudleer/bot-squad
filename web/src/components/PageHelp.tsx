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
      <button
        type="button"
        className="mc-page-help-toggle"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        title={open ? "Hide page help" : "What is this page?"}
      >
        <span className="mc-page-help-icon">?</span>
        {open ? "hide page help" : "what is this page?"}
      </button>
      {open && <div className="mc-page-help">{children}</div>}
    </div>
  );
}
