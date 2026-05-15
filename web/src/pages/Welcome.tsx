import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";

import { api } from "../api";
import { CopyableTmuxAttach } from "../components/CopyableTmuxAttach";

/** The path the primary "Next" button routes to. Exported for unit tests
 *  so a future re-route (e.g. to a project picker variant) can't silently
 *  diverge from the §8 spec. */
export const WELCOME_NEXT_PATH = "/";

/** Normalise an operator-session API response into a non-empty session
 *  name, or `null` if the server returned something unusable. Pure for
 *  unit-testing without the fetch. */
export function pickOperatorSession(
  resp: { session?: unknown } | null | undefined,
): string | null {
  if (!resp || typeof resp.session !== "string") return null;
  const trimmed = resp.session.trim();
  return trimmed.length === 0 ? null : trimmed;
}

/**
 * /welcome — Post-install "you're all set" handoff (T-0013, Chapter I §8).
 *
 * Reached by:
 *   - the shell installer, which now points the final UI URL at `/welcome`
 *     (scripts/install/install.sh: step_print_attach).
 *   - the mothership add-server flow, which redirects from `/m/servers/:id`
 *     to the target server's `/welcome` once the final checkpoint lands.
 *
 * Behaviour:
 *   - resolves the operator tmux session name SERVER-SIDE
 *     (`GET /api/welcome/operator`) so the UI never has to guess.
 *   - renders the copyable `tmux a -t <operator-session>` command via
 *     the shared `CopyableTmuxAttach` widget (T-0006).
 *   - primary "Next" button routes to `/` (the server-view Picker, or the
 *     mothership AllProjects view when VITE_MOTHERSHIP=1).
 *
 * Re-readability: the page does NOT trigger any onboarding (T-0012/T-0014
 * own §9 onboarding). Visiting it after first-time render is fine.
 */
export function Welcome() {
  const navigate = useNavigate();
  const [session, setSession] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .welcomeOperator()
      .then((r) => {
        if (cancelled) return;
        const name = pickOperatorSession(r);
        if (name) setSession(name);
        else setError("server returned an empty operator session name");
      })
      .catch((e: unknown) => {
        if (!cancelled) {
          setError(e instanceof Error ? e.message : String(e));
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="mc-help-page" data-testid="welcome-page">
      <div className="mc-help-hero">
        <h1>YOU'RE ALL SET</h1>
        <p>
          bot-squad is installed on this server and the operator tmux session
          is running. Most day-to-day operations go through the operator —
          attach from any ssh session, talk to it like you would any
          claude-code instance. The UI you're looking at now is for the things
          that are easier as clicks than as prose: browsing the backlog,
          tweaking task priorities, watching session activity at a glance.
        </p>
      </div>

      <section className="mc-help-section" id="attach">
        <h2>Attach to the operator</h2>
        <p>
          From any ssh session on this host, run:
        </p>
        <div style={{ margin: "0.75rem 0 1rem" }}>
          {session ? (
            <CopyableTmuxAttach session={session} size="md" />
          ) : error ? (
            <span
              role="alert"
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.78rem",
                color: "var(--mc-red-bright, var(--mc-red))",
              }}
            >
              couldn't resolve operator session: {error}
            </span>
          ) : (
            <span
              style={{
                fontFamily: "var(--mc-mono)",
                fontSize: "0.78rem",
                color: "var(--mc-text-dim)",
              }}
            >
              resolving operator session…
            </span>
          )}
        </div>
        <div className="mc-help-callout">
          <strong>Tip:</strong> the operator session is long-lived and survives
          ssh disconnects. Detach with <code>Ctrl-b d</code> at any time; the
          operator keeps working.
        </div>
      </section>

      <section className="mc-help-section" id="next">
        <h2>What's next</h2>
        <p>
          Head over to the server view to see existing projects, or use the
          operator session to create new ones. You can come back to this page
          any time at <code>/welcome</code>.
        </p>
        <div style={{ marginTop: "1rem" }}>
          <button
            type="button"
            className="btn btn-primary"
            data-testid="welcome-next"
            onClick={() => navigate(WELCOME_NEXT_PATH)}
          >
            Next — server view →
          </button>
        </div>
      </section>
    </div>
  );
}

export default Welcome;
