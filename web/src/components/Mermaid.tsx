import { useEffect, useRef, useState } from "react";

// T-0173: render a mermaid diagram. Mermaid is heavy (~MBs) and only needed on
// the Use Cases page when a flow is open, so we lazy-load it from a CDN at
// runtime via a dynamic import rather than bundling it. `@vite-ignore` stops
// Vite from trying to resolve the URL at build time. On any failure (offline,
// CDN down, bad syntax) we fall back to showing the raw mermaid source so the
// agent walking the flow still gets the text.
const MERMAID_CDN = "https://esm.sh/mermaid@11";

// Module-level cache: load mermaid once per page, reuse across diagrams.
let mermaidPromise: Promise<any> | null = null;
function loadMermaid(): Promise<any> {
  if (!mermaidPromise) {
    mermaidPromise = import(/* @vite-ignore */ MERMAID_CDN).then((m) => {
      const mermaid = m.default ?? m;
      mermaid.initialize({ startOnLoad: false, theme: "dark", securityLevel: "strict" });
      return mermaid;
    });
  }
  return mermaidPromise;
}

let _seq = 0;

export function Mermaid({ code }: { code: string }) {
  const [svg, setSvg] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  const idRef = useRef(`mmd-${++_seq}`);

  useEffect(() => {
    let cancelled = false;
    setSvg(null);
    setFailed(false);
    if (!code.trim()) return;
    loadMermaid()
      .then((mermaid) => mermaid.render(idRef.current, code))
      .then(({ svg }: { svg: string }) => {
        if (!cancelled) setSvg(svg);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [code]);

  if (failed) {
    return (
      <div>
        <div style={{ fontSize: "0.7rem", color: "var(--mc-text-dim)", marginBottom: "0.25rem" }}>
          (diagram render unavailable — showing source)
        </div>
        <pre className="mc-pre">{code}</pre>
      </div>
    );
  }
  if (svg === null) {
    return <div className="mc-loading">Rendering diagram…</div>;
  }
  return (
    <div
      className="mc-mermaid"
      style={{ overflowX: "auto" }}
      // mermaid output is sanitized (securityLevel: strict) before injection.
      dangerouslySetInnerHTML={{ __html: svg }}
    />
  );
}

// Extract the first ```mermaid fenced block from a markdown body, if any.
export function extractMermaid(body: string): string | null {
  const m = body.match(/```mermaid\s*\n([\s\S]*?)```/);
  return m ? m[1].trim() : null;
}
