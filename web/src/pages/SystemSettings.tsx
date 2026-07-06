import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, SchedulerJob, SchedulerState, SystemSettings as Settings } from "../api";
import { Modal } from "../components/Modal";
import { Coachmark } from "../onboarding";

const TTL_RE = /^\d+[smhd]$/;
// T-0194: socks5(h)/http(s) — mirrors the API's _PROXY_RE. Empty = direct.
const PROXY_RE = /^(socks5h?|https?):\/\/.+/i;

function fmtFuture(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (isNaN(ts)) return "—";
  const diff = Math.floor((ts - Date.now()) / 1000);
  if (diff < 0) return "now";
  if (diff < 60) return `in ${diff}s`;
  if (diff < 3600) return `in ${Math.floor(diff / 60)}m`;
  if (diff < 86400) return `in ${Math.floor(diff / 3600)}h`;
  return `in ${Math.floor(diff / 86400)}d`;
}

/**
 * Pure render of the job list — no fetch, no heartbeat (WorkerHealthPill
 * owns worker health, D-0056). Exported so it can be unit-tested directly.
 */
export function SchedulerJobsView({ jobs }: { jobs: SchedulerJob[] }) {
  if (jobs.length === 0) {
    return (
      <div className="text-muted" style={{ fontSize: "0.82rem" }}>
        No scheduled jobs.
      </div>
    );
  }
  return (
    <table className="table table-sm mc-table" style={{ fontSize: "0.78rem" }}>
      <thead>
        <tr>
          <th>Job</th>
          <th>Trigger</th>
          <th>Next run</th>
        </tr>
      </thead>
      <tbody>
        {jobs.map((job) => (
          <tr key={job.id}>
            <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem" }}>{job.id}</td>
            <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
              {job.trigger}
            </td>
            <td style={{ fontFamily: "var(--mc-mono)", fontSize: "0.74rem", color: "var(--mc-text-dim)" }}>
              <span title={job.next_run ?? undefined}>{fmtFuture(job.next_run)}</span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/**
 * Relocated from ObservabilityPanel (D-0056/T-0629, T-0627 removed it there) —
 * system-level job facts belong on the admin surface, not the per-project
 * home. Own fetch + 30s poll (matches the old panel's cadence) so a scheduler
 * hiccup can't blank the rest of the settings form.
 */
function SchedulerJobsSection() {
  const [state, setState] = useState<SchedulerState | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      api
        .scheduler()
        .then((s) => {
          if (!cancelled) {
            setState(s);
            setError(null);
          }
        })
        .catch((e) => {
          if (!cancelled) setError(String(e));
        });
    load();
    const id = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return (
    <details className="mb-4" data-testid="scheduler-jobs">
      <summary style={{ fontSize: "0.85rem", fontWeight: 600, cursor: "pointer" }}>
        Scheduler jobs
      </summary>
      <div style={{ marginTop: "0.5rem" }}>
        {error && (
          <div className="text-muted" style={{ fontSize: "0.85rem" }}>
            Scheduler state unavailable: {error}
          </div>
        )}
        {state === null && !error && <div className="mc-loading">Loading scheduler state</div>}
        {state && <SchedulerJobsView jobs={state.jobs} />}
      </div>
    </details>
  );
}

export function SystemSettings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [isAdmin, setIsAdmin] = useState<boolean>(false);
  const [detachOpen, setDetachOpen] = useState(false);

  const [botToken, setBotToken] = useState<string>("");
  const [showToken, setShowToken] = useState(false);
  const [defaultChatId, setDefaultChatId] = useState<string>("");
  const [proxyUrl, setProxyUrl] = useState<string>("");
  const [quietStart, setQuietStart] = useState<number>(17);
  const [quietEnd, setQuietEnd] = useState<number>(5);
  const [ttl, setTtl] = useState<string>("7d");
  const [coordUser, setCoordUser] = useState<string>("");

  function load() {
    setError(null);
    api
      .getSystemSettings()
      .then((s) => {
        setSettings(s);
        setQuietStart(s.tg.quiet_hours_start_utc);
        setQuietEnd(s.tg.quiet_hours_end_utc);
        setDefaultChatId(s.tg.default_chat_id);
        setProxyUrl(s.tg.proxy_url);
        setTtl(s.session.ttl);
        setCoordUser(s.admin.coordinator_user);
      })
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    load();
    api
      .me()
      .then((m) => setIsAdmin(Boolean(m.is_admin)))
      .catch(() => {
        /* anonymous / load error — leave isAdmin false so Danger zone stays hidden */
      });
  }, []);

  function validate(): string | null {
    if (!Number.isInteger(quietStart) || quietStart < 0 || quietStart > 23) {
      return "quiet_hours_start_utc must be int 0–23";
    }
    if (!Number.isInteger(quietEnd) || quietEnd < 0 || quietEnd > 23) {
      return "quiet_hours_end_utc must be int 0–23";
    }
    if (!TTL_RE.test(ttl)) {
      return "session TTL must match ^\\d+[smhd]$ (e.g. 7d, 24h, 30m)";
    }
    if (proxyUrl.trim() && !PROXY_RE.test(proxyUrl.trim())) {
      return "TG proxy URL must be socks5://, http://, or https:// (or empty)";
    }
    if (!coordUser.trim()) {
      return "coordinator linux user must not be empty";
    }
    return null;
  }

  async function save() {
    setNotice(null);
    setError(null);
    const v = validate();
    if (v !== null) {
      setError(v);
      return;
    }
    setSaving(true);
    try {
      // When attached to the mothership, the bot token + default chat are locked
      // (inputs disabled). Omit them from the payload so the API doesn't 409.
      const managed = settings?.tg.managed_by_mothership ?? false;
      const body = {
        tg: {
          quiet_hours_start_utc: quietStart,
          quiet_hours_end_utc: quietEnd,
          // T-0194: proxy_url is NOT mothership-locked — always sent.
          proxy_url: proxyUrl.trim(),
          ...(managed
            ? {}
            : {
                default_chat_id: defaultChatId.trim(),
                ...(botToken !== "" ? { bot_token: botToken } : {}),
              }),
        },
        session: { ttl },
        admin: { coordinator_user: coordUser.trim() },
      };
      const result = await api.putSystemSettings(body);
      setSettings(result);
      setBotToken("");
      setNotice(result.restart_required ? "Saved. Restart the worker for changes to take effect." : "Saved.");
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "720px" }}>
      {/* §9.2 — admin-only spotlight pointing at the Detach affordance.
          Fires only once an admin lands on /system-settings, so it doesn't
          compete with §9.1 on the Picker. Non-admins never render it, so the
          step is never marked seen for them and never gates onboarding. */}
      {isAdmin && (
        <Coachmark
          stepId="srv.9_2.detach_admin"
          title="You can detach this server"
          anchorSelector='[data-onboarding-anchor="detach-toggle"]'
          placement="top"
          body={
            <>
              Now that you own a bot-squad installation, you can detach from
              botsquad.dev at any time and run standalone from this server's
              own address. See the advantages before you do.
            </>
          }
        />
      )}

      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.75rem" }}>
        Server settings
      </h2>

      {/* T-0170: server-admin tools that lost their sidebar "MORE" rows are
          re-homed here, behind the server-settings gear. The settings form
          below covers the server's TG bot config (incl. the detached-install
          token, T-0171), quiet hours, session TTL and coordinator user. */}
      {isAdmin && (
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "0.5rem",
            marginBottom: "1.25rem",
          }}
        >
          <Link to="/users" className="mc-badge mc-badge-info" style={{ textDecoration: "none", padding: "0.3rem 0.7rem" }}>
            Manage users →
          </Link>
          {/* T-0572 (Occam pass): the standalone /scheduler page merged into
              the per-project observability panel on the project home (T-0593). */}
        </div>
      )}

      {error && <div className="alert alert-danger">{error}</div>}
      {notice && <div className="alert alert-success py-2">{notice}</div>}

      {settings === null && !error && <div className="mc-loading">Loading</div>}

      {settings && (
        <>
          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Telegram bot
            </h3>

            {/* T-0171: when this server is an attached mothership consumer, the
                per-server bot token + default chat are locked — notifications
                flow through the mothership's @bot_squad_bot. */}
            {settings.tg.managed_by_mothership && (
              <div
                className="alert alert-info py-2"
                style={{ fontSize: "0.8rem" }}
                data-testid="tg-mothership-lock-banner"
              >
                🔒 Locked — this server is attached to{" "}
                {settings.tg.mothership_url ? (
                  <a href={settings.tg.mothership_url} target="_blank" rel="noreferrer">
                    the mothership
                  </a>
                ) : (
                  "the mothership"
                )}
                . Notifications are delivered through the mothership's{" "}
                <code>@bot_squad_bot</code>, so this server doesn't need its own
                bot. Detach to use your own bot token.
              </div>
            )}

            <label htmlFor="ss-bot-token" className="form-label" style={{ fontSize: "0.72rem" }}>
              Bot token
            </label>
            <div className="d-flex gap-2 align-items-center mb-1">
              <input
                id="ss-bot-token"
                type={showToken ? "text" : "password"}
                className="form-control"
                value={botToken}
                disabled={settings.tg.managed_by_mothership}
                onChange={(e) => setBotToken(e.target.value)}
                placeholder={settings.tg.bot_token_set ? "•••••• (token configured)" : "paste bot token"}
                style={{ fontFamily: "var(--mc-mono)" }}
              />
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                disabled={settings.tg.managed_by_mothership}
                onClick={() => setShowToken((v) => !v)}
              >
                {showToken ? "Hide" : "Show"}
              </button>
            </div>
            <small style={{ display: "block", color: "var(--mc-text-dim)" }}>
              Bot token configured: {settings.tg.bot_token_set ? "yes" : "no"}.
              Leave blank to keep current. Submit an empty value to clear (use Show then Clear).
            </small>

            <label htmlFor="ss-default-chat" className="form-label mt-3" style={{ fontSize: "0.72rem" }}>
              Default chat id
            </label>
            <input
              id="ss-default-chat"
              type="text"
              className="form-control"
              value={defaultChatId}
              disabled={settings.tg.managed_by_mothership}
              onChange={(e) => setDefaultChatId(e.target.value)}
              placeholder="e.g. 404580642"
              style={{ width: "18rem", fontFamily: "var(--mc-mono)" }}
            />
            <small style={{ display: "block", color: "var(--mc-text-dim)" }}>
              Default Telegram chat for notifications that aren't bound to a
              project. Used by this server's own bot when detached.
            </small>

            {/* T-0194: per-installation TG egress proxy. NOT mothership-locked —
                it's a host-network concern (some hosts DPI-block Telegram, T-0192).
                Routes ONLY the worker's Telegram traffic through the proxy. */}
            <label htmlFor="ss-proxy-url" className="form-label mt-3" style={{ fontSize: "0.72rem" }}>
              Egress proxy URL
            </label>
            <input
              id="ss-proxy-url"
              type="text"
              className="form-control"
              value={proxyUrl}
              onChange={(e) => setProxyUrl(e.target.value)}
              placeholder="e.g. http://153.80.195.83:8888 or socks5://host:1080"
              data-testid="tg-proxy-url"
              style={{ width: "24rem", fontFamily: "var(--mc-mono)" }}
            />
            <small style={{ display: "block", color: "var(--mc-text-dim)" }}>
              Route this server's Telegram traffic through a proxy
              (<code>socks5://</code>, <code>http://</code>, or <code>https://</code>).
              Use when the host can't reach api.telegram.org directly. Leave blank
              for direct egress. Restart the worker after saving.
            </small>
          </section>

          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Telegram quiet hours (UTC)
            </h3>
            <div className="d-flex gap-3 align-items-end">
              <div>
                <label htmlFor="ss-quiet-start" className="form-label" style={{ fontSize: "0.72rem" }}>
                  Start hour
                </label>
                <input
                  id="ss-quiet-start"
                  type="number"
                  min={0}
                  max={23}
                  className="form-control"
                  value={quietStart}
                  onChange={(e) => setQuietStart(Number.parseInt(e.target.value, 10))}
                  style={{ width: "6rem" }}
                />
              </div>
              <div>
                <label htmlFor="ss-quiet-end" className="form-label" style={{ fontSize: "0.72rem" }}>
                  End hour
                </label>
                <input
                  id="ss-quiet-end"
                  type="number"
                  min={0}
                  max={23}
                  className="form-control"
                  value={quietEnd}
                  onChange={(e) => setQuietEnd(Number.parseInt(e.target.value, 10))}
                  style={{ width: "6rem" }}
                />
              </div>
            </div>
            <small style={{ color: "var(--mc-text-dim)" }}>
              Range wraps midnight: start &gt; end means start..23 + 0..end.
            </small>
          </section>

          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Session TTL
            </h3>
            <input
              id="ss-ttl"
              aria-label="Session TTL"
              type="text"
              className="form-control"
              value={ttl}
              onChange={(e) => setTtl(e.target.value)}
              style={{ width: "10rem", fontFamily: "var(--mc-mono)" }}
            />
            <small style={{ color: "var(--mc-text-dim)" }}>
              Match <code>^\d+[smhd]$</code> — e.g. <code>7d</code>, <code>24h</code>, <code>30m</code>.
            </small>
          </section>

          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Coordinator Linux user
            </h3>
            <input
              id="ss-coord-user"
              aria-label="Coordinator Linux user"
              type="text"
              className="form-control"
              value={coordUser}
              onChange={(e) => setCoordUser(e.target.value)}
              style={{ width: "18rem", fontFamily: "var(--mc-mono)" }}
            />
            <small style={{ color: "var(--mc-text-dim)" }}>
              Linux user that hosts the coordinator worker. Changing this requires re-deploy.
            </small>
          </section>

          {/* D-0056 (T-0629): the caps meters + admin inputs moved to
              ResourceCapsPanel on each project's Processes page (T-0339 chose
              that home) — this page keeps only a pointer so the fact has one
              home instead of two. */}
          <section className="mb-4" data-testid="resource-caps">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Resource caps
            </h3>
            <div className="alert alert-secondary py-2" style={{ fontSize: "0.8rem", marginBottom: 0 }}>
              Resource caps (parallel sessions, token budget) are viewed and set
              on each project&apos;s <Link to="/">Processes page</Link> (Resources
              &amp; quota panel) — pick a project from the home picker.
            </div>
          </section>

          {/* D-0056 (T-0629): the scheduler job table relocates here from the
              project home's ObservabilityPanel (lane A, T-0627) — system-level
              facts live on the admin surface, not the per-project one. No
              heartbeat dot: WorkerHealthPill (Shell) already owns worker
              health. */}
          <SchedulerJobsSection />

          <button
            type="button"
            className="btn btn-primary"
            onClick={save}
            disabled={saving}
          >
            {saving ? "Saving…" : "Save"}
          </button>

          {isAdmin && (
            <section className="mt-5 pt-4 border-top" id="detach-affordance">
              <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem", color: "var(--mc-red-bright)" }}>
                Danger zone
              </h3>
              <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.75rem" }}>
                Detach this server from botsquad.dev. Your installation will keep working as a
                standalone server-only frontend; the centralization layer will be unmounted.
              </p>
              <button
                type="button"
                className="btn btn-outline-danger btn-sm"
                data-onboarding-anchor="detach-toggle"
                onClick={() => setDetachOpen(true)}
              >
                Detach from botsquad.dev…
              </button>
            </section>
          )}
        </>
      )}

      <Modal
        open={detachOpen}
        title="Detach this server from botsquad.dev?"
        onClose={() => setDetachOpen(false)}
        footer={
          <>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => setDetachOpen(false)}
            >
              Keep centralized
            </button>
            <button
              type="button"
              className="btn btn-outline-danger"
              onClick={() => {
                setDetachOpen(false);
                setNotice("Detach is not yet available — see vision/initiatives/detach-sequence.md.");
              }}
            >
              Detach anyway
            </button>
          </>
        }
      >
        <p style={{ marginBottom: "0.75rem" }}>
          Before you detach, here's what you'd give up by leaving botsquad.dev:
        </p>
        <ul style={{ paddingLeft: "1.1rem", marginBottom: "0.75rem" }}>
          <li>
            <strong>Cross-server projects view</strong> — the <code>/m</code> all-projects window
            that shows every project on every attached server, split by server.
          </li>
          <li>
            <strong>Preferences sync</strong> across all your servers (deferred but designed-in).
          </li>
          <li>
            <strong>Quick project switcher</strong> — the cross-server drop-down for jumping
            between projects without going back to the picker.
          </li>
          <li>
            <strong>Free <code>@bot_squad_bot</code> Telegram routing</strong> — without it, every
            user on this server would need to bring their own bot token.
          </li>
        </ul>
        <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: 0 }}>
          Detach is not yet implemented; this dialog is here so you know what the choice will
          cost when it ships.
        </p>
      </Modal>
    </div>
  );
}
