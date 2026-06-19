import { NavLink, Outlet, useParams } from "react-router-dom";

// T-0235 (Pillar C): the Docs SECTION wrapper. The paradigm reframe folds the
// former top-level "User Feedback" + "Use Cases" nav entries INTO docs — they
// are project artifacts, not first-class nav. This wrapper renders a sub-nav
// (Docs · User Feedback · Use Cases) above an <Outlet> so the three live under
// one /p/:slug/docs surface. The individual pages (Docs / Feedback / UseCases)
// render unchanged inside the outlet; the old top-level routes redirect here.
export function DocsSection() {
  const { slug = "" } = useParams();
  const base = `/p/${slug}/docs`;
  const tab = (to: string, label: string, end = false) => (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `btn btn-sm ${isActive ? "btn-secondary" : "btn-outline-secondary"}`
      }
      style={{ fontSize: "0.74rem", padding: "0.2rem 0.7rem" }}
    >
      {label}
    </NavLink>
  );
  return (
    <div>
      <div
        className="d-flex align-items-center gap-2 px-4 pt-3"
        style={{ flexWrap: "wrap" }}
      >
        <span
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.7rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            marginRight: "0.3rem",
          }}
        >
          Docs &amp; artifacts:
        </span>
        <div className="btn-group btn-group-sm" role="group" aria-label="Docs section">
          {tab(base, "Docs", true)}
          {tab(`${base}/feedback`, "User Feedback")}
          {tab(`${base}/usecases`, "Use Cases")}
        </div>
      </div>
      <Outlet />
    </div>
  );
}
