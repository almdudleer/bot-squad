import { useState } from "react";
import { Markdown } from "./Markdown";

// T-0737: the ONE way a stored markdown body gets put on screen.
//
// T-0733 built this treatment (boxed, markdown-rendered, long walls collapsed)
// for legacy ticket bodies on TaskDetail. Vision kept dumping the same content
// class — an initiative's stored md body — into a raw `<pre>`, so `## Origin`
// showed up literally one page over from where it now renders. Two surfaces
// disagreeing about whether a md body gets rendered is the same
// two-places-encode-one-decision drift as T-0714/T-0719/T-0729/T-0731, so the
// treatment moved HERE and both callers point at it. Add a third surface by
// importing this, never by re-deriving it.
//
// Rendering itself is `Markdown` (T-0275): react-markdown + GFM, no raw HTML,
// frontmatter stripped, T-/D- mentions linkified.

// A body longer than this collapses behind an expander when the caller asks for
// it. The live offenders on both surfaces are the same order of magnitude —
// 7.6k–11.4k-char legacy tickets (T-0553/T-0555/T-0558), 3.8k–20.2k-char
// initiative bodies (T-0551 is the biggest) — thousands of px of wall. Anything
// under the threshold reads fine in place and never gets a control it doesn't
// need.
export const BODY_COLLAPSE_CHARS = 1200;

export function MarkdownBody({
  source,
  slug,
  // Opt-in, because "long" is not the same judgement on every surface. A
  // 20k-char initiative body buries the page; Vision's product description
  // (~1.4k) is the page's lede and would trip the threshold for ~200 chars of
  // overflow — putting the product blurb behind a click is the opposite of the
  // observability this page exists for. Callers decide; the threshold doesn't
  // move.
  collapsible = false,
}: {
  source: string | null | undefined;
  slug?: string;
  collapsible?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const text = source?.trim() ?? "";
  const canCollapse = collapsible && text.length > BODY_COLLAPSE_CHARS;
  const collapsed = canCollapse && !expanded;

  return (
    <>
      <div
        className="mc-md-body"
        style={collapsed ? { maxHeight: "18rem", overflow: "hidden" } : undefined}
      >
        <Markdown source={text} slug={slug} />
        {collapsed && <div className="mc-md-body-fade" />}
      </div>
      {canCollapse && (
        <button
          type="button"
          className="btn btn-sm btn-outline-secondary mt-2"
          style={{ fontFamily: "var(--mc-mono)", fontSize: "0.7rem" }}
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded
            ? "Collapse body"
            : `Show full body (${text.length.toLocaleString()} chars)`}
        </button>
      )}
    </>
  );
}
