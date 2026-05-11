import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, MessageRecord } from "../api";

// ---------------------------------------------------------------------------
// Tool use card — collapsed by default
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
        className="btn btn-outline-secondary btn-sm d-flex align-items-center gap-1"
        style={{ fontSize: "0.75rem" }}
        onClick={() => setOpen((v) => !v)}
      >
        <span style={{ fontFamily: "monospace" }}>{name}</span>
        <span className="text-muted" style={{ fontSize: "0.65rem" }}>({tuId.slice(0, 8)})</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre
          className="bg-light border rounded p-2 mt-1 mb-0"
          style={{ fontSize: "0.72rem", maxHeight: "12rem", overflowY: "auto" }}
        >
          {JSON.stringify(input, null, 2)}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Tool result card — collapsed by default
// ---------------------------------------------------------------------------

function ToolResultCard({ output, toolUseId }: { output: string; toolUseId: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="mb-1">
      <button
        type="button"
        className="btn btn-outline-info btn-sm d-flex align-items-center gap-1"
        style={{ fontSize: "0.75rem" }}
        onClick={() => setOpen((v) => !v)}
      >
        <span>tool result</span>
        <span className="text-muted" style={{ fontSize: "0.65rem" }}>({toolUseId.slice(0, 8)})</span>
        <span>{open ? "▾" : "▸"}</span>
      </button>
      {open && (
        <pre
          className="bg-light border rounded p-2 mt-1 mb-0"
          style={{ fontSize: "0.72rem", maxHeight: "16rem", overflowY: "auto" }}
        >
          {output || "(empty)"}
        </pre>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Single message bubble
// ---------------------------------------------------------------------------

function MessageBubble({ msg }: { msg: MessageRecord }) {
  const isUser = msg.role === "user";
  const isAssistant = msg.role === "assistant";
  const isTool = msg.role === "tool";

  const ts = msg.ts ? new Date(msg.ts).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";

  if (isTool) {
    return (
      <div className="d-flex justify-content-start mb-2">
        <div style={{ maxWidth: "85%" }}>
          <ToolResultCard
            output={msg.tool_result?.output ?? msg.text}
            toolUseId={msg.tool_result?.tool_use_id ?? ""}
          />
          {ts && <div className="text-muted" style={{ fontSize: "0.65rem" }}>{ts}</div>}
        </div>
      </div>
    );
  }

  return (
    <div
      className={`d-flex mb-3 ${isUser ? "justify-content-end" : "justify-content-start"}`}
    >
      <div style={{ maxWidth: "80%" }}>
        <div
          className={`rounded-3 px-3 py-2 ${
            isUser
              ? "bg-primary text-white"
              : "bg-light border"
          }`}
          style={{ fontSize: "0.875rem", lineHeight: "1.5" }}
        >
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

          {/* Empty assistant message (just tool use) */}
          {isAssistant && !msg.text?.trim() && (!msg.tool_uses || msg.tool_uses.length === 0) && (
            <span className="text-muted fst-italic" style={{ fontSize: "0.8rem" }}>(no text)</span>
          )}
        </div>
        {ts && (
          <div
            className={`mt-1 text-muted ${isUser ? "text-end" : ""}`}
            style={{ fontSize: "0.65rem" }}
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
        // Auto-scroll to bottom on first load
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
      {/* Breadcrumb */}
      <nav className="mb-3 small">
        <Link to="/">← Projects</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}`}>Backlog</Link>
        <span className="mx-2 text-muted">|</span>
        <Link to={`/p/${slug}/sessions`}>Sessions</Link>
        <span className="mx-2 text-muted">|</span>
        Messages
      </nav>

      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h2 className="mb-0" style={{ fontSize: "1.2rem" }}>
          Session transcript
          <code
            className="ms-2 text-muted fw-normal"
            style={{ fontSize: "0.8rem" }}
          >
            {claude_uuid}
          </code>
        </h2>
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
            <div className="mt-1 small text-muted">
              The session log file may not exist yet or may belong to a different user.
            </div>
          )}
        </div>
      )}

      {/* Loading */}
      {messages === null && !error && (
        <p className="text-muted">Loading transcript…</p>
      )}

      {/* Empty state */}
      {messages !== null && messages.length === 0 && (
        <div className="text-center py-5">
          <p className="text-muted fs-5">No messages in this session yet.</p>
          <p className="text-muted small">Messages appear here once the Claude session exchanges data.</p>
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
