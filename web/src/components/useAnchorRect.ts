import { useLayoutEffect, useState, type RefObject } from "react";

/**
 * T-0140 — sidebar-popover anti-clip helper.
 *
 * The sidebar (`.mc-sidebar`) scrolls with `overflow-y: auto`, which per the
 * CSS overflow spec also clips the cross axis. Any popover/tooltip wider than
 * the 240px rail (the global-busy panel, the autoupdate popover, the project &
 * server pickers) was therefore sliced off at the sidebar's right edge — the
 * stakeholder's "side-bar tooltips are cropped by side-bar edge" bug.
 *
 * Fix strategy = **reposition with `position: fixed`**. A fixed-positioned
 * element resolves against the viewport, not the scrolling overflow box, so it
 * escapes the clip — provided no ancestor establishes a fixed containing block
 * (transform / filter / will-change / contain). The sidebar is `position:
 * sticky`, which does NOT, so fixed children render free of the clip while
 * staying DOM children of their trigger (hover + click-outside logic untouched).
 *
 * This hook returns the trigger's current viewport rect while `open`, kept
 * fresh across scroll/resize, so each popover can compute its own fixed
 * top/left. Returns `null` when closed or before first measure.
 */
export function useAnchorRect(
  anchorRef: RefObject<HTMLElement | null>,
  open: boolean,
): DOMRect | null {
  const [rect, setRect] = useState<DOMRect | null>(null);

  useLayoutEffect(() => {
    if (!open) {
      setRect(null);
      return;
    }
    function update() {
      const el = anchorRef.current;
      if (el) setRect(el.getBoundingClientRect());
    }
    update();
    // capture-phase scroll so we react to ANY scrolling ancestor (the sidebar
    // itself, the page) — fixed coords must track the trigger as it moves.
    window.addEventListener("scroll", update, true);
    window.addEventListener("resize", update);
    return () => {
      window.removeEventListener("scroll", update, true);
      window.removeEventListener("resize", update);
    };
  }, [open, anchorRef]);

  return open ? rect : null;
}

/**
 * Compute a fixed-position style for a popover anchored below-left of its
 * trigger (the default for sidebar dropdowns/panels). `gap` is the vertical
 * offset below the trigger. Falls back to an off-screen hidden style until the
 * first rect is measured so the panel never flashes at 0,0.
 */
export function anchoredBelowLeft(
  rect: DOMRect | null,
  gap = 4,
): React.CSSProperties {
  if (!rect) return { position: "fixed", visibility: "hidden", top: 0, left: 0 };
  return { position: "fixed", top: rect.bottom + gap, left: rect.left };
}
