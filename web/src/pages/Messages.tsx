import { useEffect, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import { api, MessageRecord } from "../api";

// ---------------------------------------------------------------------------
// Tool use card
// ---------------------------------------------------------------------------

function ToolUseCard({
  name,
  id: tuId,
  input,
}: {
  name: string;
  id: string;
  input: Record<string, unknown>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-1">
      <button
        type="button"
        style={{
          background: "var(--mc-surface)",
          border: "1px solid var(--mc-border-mid)",
          borderRadius: "2px",
          padding: "2px 8px",
          fontSize: "0.72rem",
          fontFamily: "var(--mc-mono)",
          color: "var(--mc-text-dim)",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: "0.35rem",
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span>{name}</span>
        <span style={{ opacity: 0.5, fontSize: "0.65rem" }}>({tuId.slice(0, 8)})</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre
          style={{
            background: "var(--mc-surface-raised)",
            border: "1px solid var(--mc-border)",
            borderRadius: "2px",
            padding: "0.5rem 0.75rem",
            fontSize: "0.7rem",
            fontFamily: "var(--mc-mono)",
            maxHeight: "12rem",
            overflowY: "auto",
            marginTop: "0.25rem",
            marginBottom: 0,
            color: "var(--mc-text-mid)",
          }}
        >
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool result card
// ---------------------------------------------------------------------------

function ToolResultCard({ output, toolUseId }: { output: string; toolUseId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-1">
      <button
        type="button"
        style={{
          background: "var(--mc-surface)",
          border: "1px solid var(--mc-accent-dim)",
          borderRadius: "2px",
          padding: "2px 8px",
          fontSize: "0.72rem",
          fontFamily: "var(--mc-mono)",
          color: "var(--mc-accent-dim)",
          cursor: "pointer",
          display: "inline-flex",
          alignItems: "center",
          gap: "0.35rem",
        }}
        onClick={() => setOpen((v) => !v)}
      >
        <span>tool result</span>
        <span style={{ opacity: 0.6, fontSize: "0.65rem" }}>({toolUseId.slice(0, 8)})</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre
          style={{
            background: "var(--mc-surface-raised)",
            border: "1px solid var(--mc-border)",
            borderRadius: "2px",
            padding: "0.5rem 0.75rem",
            fontSize: "0.7rem",
            fontFamily: "var(--mc-mono)",
            maxHeight: "16rem",
            overflowY: "auto",
            marginTop: "0.25rem",
            marginBottom: 0,
            color: "var(--mc-text-mid)",
          }}
        >
          {output || "(empty)"}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Message bubble
// ---------------------------------------------------------------------------

function MessageBubble({ msg }: { msg: MessageRecord }) {
  const isUser = msg.role === "user";
  const isAssistant = msg.role === "assistant";
  const isTool = msg.role === "tool";

  const ts = msg.ts
    ? new Date(msg.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "";

  if (isTool) {
    return (
      <div className="d-flex justify-content-start mb-2">
        <div style={{ maxWidth: "85%" }}>
          <ToolResultCard
            output={msg.tool_result?.output ?? msg.text}
            toolUseId={msg.tool_result?.tool_use_id ?? ""}
          />
          {ts && (
            <div style={{ fontFamily: "var(--mc-mono)", fontSize: "0.62rem", color: "var(--mc-text-dim)", marginTop: "0.2rem" }}>
              {ts}
            </div>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className={`d-flex mb-3 ${isUser ? "justify-content-end" : "justify-content-start"}`}>
      <div style={{ maxWidth: "80%" }}>
        <div className={isUser ? "mc-msg-user" : "mc-msg-assistant"}>
          {/* Tool uses (assistant) */}
          {isAssistant && msg.tool_uses && msg.tool_uses.length > 0 && (
            <div className="mb-2">
              {msg.tool_uses.map((tu) => (
                <ToolUseCard key={tu.id} name={tu.name} id={tu.id} input={tu.input} />
              ))}
            </div>
          )}

          {/* Main text */}
          {msg.text && msg.text.trim() && (
            <div style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>
              {msg.text}
            </div>
          )}

          {/* Empty assistant message */}
          {isAssistant && !msg.text?.trim() && (!msg.tool_uses || msg.tool_uses.length === 0) && (
            <span style={{ color: "var(--mc-text-dim)", fontStyle: "italic", fontSize: "0.78rem" }}>
              (no text)
            </span>
          )}
        </div>
        {ts && (
          <div
            className={`mc-msg-role mt-1 ${isUser ? "text-end" : ""}`}
          >
            {msg.role} · {ts}
          </div>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Messages page
// ---------------------------------------------------------------------------

export function Messages() {
  const { slug = "", claude_uuid = "" } = useParams();
  const [messages, setMessages] = useState<MessageRecord[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const limit = 200;
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .sessionMessages(slug, claude_uuid, limit, 0)
      .then((rows) => {
        setMessages(rows);
        setHasMore(rows.length >= limit);
        setError(null);
        setTimeout(() => bottomRef.current?.scrollIntoView({ behavior: "smooth" }), 100);
      })
      .catch((e: unknown) => setError(String(e)));
  }, [slug, claude_uuid]);

  async function loadMore() {
    if (loadingMore || messages === null) return;
    setLoadingMore(true);
    try {
      const more = await api.sessionMessages(slug, claude_uuid, limit, messages.length);
      setMessages((prev) => [...(prev ?? []), ...more]);
      setHasMore(more.length >= limit);
    } catch (e: unknown) {
      setError(String(e));
    } finally {
      setLoadingMore(false);
    }
  }

  function scrollToBottom() {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }

  return (
    <div className="container py-4">
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <div>
          <div style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.15rem" }}>
            Session transcript
          </div>
          <code style={{ fontFamily: "var(--mc-mono)", fontSize: "0.72rem", color: "var(--mc-text-dim)" }}>
            {claude_uuid}
          </code>
        </div>
        <button
          type="button"
          className="btn btn-outline-secondary btn-sm"
          onClick={scrollToBottom}
        >
          ↓ Jump to bottom
        </button>
      </div>

      {/* Error */}
      {error && (
        <div className="alert alert-danger">
          {error}
          {error.includes("session log not found") && (
            <div className="mt-1" style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
              The session log file may not exist yet or may belong to a different user.
            </div>
          )}
        </div>
      )}

      {/* Loading */}
      {messages === null && !error && (
        <div className="mc-loading">Loading transcript</div>
      )}

      {/* Empty state */}
      {messages !== null && messages.length === 0 && (
        <div className="mc-empty">
          <div className="mc-empty-icon">◯</div>
          <div>No messages in this session yet.</div>
          <div style={{ fontSize: "0.75rem", marginTop: "0.3rem" }}>Messages appear once the Claude session exchanges data.</div>
        </div>
      )}

      {/* Load more (top) */}
      {messages !== null && messages.length > 0 && hasMore && (
        <div className="text-center mb-3">
          <button
            type="button"
            className="btn btn-outline-secondary btn-sm"
            onClick={loadMore}
            disabled={loadingMore}
          >
            {loadingMore ? "Loading…" : "Load earlier messages"}
          </button>
        </div>
      )}

      {/* Messages */}
      {messages !== null && messages.length > 0 && (
        <div>
          {messages.map((msg, i) => (
            <MessageBubble key={i} msg={msg} />
          ))}
          <div ref={bottomRef} />
        </div>
      )}

      {messages !== null && messages.length > 0 && (
        <div className="text-center mt-3">
          <button
            type="button"
            className="btn btn-outline-secondary btn-sm"
            onClick={scrollToBottom}
          >
            ↓ Bottom
          </button>
        </div>
      )}
    </div>
  );
}
