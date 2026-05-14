import { useEffect, useState } from "react";

export type TypewriterProps = {
  text: string;
  // Total animation budget; the per-char interval is derived so any string of
  // typical spotlight length finishes inside this window. Capped at 1.5s per
  // T-0014 DoD.
  durationMs?: number;
  className?: string;
  // Rendered after the typewriter finishes (and immediately in reduced-motion
  // mode). Lets the spotlight tuck a deep-link beneath the typed line without
  // having the link blink in mid-animation.
  trailing?: React.ReactNode;
};

const DEFAULT_DURATION_MS = 1200;
const MIN_INTERVAL_MS = 18;

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || !window.matchMedia) return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function Typewriter({
  text,
  durationMs = DEFAULT_DURATION_MS,
  className,
  trailing,
}: TypewriterProps) {
  const reduced = prefersReducedMotion();
  const [shown, setShown] = useState<number>(reduced ? text.length : 0);

  useEffect(() => {
    if (reduced) {
      setShown(text.length);
      return;
    }
    setShown(0);
    const interval = Math.max(MIN_INTERVAL_MS, Math.floor(durationMs / Math.max(text.length, 1)));
    let i = 0;
    const id = window.setInterval(() => {
      i += 1;
      setShown(i);
      if (i >= text.length) window.clearInterval(id);
    }, interval);
    return () => window.clearInterval(id);
  }, [text, durationMs, reduced]);

  const done = shown >= text.length;
  return (
    <span className={className}>
      <span aria-live="polite">{text.slice(0, shown)}</span>
      {!done && <span className="bs-typewriter-caret" aria-hidden="true">▍</span>}
      {done && trailing}
    </span>
  );
}
