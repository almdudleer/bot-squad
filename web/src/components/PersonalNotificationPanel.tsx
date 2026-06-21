/**
 * T-0218 — personal notification inheritance panel.
 *
 * Distinct from the PROJECT-WIDE Telegram binding above it on the project
 * settings page (that one pings everyone via projects.toml). THIS panel is the
 * logged-in user's *personal* target, which inherits most-specific-wins.
 *
 * T-0338 (reframe single-brain + Occam, 2026-06-21): collapsed the surface from
 * 3 tiers (global → server → project) to TWO — a **Global default** and a
 * **per-Project override** — for the single operator. The middle **Server**
 * tier was redundant machinery (a 3rd chat-id field + Save + Test) that never
 * mapped to a delivery the operator actually reasons about. It is no longer
 * offered as a settable level; the Server row appears ONLY as a self-healing
 * escape hatch when a *legacy* server override is still set (so it stays
 * visible + clearable), and disappears once cleared. The backend still resolves
 * project → server → global (FE never computes precedence — it renders
 * `GET /me/notifications/resolved`, D-0022), so a pre-existing server override
 * keeps working until cleared. Resolution is channel-agnostic (the worker picks
 * Telegram vs MAX, T-0247), so collapsing tiers does not touch channel routing.
 *
 * Project-level persistence is live (Team-1): putProjectTgChatId persists the
 * per-project personal override (Attachment.project_tg_chat_ids for migrated
 * attachments, UserMeta map otherwise), the resolved view round-trips it, and
 * the worker honors project precedence — so a Project override survives a
 * re-read instead of reverting to "Inherited" (corrected per T-0307/03202ea;
 * the earlier "stub does not persist" note was a stale-docstring false positive).
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
      help: "Your default — where you get pinged unless a project overrides it.",
      save: (v) => api.putMyTgChatId(v),
      test: () => api.testMyTgChatId(),
    },
    // T-0338: the Server tier is no longer an offered level (redundant for the
    // single operator). The RowDef is retained so a LEGACY server override can
    // still be shown + cleared via the same save/test plumbing; it is filtered
    // out of the rendered rows unless `resolved.levels.server.set` is true.
    {
      key: "server",
      label: "Server (legacy)",
      help: "Legacy server-level override — clear it to fall back to Project/Global.",
      save: (v) => attachmentApi.putTgChatId(serverId, v),
      test: () => attachmentApi.testTgChatId(serverId),
    },
    {
      key: "project",
      label: "Project",
      help: "Overrides your Global default for this project only.",
      save: (v) => api.putProjectTgChatId(slug, v),
      test: () => api.testProjectTgChatId(slug),
    },
  ];

  // T-0338: default to the two-tier surface (Global default + Project override).
  // The legacy Server row only appears when an override is actually set there,
  // so it stays visible/clearable without re-introducing a permanent 3rd tier.
  const visibleRows = rows.filter(
    (r) => r.key !== "server" || (resolved?.levels.server.set ?? false),
  );

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
        Where <em>you</em> get pinged for this project. A <strong>Global</strong>{" "}
        default applies everywhere; a <strong>Project</strong> override wins for
        this project only (<code>project → global</code>). Clear an override
        (empty + Save) to fall back to the default. Separate from the
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

          {visibleRows.map((row) => {
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
