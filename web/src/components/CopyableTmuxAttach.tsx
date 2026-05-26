import { useCallback, useEffect, useRef, useState } from "react";

export type CopyableTmuxAttachProps = {
  session: string;
  window?: string;
  // "sm" matches the inline density used in tables; "md" gives the strip a
  // little more breathing room. Default sm.
  size?: "sm" | "md";
  // Icon-only mode: skip the inline `<code>` echo of the command and render
  // just a small glyph button. Used inside the sessions table where the
  // full string would blow the row width (T-0098).
  iconOnly?: boolean;
  // Optional class on the wrapper for one-off layout tweaks.
  className?: string;
};

function commandFor(session: string, window?: string): string {
  const w = (window ?? "").trim();
  return w ? `tmux a -t ${session}:${w}` : `tmux a -t ${session}`;
}

export function CopyableTmuxAttach({
  session,
  window,
  size = "sm",
  iconOnly = false,
  className,
}: CopyableTmuxAttachProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);
  const cmd = commandFor(session, window);

  useEffect(() => {
    return () => {
      if (timer.current !== null) {
        globalThis.window?.clearTimeout(timer.current);
      }
    };
  }, []);

  const onCopy = useCallback(async () => {
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(cmd);
      } else {
        // Fallback for environments without the clipboard API: a hidden
        // textarea + execCommand. Best-effort — if this also fails we just
        // skip the success flash.
        const ta = document.createElement("textarea");
        ta.value = cmd;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-1000px";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
      if (timer.current !== null) globalThis.window?.clearTimeout(timer.current);
      timer.current = globalThis.window?.setTimeout(() => setCopied(false), 1500) ?? null;
    } catch {
      /* swallow — the user can still select the text manually */
    }
  }, [cmd]);

  const fontSize = size === "md" ? "0.82rem" : "0.74rem";
  const padY = size === "md" ? "0.25rem" : "0.15rem";

  const button = (
    <button
      type="button"
      onClick={onCopy}
      aria-label={`Copy ${cmd}`}
      title={copied ? "Copied!" : `Copy: ${cmd}`}
      style={{
        fontFamily: "var(--mc-mono)",
        fontSize,
        padding: iconOnly ? `${padY} 0.35rem` : `${padY} 0.45rem`,
        border: "1px solid var(--mc-border)",
        borderRadius: 2,
        background: copied ? "var(--mc-accent-ok)" : "var(--mc-surface-raised)",
        color: copied ? "var(--mc-surface-deep)" : "var(--mc-accent)",
        cursor: "pointer",
        transition: "background 120ms ease, color 120ms ease",
        lineHeight: 1,
      }}
    >
      {iconOnly
        ? (copied ? "✓" : "⎘")
        : (copied ? "✓ copied" : "copy")}
    </button>
  );

  if (iconOnly) {
    return (
      <span
        className={className}
        style={{
          display: "inline-flex",
          alignItems: "center",
          fontFamily: "var(--mc-mono)",
          fontSize,
        }}
      >
        {button}
      </span>
    );
  }

  return (
    <span
      className={className}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: "0.35rem",
        fontFamily: "var(--mc-mono)",
        fontSize,
      }}
    >
      <code
        title="tmux attach command"
        style={{
          padding: `${padY} 0.4rem`,
          background: "var(--mc-surface-deep)",
          color: "var(--mc-text)",
          border: "1px solid var(--mc-border)",
          borderRadius: 2,
          fontFamily: "var(--mc-mono)",
          fontSize,
          whiteSpace: "nowrap",
        }}
      >
        {cmd}
      </code>
      {button}
    </span>
  );
}
