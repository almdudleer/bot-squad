import { useEffect, useState } from "react";
import { api, SystemSettings as Settings } from "../api";

const TTL_RE = /^\d+[smhd]$/;

export function SystemSettings() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [botToken, setBotToken] = useState<string>("");
  const [showToken, setShowToken] = useState(false);
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
        setTtl(s.session.ttl);
        setCoordUser(s.admin.coordinator_user);
      })
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    load();
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
      const body = {
        tg: {
          quiet_hours_start_utc: quietStart,
          quiet_hours_end_utc: quietEnd,
          ...(botToken !== "" ? { bot_token: botToken } : {}),
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
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "1rem" }}>
        System settings
      </h2>

      {error && <div className="alert alert-danger">{error}</div>}
      {notice && <div className="alert alert-success py-2">{notice}</div>}

      {settings === null && !error && <div className="mc-loading">Loading</div>}

      {settings && (
        <>
          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Telegram bot token
            </h3>
            <div className="d-flex gap-2 align-items-center mb-1">
              <input
                type={showToken ? "text" : "password"}
                className="form-control"
                value={botToken}
                onChange={(e) => setBotToken(e.target.value)}
                placeholder={settings.tg.bot_token_set ? "•••••• (token configured)" : "paste bot token"}
                style={{ fontFamily: "var(--mc-mono)" }}
              />
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                onClick={() => setShowToken((v) => !v)}
              >
                {showToken ? "Hide" : "Show"}
              </button>
            </div>
            <small style={{ color: "var(--mc-text-dim)" }}>
              Bot token configured: {settings.tg.bot_token_set ? "yes" : "no"}.
              Leave blank to keep current. Submit an empty value to clear (use Show then Clear).
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
        </>
      )}
    </div>
  );
}
