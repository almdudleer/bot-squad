/**
 * T-0061 — Telegram chat-id binding page.
 *
 * Per the locked spec in ``vision/multi-server/nav-restructure.md``, this
 * row moved out of /me (global cross-server profile) into the ATTACHMENT
 * section since it's scoped per-user-per-server. The actual write goes to
 * the Attachment store via ``/api/me/attachment/<server_id>/tg-chat-id``
 * (T-0066) when the user is migrated; for un-migrated users the backend
 * silently routes the same call back to UserMeta.tg_chat_id (legacy) so
 * the page works transparently before ``users_split.py`` runs.
 */
import { useEffect, useState } from "react";
import { attachmentApi } from "../attachmentApi";
import { readAttachmentServerId } from "../components/sidebarHelpers";
import { Coachmark } from "../onboarding";

type TestState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "ok" }
  | { kind: "err"; msg: string };

export function AttachmentTgBinding() {
  const [serverId] = useState<string>(() => readAttachmentServerId());
  const [loaded, setLoaded] = useState<boolean>(false);
  const [bound, setBound] = useState<string>("");
  const [chatId, setChatId] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [test, setTest] = useState<TestState>({ kind: "idle" });

  useEffect(() => {
    attachmentApi
      .getTgChatId(serverId)
      .then((p) => {
        setBound(p.tg_chat_id ?? "");
        setChatId(p.tg_chat_id ?? "");
        setLoaded(true);
      })
      .catch((e) => setError(String(e)));
  }, [serverId]);

  const dirty = chatId.trim() !== bound.trim();

  async function save() {
    setError(null);
    setTest({ kind: "idle" });
    setSaving(true);
    try {
      const result = await attachmentApi.putTgChatId(serverId, chatId.trim());
      const next = result.tg_chat_id ?? "";
      setBound(next);
      setChatId(next);
      setSavedAt(Date.now());
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function sendTest() {
    setError(null);
    setTest({ kind: "sending" });
    try {
      await attachmentApi.testTgChatId(serverId);
      setTest({ kind: "ok" });
    } catch (e) {
      setTest({ kind: "err", msg: String(e) });
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "640px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Telegram binding
      </h2>
      <p
        style={{
          fontSize: "0.75rem",
          color: "var(--mc-text-dim)",
          marginBottom: "1rem",
        }}
      >
        Scoped to <code>{serverId}</code>. Bound chat ID receives task
        notifications and worker test pings from this server.
      </p>

      {error && <div className="alert alert-danger">{error}</div>}
      {!loaded && !error && <div className="mc-loading">Loading</div>}

      {loaded && (
        <>
          {/* §9.5 spotlight relocated from /me — same stepId so users who
              dismissed it on the old page don't see it again here. */}
          <Coachmark
            stepId="srv.9_5.tg_binding"
            title="Bind your Telegram chat ID"
            anchorSelector='[data-onboarding-anchor="tg-chat-id"]'
            placement="bottom"
            body={
              <>
                Help user to bind his telegram chat id, ping him with a test
                message. While this server is attached to botsquad.dev, all
                Telegram traffic routes through <code>@bot_squad_bot</code> —
                you don&apos;t need to bring your own bot.
              </>
            }
          />

          <section className="mb-4">
            <h3
              style={{
                fontSize: "0.85rem",
                fontWeight: 600,
                marginBottom: "0.5rem",
              }}
            >
              Telegram chat ID
            </h3>
            <div className="d-flex gap-2 align-items-center mb-1">
              <input
                type="text"
                inputMode="numeric"
                pattern="-?[0-9]*"
                className="form-control"
                value={chatId}
                onChange={(e) => setChatId(e.target.value)}
                placeholder="e.g. 404580642"
                data-onboarding-anchor="tg-chat-id"
                style={{ fontFamily: "var(--mc-mono)", width: "16rem" }}
              />
              <button
                type="button"
                className="btn btn-primary btn-sm"
                onClick={save}
                disabled={saving || !dirty}
              >
                {saving ? "Saving…" : "Save"}
              </button>
              <button
                type="button"
                className="btn btn-outline-secondary btn-sm"
                onClick={sendTest}
                disabled={dirty || !bound || test.kind === "sending"}
                title={
                  dirty
                    ? "Save first"
                    : !bound
                      ? "No chat ID bound"
                      : "Send a test ping to this chat"
                }
              >
                {test.kind === "sending" ? "Pinging…" : "Send test ping"}
              </button>
            </div>
            <div style={{ fontSize: "0.75rem", color: "var(--mc-text-dim)" }}>
              {bound ? (
                <>
                  Bound to <code>{bound}</code>
                  {savedAt && " — saved"}.
                </>
              ) : (
                "Unbound — paste your Telegram chat ID and Save."
              )}
              {test.kind === "ok" && (
                <span style={{ color: "var(--mc-green)" }}>
                  {" "}
                  — test ping sent.
                </span>
              )}
              {test.kind === "err" && (
                <span style={{ color: "var(--mc-red)" }}>
                  {" "}
                  — test ping failed: {test.msg}
                </span>
              )}
            </div>
            <small style={{ color: "var(--mc-text-dim)" }}>
              Open <code>@bot_squad_bot</code> in Telegram, send{" "}
              <code>/start</code>, and the bot will reply with your chat ID.
              Paste it here, save, and hit “Send test ping” to confirm wiring.
            </small>
          </section>
        </>
      )}
    </div>
  );
}
