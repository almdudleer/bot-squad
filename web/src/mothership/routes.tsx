import { Routes, Route } from "react-router-dom";

/**
 * Mothership centralization-layer routes. Mounted under /m/* in App.tsx
 * only when import.meta.env.VITE_MOTHERSHIP === "1" (Vite inlines that
 * literal at build time, so this module is tree-shaken from the single-
 * install bundle).
 *
 * SCAFFOLD only — sibling tasks land the real surface:
 *   T-0024 → /m/servers/add wizard (install-token issuance + checkpoint stream)
 *   T-0025 → cross-server all-projects view (also swaps the "/" element
 *            so the wordmark-click landing page on the mothership is the
 *            cross-server picker, not the local-only one)
 *   T-0023 → per-server backend client used by the above
 *
 * Contract: vision/architecture/mothership-seam.md.
 */
function Placeholder({ which }: { which: string }) {
  return (
    <div className="container py-4" style={{ maxWidth: "900px" }}>
      <div className="mc-section-title">Mothership · {which}</div>
      <div className="mc-empty">
        <div className="mc-empty-icon">◇</div>
        <div>Centralization-layer scaffold. T-0023 / T-0024 / T-0025 land here.</div>
      </div>
    </div>
  );
}

export default function MothershipRoutes() {
  return (
    <Routes>
      <Route path="servers" element={<Placeholder which="servers" />} />
      <Route path="servers/add" element={<Placeholder which="add server" />} />
      <Route path="*" element={<Placeholder which="placeholder" />} />
    </Routes>
  );
}
