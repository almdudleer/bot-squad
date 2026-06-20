/**
 * T-0218 — personal notification 3-level inheritance panel.
 *
 * Distinct from the PROJECT-WIDE Telegram binding above it on the project
 * settings page (that one pings everyone via projects.toml). THIS panel is the
 * logged-in user's *personal* target, which inherits most-specific-wins:
 *
 *     project  ->  server  ->  global  ->  (project-wide fallback)
 *
 * Each of the 3 levels shows its own raw value, an Inherited-vs-Override badge,
 * and the winning level is highlighted. The user can set / clear / test any
 * level. The FE never computes precedence — it renders whatever
 * `GET /me/notifications/resolved` reports (D-0022). Writes go to each level's
 * own route, then we re-read the resolved view.
 *
 * Project-level persistence is a deferred T-0218 follow-up (Team-1): the stub
 * echoes a PUT but does not persist, so after a re-read the project row returns
 * to "Inherited". The panel surfaces that honestly rather than faking a winner.
 */
import { useEffect, useState } from "react";
import { api, NotificationsResolved } from "../api";
import { attachmentApi } from "../attachmentApi";
import { readAttachmentServerId } from "./sidebarHelpers";
import {
  effectiveSummary,
  levelRowViews,
  NotificationLevelKey,
} from "../utils/notificationResolve";

type RowDef = {
  key: NotificationLevelKey;
  label: string;
  help: string;
  /** Persist a raw value ("" clears the override) at this level. */
  save: (value: string) => Promise<unknown>;
  /** Ping this level's RESOLVED target. */
  test: () => Promise<{ ok: boolean; sent: boolean }>;
};

function isValidChatId(v: string): boolean {
  const t = v.trim();
  return t === "" || /^-?\d+$/.test(t);
}

export function PersonalNotificationPanel({ slug }: { slug: string }) {
  const [serverId] = useState<string>(() => readAttachmentServerId());
  const [resolved, setResolved] = useState<NotificationsResolved | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // Edit buffers + busy flags keyed by level.
  const [buf, setBuf] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, "save" | "test" | null>>({});

  function load() {
    setError(null);
    api
      .notificationsResolved(serverId, slug)
      .then((r) => {
        setResolved(r);
        // Seed each edit buffer with the level's own raw value.
        setBuf({
          global: r.levels.global.tg_chat_id ?? "",
          server: r.levels.server.tg_chat_id ?? "",
          project: r.levels.project.tg_chat_id ?? "",
        });
      })
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverId, slug]);

  const rows: RowDef[] = [
    {
      key: "global",
      label: "Global",
      help: "Your default across every server and project.",
      save: (v) => api.putMyTgChatId(v),
      test: () => api.testMyTgChatId(),
    },
    {
      key: "server",
      label: "Server",
      help: "Overrides Global for this server only.",
      save: (v) => attachmentApi.putTgChatId(serverId, v),
      test: () => attachmentApi.testTgChatId(serverId),
    },
    {
      key: "project",
      label: "Project",
      help: "Overrides Server/Global for this project only.",
      save: (v) => api.putProjectTgChatId(slug, v),
      test: () => api.testProjectTgChatId(slug),
    },
  ];

  async function onSave(row: RowDef) {
    const value = (buf[row.key] ?? "").trim();
    if (!isValidChatId(value)) {
      setError("Chat id must be an integer (negative for groups) or empty.");
      return;
    }
    setError(null);
    setNotice(null);
    setBusy((b) => ({ ...b, [row.key]: "save" }));
    try {
      await row.save(value);
      setNotice(
        value
          ? `${row.label} override saved.`
          : `${row.label} override cleared — inheriting again.`,
      );
      load();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy((b) => ({ ...b, [row.key]: null }));
    }
  }

  async function onTest(row: RowDef) {
    setError(null);
    setNotice(null);
    setBusy((b) => ({ ...b, [row.key]: "test" }));
    try {
      const r = await row.test();
      setNotice(
        r.sent
          ? `Test ping sent via the ${row.label} route — check the chat.`
          : "Worker accepted the ping but suppressed it (debounce or quiet hours). Try again shortly.",
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy((b) => ({ ...b, [row.key]: null }));
    }
  }

  const views = resolved ? levelRowViews(resolved) : [];

  return (
    <section className="mb-4">
      <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.25rem" }}>
        Personal notifications <span style={{ color: "var(--mc-text-dim)", fontWeight: 400 }}>(you only)</span>
      </h3>
      <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.75rem" }}>
        Where <em>you</em> get pinged for this project. Inherits most-specific
        first: <code>project → server → global</code>. Set an override at any
        level, or clear it (empty + Save) to inherit again. Separate from the
        project-wide binding above, which notifies everyone.
      </p>

      {error && <div className="alert alert-danger py-2">{error}</div>}
      {notice && <div className="alert alert-success py-2">{notice}</div>}
      {resolved === null && !error && <div className="mc-loading">Loading</div>}

      {resolved && (
        <>
          <div
            style={{
              fontSize: "0.8rem",
              fontFamily: "var(--mc-mono)",
              marginBottom: "0.75rem",
              padding: "0.4rem 0.6rem",
              borderRadius: "0.25rem",
              background: "var(--mc-surface-2, rgba(127,127,127,0.08))",
            }}
          >
            {effectiveSummary(resolved)}
          </div>

          {rows.map((row) => {
            const view = views.find((v) => v.key === row.key)!;
            const rowBusy = busy[row.key] ?? null;
            const value = buf[row.key] ?? "";
            const dirty = value.trim() !== view.raw.trim();
            // "Clear" only when emptying an existing override; otherwise "Save".
            const saveLabel = !value.trim() && view.set ? "Clear" : "Save";
            return (
              <div
                key={row.key}
                style={{
                  marginBottom: "0.6rem",
                  paddingLeft: "0.6rem",
                  borderLeft: view.isWinner
                    ? "3px solid var(--mc-green, #3fb950)"
                    : "3px solid transparent",
                }}
              >
                <div className="d-flex align-items-center gap-2 mb-1">
                  <label
                    htmlFor={`pnp-chat-${row.key}`}
                    style={{ fontSize: "0.74rem", fontWeight: 600, width: "4rem" }}
                  >
                    {row.label}
                  </label>
                  {view.set ? (
                    <span
                      className="badge"
                      style={{
                        background: "var(--mc-accent, #2f6feb)",
                        fontSize: "0.62rem",
                      }}
                    >
                      Override
                    </span>
                  ) : (
                    <span
                      className="badge"
                      style={{
                        background: "transparent",
                        color: "var(--mc-text-dim)",
                        border: "1px solid var(--mc-border, #444)",
                        fontSize: "0.62rem",
                      }}
                    >
                      Inherited
                    </span>
                  )}
                  {view.isWinner && (
                    <span
                      style={{ fontSize: "0.66rem", color: "var(--mc-green, #3fb950)" }}
                    >
                      ← pings here
                    </span>
                  )}
                </div>
                <div className="d-flex gap-2 align-items-center">
                  <input
                    id={`pnp-chat-${row.key}`}
                    type="text"
                    inputMode="numeric"
                    pattern="-?[0-9]*"
                    aria-label={`${row.label} chat id`}
                    className="form-control form-control-sm"
                    value={value}
                    onChange={(e) =>
                      setBuf((b) => ({ ...b, [row.key]: e.target.value }))
                    }
                    placeholder="inherit — leave blank"
                    style={{ fontFamily: "var(--mc-mono)", width: "13rem" }}
                  />
                  <button
                    type="button"
                    className="btn btn-primary btn-sm"
                    onClick={() => onSave(row)}
                    disabled={rowBusy !== null || !dirty}
                  >
                    {rowBusy === "save" ? "Saving…" : saveLabel}
                  </button>
                  <button
                    type="button"
                    className="btn btn-outline-secondary btn-sm"
                    onClick={() => onTest(row)}
                    disabled={rowBusy !== null}
                    title="Send a test ping to the chat this level resolves to"
                  >
                    {rowBusy === "test" ? "Pinging…" : "Test"}
                  </button>
                </div>
                <small style={{ color: "var(--mc-text-dim)" }}>{row.help}</small>
              </div>
            );
          })}
        </>
      )}
    </section>
  );
}
