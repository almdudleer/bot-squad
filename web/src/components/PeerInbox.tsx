/**
 * T-0127: in-UI inbox/chat panel for peer replies.
 *
 * Teammates reply to the logged-in user's stable UI SID (S-<user>-ui-p0) over
 * the peer message bus. T-0035 already mirrors those replies to Telegram; this
 * panel COEXISTS with that path, surfacing the same replies inside the web app
 * so the operator can read + answer without leaving the board.
 *
 * The peer inbox DRAINS on read (each peerInboxRead returns only lines appended
 * since the last read), so we persist everything we've seen to localStorage via
 * the pure helpers in ../peerInbox and long-poll (peerInboxWait) for new mail.
 */
import { useEffect, useState } from "react";

import { useApiClient } from "../apiContext";
import {
  loadStored,
  mergeMessages,
  messageId,
  saveStored,
  uiSidFor,
  type StoredMsg,
} from "../peerInbox";

const WAIT_TIMEOUT_SEC = 60;
const ERROR_BACKOFF_MS = 4000;

function fmtTs(ts: string): string {
  const d = new Date(ts);
  if (isNaN(d.getTime())) return ts;
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function PeerInbox({ slug, username }: { slug: string; username: string }) {
  const api = useApiClient();
  const sid = uiSidFor(username);

  const [messages, setMessages] = useState<StoredMsg[]>(() => loadStored(slug, sid));
  const [open, setOpen] = useState(false);
  const [replyTo, setReplyTo] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState<string | null>(null);

  // Reload persisted state when the (slug, sid) target changes.
  useEffect(() => {
    setMessages(loadStored(slug, sid));
  }, [slug, sid]);

  // Real-time pump: drain once, then long-poll forever until cleanup.
  useEffect(() => {
    let cancelled = false;

    const apply = (lines: string[]) => {
      if (cancelled || lines.length === 0) return;
      setMessages((prev) => {
        const merged = mergeMessages(prev, lines);
        if (merged === prev) return prev;
        saveStored(slug, sid, merged);
        return merged;
      });
    };

    const sleep = (ms: number) =>
      new Promise<void>((resolve) => setTimeout(resolve, ms));

    (async () => {
      // Initial drain so we don't wait a full poll cycle to show backlog.
      try {
        const r = await api.peerInboxRead(slug, sid);
        apply(r.messages ?? []);
      } catch {
        // ignore — the loop below will retry
      }
      while (!cancelled) {
        try {
          const w = await api.peerInboxWait(slug, sid, WAIT_TIMEOUT_SEC);
          if (cancelled) break;
          if (w.ready) {
            const r = await api.peerInboxRead(slug, sid);
            if (cancelled) break;
            apply(r.messages ?? []);
          }
        } catch {
          if (cancelled) break;
          // Swallow + back off so a failing endpoint never hot-spins.
          await sleep(ERROR_BACKOFF_MS);
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [api, slug, sid]);

  const unread = messages.filter((m) => !m.read).length;

  function persist(next: StoredMsg[]) {
    saveStored(slug, sid, next);
    setMessages(next);
  }

  function markRead(id: string) {
    persist(messages.map((m) => (m.id === id ? { ...m, read: true } : m)));
  }

  function markAllRead() {
    persist(messages.map((m) => (m.read ? m : { ...m, read: true })));
  }

  function startReply(to: string) {
    setReplyTo(to);
    setReplyText("");
    setSendError(null);
  }

  async function sendReply() {
    if (replyTo == null || !replyText.trim()) {
      setSendError("Reply text is required");
      return;
    }
    setSending(true);
    setSendError(null);
    try {
      await api.peerSend(slug, sid, replyTo, replyText.trim());
      // Echo our own reply into the local list as a read, self-tagged message.
      const sent = {
        ts: new Date().toISOString(),
        from: `${sid} (you → ${replyTo})`,
        body: replyText.trim(),
      };
      const echo: StoredMsg = { id: messageId(sent), ...sent, read: true };
      persist([...messages, echo]);
      setReplyText("");
      setReplyTo(null);
    } catch (e: unknown) {
      setSendError(String(e));
    } finally {
      setSending(false);
    }
  }

  // newest-first for display
  const rows = messages.slice().reverse();

  return (
    <div style={{ position: "fixed", right: 16, bottom: 16, zIndex: 1080 }}>
      {open && (
        <div
          className="card shadow"
          style={{
            width: 360,
            maxWidth: "90vw",
            marginBottom: 8,
            background: "var(--mc-surface, #fff)",
            border: "1px solid var(--mc-border)",
            borderRadius: 8,
            overflow: "hidden",
          }}
        >
          <div
            className="d-flex justify-content-between align-items-center px-3 py-2"
            style={{ borderBottom: "1px solid var(--mc-border)" }}
          >
            <strong style={{ fontSize: "0.85rem" }}>Peer inbox</strong>
            <div className="d-flex align-items-center gap-2">
              <button
                type="button"
                className="btn btn-sm btn-link p-0"
                style={{ fontSize: "0.72rem" }}
                onClick={markAllRead}
                disabled={unread === 0}
              >
                Mark all read
              </button>
              <button
                type="button"
                className="btn-close"
                aria-label="Close inbox"
                onClick={() => setOpen(false)}
              />
            </div>
          </div>

          <div style={{ maxHeight: 360, overflowY: "auto" }}>
            {rows.length === 0 && (
              <div
                className="px-3 py-4 text-center"
                style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}
              >
                No peer replies yet.
              </div>
            )}
            {rows.map((m) => (
              <div
                key={m.id}
                className="px-3 py-2"
                style={{
                  borderBottom: "1px solid var(--mc-border)",
                  background: m.read ? "transparent" : "var(--mc-surface-accent, rgba(0,123,255,0.06))",
                }}
              >
                <div className="d-flex justify-content-between align-items-baseline gap-2">
                  <code style={{ fontSize: "0.72rem", fontFamily: "var(--mc-mono)" }}>
                    {m.from}
                  </code>
                  <span style={{ fontSize: "0.68rem", color: "var(--mc-text-dim)" }}>
                    {!m.read && (
                      <span className="mc-badge mc-badge-active" style={{ marginRight: 6 }}>
                        new
                      </span>
                    )}
                    {fmtTs(m.ts)}
                  </span>
                </div>
                <div style={{ fontSize: "0.82rem", whiteSpace: "pre-wrap", wordBreak: "break-word", margin: "2px 0 4px" }}>
                  {m.body}
                </div>
                <div className="d-flex gap-2">
                  {!m.read && (
                    <button
                      type="button"
                      className="btn btn-sm btn-link p-0"
                      style={{ fontSize: "0.72rem" }}
                      onClick={() => markRead(m.id)}
                    >
                      Mark read
                    </button>
                  )}
                  <button
                    type="button"
                    className="btn btn-sm btn-link p-0"
                    style={{ fontSize: "0.72rem" }}
                    onClick={() => startReply(m.from)}
                  >
                    Reply
                  </button>
                </div>
              </div>
            ))}
          </div>

          {replyTo != null && (
            <div className="px-3 py-2" style={{ borderTop: "1px solid var(--mc-border)" }}>
              <div className="mb-1" style={{ fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
                Reply to <code style={{ fontFamily: "var(--mc-mono)" }}>{replyTo}</code>
              </div>
              {sendError && (
                <div className="alert alert-danger py-1 px-2 mb-1" style={{ fontSize: "0.75rem" }}>
                  {sendError}
                </div>
              )}
              <textarea
                className="form-control"
                rows={2}
                value={replyText}
                onChange={(e) => setReplyText(e.target.value)}
                placeholder="Type a reply…"
                style={{ fontSize: "0.82rem" }}
              />
              <div className="d-flex justify-content-end gap-2 mt-1">
                <button
                  type="button"
                  className="btn btn-sm btn-secondary"
                  onClick={() => setReplyTo(null)}
                  disabled={sending}
                >
                  Cancel
                </button>
                <button
                  type="button"
                  className="btn btn-sm btn-primary"
                  onClick={sendReply}
                  disabled={sending}
                >
                  {sending ? "Sending…" : "Send"}
                </button>
              </div>
            </div>
          )}
        </div>
      )}

      <button
        type="button"
        className="btn btn-primary btn-sm shadow"
        style={{ borderRadius: 999 }}
        onClick={() => setOpen((o) => !o)}
        title="Peer inbox"
      >
        Inbox
        {unread > 0 && (
          <span className="mc-badge mc-badge-active" style={{ marginLeft: 6 }}>
            {unread}
          </span>
        )}
      </button>
    </div>
  );
}
