import { useEffect, useRef, useState } from "react";

// T-0141: collapse the per-row action button cluster behind a single
// kebab/overflow button so session rows stay short (≤48px). The old layout
// rendered up to four `btn`s in a `flex-wrap` div which wrapped to a second
// line on narrow viewports and blew the row height (stakeholder note 10).

export type RowAction = {
  label: string;
  onClick: () => void;
  variant?: "default" | "warning" | "danger" | "success";
  disabled?: boolean;
  title?: string;
};

function variantColor(variant: RowAction["variant"]): string {
  switch (variant) {
    case "warning":
      return "var(--mc-amber, #fbbf24)";
    case "danger":
      return "var(--mc-accent-danger, #f87171)";
    case "success":
      return "var(--mc-accent-success, #4ade80)";
    default:
      return "var(--mc-text)";
  }
}

export function RowActionsMenu({
  actions,
  ariaLabel = "Row actions",
}: {
  actions: RowAction[];
  ariaLabel?: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    function onDoc(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (actions.length === 0) return null;

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block" }}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        // T-0161: styling lives in `.mc-kebab-trigger` (missioncontrol.css) so a
        // media query can grow the hit target to ≥44×44 on phone widths while
        // keeping the compact look on desktop — an inline style can't carry a
        // media query, and the old inline padding rendered a 33×20px box that
        // was hard to find/tap on a narrow viewport.
        className="btn btn-outline-secondary btn-sm mc-kebab-trigger"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        ⋯
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            right: 0,
            top: "100%",
            zIndex: 30,
            minWidth: "9.5rem",
            background: "var(--mc-surface-raised)",
            border: "1px solid var(--mc-border)",
            borderRadius: 4,
            boxShadow: "0 6px 20px rgba(0,0,0,0.4)",
            padding: "0.25rem",
            marginTop: "0.2rem",
          }}
        >
          {actions.map((a, i) => (
            <button
              key={`${a.label}-${i}`}
              type="button"
              role="menuitem"
              disabled={a.disabled}
              title={a.title}
              onClick={(e) => {
                e.stopPropagation();
                if (a.disabled) return;
                setOpen(false);
                a.onClick();
              }}
              style={{
                display: "block",
                width: "100%",
                textAlign: "left",
                background: "none",
                border: "none",
                color: variantColor(a.variant),
                fontSize: "0.78rem",
                padding: "0.32rem 0.55rem",
                cursor: a.disabled ? "not-allowed" : "pointer",
                opacity: a.disabled ? 0.45 : 1,
                borderRadius: 2,
                whiteSpace: "nowrap",
              }}
            >
              {a.label}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
