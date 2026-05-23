import { useEffect, useState } from "react";
import { api, MeProfile } from "../api";
import { Coachmark } from "../onboarding";

type TestState =
  | { kind: "idle" }
  | { kind: "sending" }
  | { kind: "ok" }
  | { kind: "err"; msg: string };

export function Profile() {
  const [profile, setProfile] = useState<MeProfile | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [chatId, setChatId] = useState<string>("");
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const [test, setTest] = useState<TestState>({ kind: "idle" });

  useEffect(() => {
    api
      .getMyProfile()
      .then((p) => {
        setProfile(p);
        setChatId(p.tg_chat_id ?? "");
      })
      .catch((e) => setError(String(e)));
  }, []);

  const bound = (profile?.tg_chat_id ?? "").trim();
  const dirty = chatId.trim() !== bound;

  async function save() {
    setError(null);
    setTest({ kind: "idle" });
    setSaving(true);
    try {
      const result = await api.putMyTgChatId(chatId.trim());
      setProfile(result);
      setChatId(result.tg_chat_id ?? "");
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
      await api.testMyTgChatId();
      setTest({ kind: "ok" });
    } catch (e) {
      setTest({ kind: "err", msg: String(e) });
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "640px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        My profile
      </h2>
      <p
        style={{
          fontSize: "0.75rem",
          color: "var(--mc-text-dim)",
          marginBottom: "1rem",
        }}
      >
        These settings apply across all bot-squad servers you're attached to.
      </p>

      {error && <div className="alert alert-danger">{error}</div>}
      {profile === null && !error && <div className="mc-loading">Loading</div>}

      {profile && (
        <>
          {/* §9.5 — passive spotlight. Fires once the user lands on /me from
              the §9.x chain, points at the chat-id input, and is dismissed by
              ESC / backdrop / "Got it". Framework handles seen_steps. */}
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
                you don't need to bring your own bot.
              </>
            }
          />

          <section className="mb-4">
            <div style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
              {profile.username}
              {profile.linux_user !== profile.username
                ? ` (${profile.linux_user})`
                : ""}
              {profile.is_admin ? " — admin" : ""}
            </div>
          </section>

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
                style={{
                  fontFamily: "var(--mc-mono)",
                  width: "16rem",
                }}
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
                disabled={
                  dirty ||
                  !bound ||
                  test.kind === "sending"
                }
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
                  {" "}— test ping sent.
                </span>
              )}
              {test.kind === "err" && (
                <span style={{ color: "var(--mc-red)" }}>
                  {" "}— test ping failed: {test.msg}
                </span>
              )}
            </div>
            <small style={{ color: "var(--mc-text-dim)" }}>
              Open <code>@bot_squad_bot</code> in Telegram, send <code>/start</code>,
              and the bot will reply with your chat ID. Paste it here, save, and
              hit “Send test ping” to confirm wiring.
            </small>
          </section>
        </>
      )}
    </div>
  );
}
