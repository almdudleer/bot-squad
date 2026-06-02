import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, SystemSettings as Settings } from "../api";
import { Modal } from "../components/Modal";
import { Coachmark } from "../onboarding";

const TTL_RE = /^\d+[smhd]$/;

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
      setNotice("Saved. Restart worker for changes to take effect.");
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
          <Link to="/scheduler" className="mc-badge mc-badge-info" style={{ textDecoration: "none", padding: "0.3rem 0.7rem" }}>
            Scheduler →
          </Link>
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

            <label className="form-label" style={{ fontSize: "0.72rem" }}>
              Bot token
            </label>
            <div className="d-flex gap-2 align-items-center mb-1">
              <input
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

            <label className="form-label mt-3" style={{ fontSize: "0.72rem" }}>
              Default chat id
            </label>
            <input
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
          </section>

          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Telegram quiet hours (UTC)
            </h3>
            <div className="d-flex gap-3 align-items-end">
              <div>
                <label className="form-label" style={{ fontSize: "0.72rem" }}>
                  Start hour
                </label>
                <input
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
                <label className="form-label" style={{ fontSize: "0.72rem" }}>
                  End hour
                </label>
                <input
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
