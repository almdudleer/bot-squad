import { lazy, Suspense } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  Outlet,
  useLocation,
  useParams,
} from "react-router-dom";
import { Login } from "./pages/Login";
import { HomeRedirect } from "./pages/HomeRedirect";
import { Project } from "./pages/Project";
import { ProjectSettings } from "./pages/ProjectSettings";
import { Vision } from "./pages/Vision";
import { Feedback } from "./pages/Feedback";
import { UseCases } from "./pages/UseCases";
import { Docs } from "./pages/Docs";
import { DocsSection } from "./pages/DocsSection";
import { Sessions } from "./pages/Sessions";
import { Analytics } from "./pages/Analytics";
import { TaskDetail } from "./pages/TaskDetail";
import { Runs } from "./pages/Runs";
import { RunLog } from "./pages/RunLog";
import { Messages } from "./pages/Messages";
import { Scheduler } from "./pages/Scheduler";
import { Autonomous } from "./pages/Autonomous";
import { Clones } from "./pages/Clones";
import { Workflow } from "./pages/Workflow";
import { Help } from "./pages/Help";
import { Welcome } from "./pages/Welcome";
import { Users } from "./pages/Users";
import { SystemSettings } from "./pages/SystemSettings";
import { Profile } from "./pages/Profile";
import { AttachmentTgBinding } from "./pages/AttachmentTgBinding";
import { AttachmentSessions } from "./pages/AttachmentSessions";
import { AttachmentWorker } from "./pages/AttachmentWorker";
import { Shell } from "./components/Shell";

// Mothership centralization layer — docs/architecture/D-0017-mothership-seam.md.
// Vite inlines VITE_MOTHERSHIP at build time, so the dynamic imports resolve
// to literal `null` on single-install builds and the chunks are tree-shaken.
const MOTHERSHIP_ENABLED = import.meta.env.VITE_MOTHERSHIP === "1";
const MothershipRoutes = MOTHERSHIP_ENABLED
  ? lazy(() => import("./mothership/routes"))
  : null;
// T-0336 (reframe-operator-paradigm): `/` no longer renders the cross-server
// AllProjects console. bot-squad reads as a single project brain — `/` lands
// on the project's process/Task-Manager view (HomeRedirect). The mothership
// AllProjects console moved to the admin `/m/` index (mothership/routes.tsx),
// reachable via the super-admin FLEET sidebar entry. (Was T-0025: `/` ===
// AllProjects on mothership builds.)

// T-0189: legacy slug redirects. The `signal-tracker` project was renamed to
// `watchrobot` (the public brand — repo, staging domain, home dir all moved).
// Old `/p/signal-tracker/*` deep-links and bookmarks redirect (replace, so the
// stale URL doesn't linger in history) to `/p/watchrobot/*` so nothing breaks
// during the cutover. SAFE TO REMOVE after ~2026-07-03 (≈30 days) once stale
// links have aged out — delete this map and inline the project routes back
// under a plain `/p/:slug` parent (drop the SlugAliasGuard layer).
const LEGACY_SLUG_ALIASES: Record<string, string> = {
  "signal-tracker": "watchrobot",
};

// Layout route at `/p/:slug` — sees the resolved slug at ANY depth, so a
// legacy slug redirects whether the URL is the bare project, a tab, or a deep
// task link. (A flat `/p/<from>/*` splat route does NOT work: React Router
// ranks the splat below the concrete `/p/:slug/<tab>` routes, so subpaths
// slip past it — caught by the T-0189 manual walkthrough.)
function SlugAliasGuard() {
  const { slug } = useParams();
  const location = useLocation();
  const alias = slug ? LEGACY_SLUG_ALIASES[slug] : undefined;
  if (alias) {
    const target =
      location.pathname.replace(`/p/${slug}`, `/p/${alias}`) +
      location.search +
      location.hash;
    return <Navigate to={target} replace />;
  }
  return <Outlet />;
}

// T-0235: legacy top-level /p/:slug/feedback + /usecases deep-links redirect
// into the relocated docs section (/p/:slug/docs/<sub>). Absolute target so
// resolution doesn't depend on relative route nesting.
function LegacyDocsRedirect({ sub }: { sub: string }) {
  const { slug = "" } = useParams();
  return <Navigate to={`/p/${slug}/docs/${sub}`} replace />;
}

export function App() {
  return (
    <BrowserRouter>
      <Routes>
        {/* Login is the only route fully outside the sidebar shell */}
        <Route path="/login" element={<Login />} />

        {/* Everything else inside the Shell sidebar layout */}
        <Route element={<Shell />}>
          <Route path="/" element={<HomeRedirect />} />
          <Route path="/help" element={<Help />} />
          <Route path="/welcome" element={<Welcome />} />
          {/* T-0189: project routes nest under SlugAliasGuard so a legacy
              slug (signal-tracker → watchrobot) redirects at ANY depth. The
              child paths are relative to `/p/:slug`. */}
          <Route path="/p/:slug" element={<SlugAliasGuard />}>
            <Route index element={<Project />} />
            <Route path="t/:id" element={<TaskDetail />} />
            <Route path="vision" element={<Vision />} />
            <Route path="workflow" element={<Workflow />} />
            {/* T-0235 (Pillar C): User Feedback + Use Cases relocated UNDER
                the docs section (retired as top-level nav). T-0337 then merged
                the three former tabs into ONE "Docs & Artifacts" view: the
                DocsSection wrapper owns the shared rail (type filter + one
                "+ New" + the cross-store tree) and hands its tree to whichever
                detail page the sub-route resolves (index=Docs, feedback, usecases)
                via Outlet context. Old top-level /feedback + /usecases deep-links
                still redirect in (LegacyDocsRedirect). */}
            <Route path="docs" element={<DocsSection />}>
              <Route index element={<Docs />} />
              <Route path="feedback" element={<Feedback />} />
              <Route path="usecases" element={<UseCases />} />
            </Route>
            <Route path="feedback" element={<LegacyDocsRedirect sub="feedback" />} />
            <Route path="usecases" element={<LegacyDocsRedirect sub="usecases" />} />
            <Route path="sessions" element={<Sessions />} />
            <Route path="settings" element={<ProjectSettings />} />
            <Route path="analytics" element={<Analytics />} />
            <Route path="runs" element={<Runs />} />
            <Route path="runs/:id" element={<RunLog />} />
            <Route path="sessions/:claude_uuid/messages" element={<Messages />} />
            <Route path="autonomous" element={<Autonomous />} />
            {/* T-0296: per-project clone health ("Installation ≠ Project"). */}
            <Route path="clones" element={<Clones />} />
          </Route>
          <Route path="/scheduler" element={<Scheduler />} />
          <Route path="/users" element={<Users />} />
          <Route path="/system-settings" element={<SystemSettings />} />
          <Route path="/me" element={<Profile />} />
          {/* T-0061: ATTACHMENT-scoped (per-user-per-server) pages.
              (T-0224: dropped the stale pointer to the deleted
              sidebarHelpers ATTACHMENT_SIDEBAR_ITEMS model.) */}
          <Route path="/attachment/tg-binding" element={<AttachmentTgBinding />} />
          <Route path="/attachment/sessions" element={<AttachmentSessions />} />
          <Route path="/attachment/worker" element={<AttachmentWorker />} />
          {MothershipRoutes && (
            <Route
              path="/m/*"
              element={
                <Suspense fallback={<div className="mc-loading">Loading mothership…</div>}>
                  <MothershipRoutes />
                </Suspense>
              }
            />
          )}
        </Route>

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}
