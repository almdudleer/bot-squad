import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { api, isNotFoundError, SessionRow, Task } from "../api";
import { MarkdownBody } from "../components/MarkdownBody";
import {
  CANONICAL_LABELS,
  CANONICAL_STATE,
  deriveParentStatus,
} from "../canonicalStatus";
import {
  isRunning,
  sessionActivity,
  sessionGlyph,
  sessionLabel,
} from "../utils/sessionStatus";

// T-0674 (D-0057 §4/§8): TaskDetail is pure read-first — every inline edit
// control (title, stage, initiative, verbatim, context, comment, dev-process
// binding, related-doc link/unlink, delete) is cut. Task mutation happens via
// the TG dialog (R5); this page is the "ticket view" lookup the north star
// names. STATUS_OPTIONS stays as a label lookup for the read-only stage text.
const STATUS_OPTIONS: { value: Task["status"]; label: string }[] = [
  { value: "planned", label: "Planned" },
  { value: "open", label: "Open" },
  { value: "in_progress", label: "In progress" },
  { value: "totest", label: "To Test" },
  { value: "reopened", label: "Reopened" },
  { value: "closed", label: "Closed" },
];

export type ProgressEntry = { ts: string; sid: string; text: string };

// T-0835: the RENDER half of the progress-note round-trip. A note is stored on
// ONE physical line — the format `- <ts> · <sid> · <text>` that this parser and
// three other consumers depend on — with its newlines/tabs escaped by
// `task_body.encode_progress_text` instead of destroyed. Structure is preserved
// at rest and expanded here, at render.
//
// A single left-to-right scan, never chained `replace`s: chaining would decode
// the output of an earlier step (`\\n` → `\n` → newline) and turn text the
// author wrote literally into a line break. An unknown escape (`\s` in a note
// quoting `re.sub(r"\s+", ...)`) is left exactly as it was, which is also what
// keeps every pre-T-0835 note rendering as it always did.
export function decodeNoteText(text: string): string {
  const map: Record<string, string> = { "\\": "\\", n: "\n", r: "\r", t: "\t" };
  let out = "";
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    const next = i + 1 < text.length ? text[i + 1] : "";
    if (ch === "\\" && next in map) {
      out += map[next];
      i++;
      continue;
    }
    out += ch;
  }
  return out;
}

export function parseProgressList(progress: string): ProgressEntry[] {
  if (!progress) return [];
  // Each line: "- <ts> · <sid> · <text>"
  return progress
    .split("\n")
    .map((ln) => ln.trim())
    .filter((ln) => ln.startsWith("- "))
    .map((ln) => {
      const rest = ln.slice(2);
      const parts = rest.split(" · ");
      if (parts.length < 3) return { ts: "", sid: "", text: decodeNoteText(rest) };
      return {
        ts: parts[0],
        sid: parts[1],
        text: decodeNoteText(parts.slice(2).join(" · ")),
      };
    });
}

// T-0274: progress-note timestamps render localized (matching the page
// footer's `updated …` and the session-history rows, both new Date().
// toLocaleString()), with the exact ISO preserved in a tooltip. Falls back
// to the raw string if the stored value isn't a parseable date.
function formatNoteTs(ts: string): string {
  if (!ts) return "";
  const d = new Date(ts);
  return isNaN(d.getTime()) ? ts : d.toLocaleString();
}

// T-0291: resolve a session's first-touch timestamp for the session-history
// audit trail. Prefer the persisted `session_history_ts` (stamped at bind time,
// survives suspend/archive/legacy), then fall back to the live-session
// `started_at` join, then null (rendered as `—`).
export function firstTouchTs(
  sid: string,
  historyTs: Record<string, string> | null | undefined,
  liveStartedAt: string | null | undefined,
): string | null {
  return historyTs?.[sid] ?? liveStartedAt ?? null;
}

// T-0238: the user (stakeholder) is the author of comments dropped into the
// working area. Progress notes from a real session carry an S-<...> SID; the
// stakeholder's comments carry this sentinel so the feed can label them
// "you" rather than as an agent.
export const STAKEHOLDER_SID = "S-stakeholder";

/**
 * The body text in the user-facing header — the ask, or the ticket body.
 *
 * T-0733: two different things share this slot and must NOT look the same.
 *
 * - A ticket WITH `## Verbatim request` shows the stakeholder's exact words:
 *   raw `<pre>`, green rule, never reflowed or markdown-rendered. Untouched.
 * - A LEGACY ticket (no such heading) falls back to its whole body, which on
 *   the live board means planning documents — `## 0. Headline framing`,
 *   dependency-order prose, code fences. Labelling that "what you asked for"
 *   is the same mislabelling T-0729 existed to stop, just arriving via the
 *   parser's legacy fallback instead of section absorption. So the primary fix
 *   is the LABEL: say it's the ticket body and that no separate request was
 *   recorded. Rendering the markdown and collapsing the wall are additions on
 *   top of the honest label, never substitutes for it.
 *
 * Which branch applies is the API's call (`verbatim_is_legacy`, from
 * `task_body.is_legacy_body`) — the heading rule is not re-implemented here.
 *
 * T-0737: the legacy branch's render+collapse now lives in `MarkdownBody`,
 * shared with Vision's initiative bodies. What stays here is the part that is
 * genuinely about tickets: which branch applies, and the caption.
 */
export function TaskBodyBlock({ task, slug }: { task: Task; slug: string }) {
  const text = task.verbatim?.trim() ?? "";

  if (!text || !task.verbatim_is_legacy) {
    return (
      <pre
        className="mc-pre"
        style={{
          borderLeft: "3px solid var(--mc-accent-success, #4ade80)",
          paddingLeft: "0.75rem",
          marginBottom: 0,
        }}
      >
        {text ? task.verbatim : (
          <span style={{ color: "var(--mc-text-dim)", fontStyle: "italic" }}>
            (no request recorded)
          </span>
        )}
      </pre>
    );
  }

  return (
    <div>
      <div
        className="mc-legacy-body-tag"
        title="This ticket predates the `## Verbatim request` section, so what you see is its whole body — a working document, not words recorded from you."
      >
        ▪ Ticket body — no separate request recorded
      </div>
      <MarkdownBody source={text} slug={slug} collapsible />
    </div>
  );
}

export function TaskDetail() {
  const { slug = "", id = "" } = useParams();
  const navigate = useNavigate();

  const [task, setTask] = useState<Task | null>(null);
  const [error, setError] = useState<string | null>(null);
  // T-0139: distinguish three terminal states for the initial load —
  // "loading" → spinner; "not_found" → titled panel with back-link;
  // "network_error" → retryable banner. Replaces the previous single
  // `error` string which conflated 404 with transient failures.
  const [loadState, setLoadState] = useState<"loading" | "ok" | "not_found" | "network_error">("loading");
  // T-0104: keep every session row keyed by sid so we can look up the
  // worker-derived activity for the bound dev session below. The
  // /backlog endpoint that drove `task.session` doesn't enrich with
  // the activity probe, so we join client-side from /sessions.
  const [sessionsBySid, setSessionsBySid] = useState<Record<string, SessionRow>>({});

  // T-0512 (M9): this task's subtasks (children whose parent_task === id),
  // loaded from the children endpoint. When non-empty the task is ABSTRACT —
  // its status is derived from these (see canonicalStatus.deriveParentStatus).
  const [children, setChildren] = useState<Task[]>([]);

  function loadTask() {
    setError(null);
    setLoadState("loading");
    api
      .backlog(slug)
      .then((tasks) => {
        const found = tasks.find((t) => t.id === id);
        if (!found) { setLoadState("not_found"); return; }
        setTask(found);
        setLoadState("ok");
        // Side-loads only fire once the parent project is known to exist
        // (i.e. backlog returned 200). Keeps the not-found path quiet —
        // no extra 404s on /sessions.
        api.sessions(slug)
          .then((rows) => {
            const map: Record<string, SessionRow> = {};
            for (const r of rows) map[r.sid] = r;
            setSessionsBySid(map);
          })
          .catch(() => {
            setSessionsBySid({});
          });
        // T-0512 (M9): load this task's subtasks for the Subtasks panel.
        api.children(slug, id)
          .then(setChildren)
          .catch(() => setChildren([]));
      })
      .catch((e) => {
        if (isNotFoundError(e)) { setLoadState("not_found"); return; }
        setError(String(e));
        setLoadState("network_error");
      });
  }

  useEffect(() => {
    loadTask();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug, id]);

  if (loadState === "not_found") {
    return (
      <div className="container py-4">
        <h4 style={{ fontSize: "1.05rem", fontWeight: 600, marginBottom: "0.75rem" }}>
          Task {id} not found
        </h4>
        <p style={{ fontSize: "0.85rem", color: "var(--mc-text-dim)" }}>
          No backlog entry for <code>{id}</code> in project <code>{slug}</code>.
        </p>
        <Link to={`/p/${slug}`} style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}>
          ← back to backlog
        </Link>
      </div>
    );
  }

  if (loadState === "network_error") {
    return (
      <div className="container py-4">
        <div className="alert alert-danger">
          Couldn't load task — {error}
        </div>
        <button type="button" className="btn btn-sm btn-outline-secondary" onClick={loadTask}>
          Retry
        </button>
      </div>
    );
  }

  if (loadState === "loading" || !task) {
    return (
      <div className="container py-4">
        <div className="mc-loading">Loading</div>
      </div>
    );
  }

  const progressEntries = parseProgressList(task.progress ?? "").reverse();

  return (
    <div className="container py-4" style={{ maxWidth: "800px" }}>
      {/* Back link */}
      <div className="mb-2">
        <Link
          to={`/p/${slug}`}
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.72rem",
            color: "var(--mc-text-dim)",
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            textDecoration: "none",
          }}
        >
          ← back to board
        </Link>
      </div>

      {/* ==================================================================
          USER-FACING HEADER (T-0238) — what the user asked for + the stage.
          T-0674: pure read-first — title/stage/initiative/verbatim are all
          plain facts now; task mutation happens via the TG dialog (R5).
          ================================================================== */}
      <div className="mc-task-zone mc-zone-header">
        {/* T-0733: the zone tag makes the same claim the body block does, so it
            has to tell the same truth. A legacy ticket has no recorded ask —
            calling the zone "what you asked for" is the mislabel arriving via
            the header instead of the block. */}
        <div className="mc-zone-tag">
          {task.verbatim_is_legacy
            ? "▸ User-facing — the ticket as recorded"
            : "▸ User-facing — what you asked for"}
        </div>

        {/* Title row — id + title */}
        <div className="d-flex align-items-start gap-2 mb-3">
          <span
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
              paddingTop: "0.35rem",
            }}
          >
            {task.id}
          </span>
          <div className="flex-grow-1">
            <h4
              style={{
                marginBottom: 0,
                fontSize: "1.05rem",
                fontWeight: 600,
                color: "var(--mc-text)",
              }}
            >
              {task.title}
            </h4>
          </div>
        </div>

        {/* Stage + initiative — read-only facts. */}
        <div className="d-flex flex-wrap align-items-center gap-3 mb-3" style={{ fontSize: "0.78rem" }}>
          <div className="d-flex align-items-center gap-2">
            <span style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)" }}>
              stage:
            </span>
            <span style={{ color: "var(--mc-text)" }}>
              {STATUS_OPTIONS.find((s) => s.value === task.status)?.label ?? task.status}
            </span>
          </div>
          {/* T-0038: initiative binding — read-only, links to the roadmap. */}
          <div className="d-flex align-items-center gap-2">
            <span style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)" }}>
              initiative:
            </span>
            {task.initiative && task.initiative !== "~" ? (
              <Link to={`/p/${slug}/vision#${encodeURIComponent(task.initiative)}`}>
                {task.initiative.replace(/\.md$/, "")}
              </Link>
            ) : (
              <span style={{ color: "var(--mc-text-dim)" }}>— unattached —</span>
            )}
          </div>
        </div>

        {/* T-0512 (M9 / Part A): Subtasks. When a task is split into subtasks it
            becomes ABSTRACT — its status is derived from its children's work
            ("dependent on actual work being done in terms of its subtasks").
            Show the derived canonical state + the list of subtasks. */}
        {children.length > 0 && (() => {
          const derived = deriveParentStatus(children.map((c) => c.status));
          return (
            <div className="mb-3" style={{ fontSize: "0.78rem" }}>
              <div
                className="d-flex align-items-center gap-2 mb-2"
                style={{ color: "var(--mc-text-dim)", fontFamily: "var(--mc-mono)" }}
              >
                <span>⛓ subtasks ({children.length}):</span>
                {derived && (
                  <span
                    title="Abstract task — status derived from its subtasks"
                    style={{
                      color: "var(--mc-text-mid)",
                      border: "1px solid var(--mc-border)",
                      borderRadius: "2px",
                      padding: "0 5px",
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                      fontSize: "0.62rem",
                    }}
                  >
                    {CANONICAL_LABELS[derived]}
                  </span>
                )}
              </div>
              <div>
                {children.map((c) => (
                  <div
                    key={c.id}
                    onClick={() => navigate(`/p/${slug}/t/${c.id}`)}
                    title={`${c.id} — open subtask`}
                    style={{
                      display: "flex",
                      alignItems: "center",
                      gap: "0.5rem",
                      padding: "0.25rem 0.4rem",
                      cursor: "pointer",
                      borderLeft: "2px solid var(--mc-border)",
                      marginBottom: "0.2rem",
                    }}
                  >
                    <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.68rem", color: "var(--mc-text-dim)" }}>
                      {c.id}
                    </span>
                    <span style={{ flex: "1 1 auto", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      {c.title}
                    </span>
                    <span style={{ fontFamily: "var(--mc-mono)", fontSize: "0.62rem", color: "var(--mc-text-dim)" }}>
                      {CANONICAL_LABELS[CANONICAL_STATE[c.status]]}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          );
        })()}

        {/* The ask — verbatim, read-only (T-0674: edit moved to the TG dialog).
            T-0733: legacy-shape tickets get a different label + rendering. */}
        <TaskBodyBlock task={task} slug={slug} />
      </div>

      {/* ==================================================================
          AGENT WORKING AREA (T-0238) — the progress-notes feed: agents'
          working / negotiation log + past user comments. T-0674: read-only —
          adding a comment moved to the TG dialog (R5).
          ================================================================== */}
      <div className="mc-task-zone mc-zone-work">
        <div
          className="mc-zone-tag"
          title="Where processes record progress and negotiate the work, and where your comments are recorded — newest first."
        >
          💬 Process working area — working / negotiation log + your comments
        </div>

        {progressEntries.length === 0 && (
          <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
            Nothing recorded yet.
          </p>
        )}
        {progressEntries.map((p, i) => {
          const mine = p.sid === STAKEHOLDER_SID;
          return (
            <div
              key={i}
              style={{
                background: "var(--mc-surface-raised)",
                border: "1px solid var(--mc-border)",
                borderLeft: mine ? "3px solid var(--mc-cyan)" : "1px solid var(--mc-border)",
                borderRadius: "3px",
                padding: "0.4rem 0.625rem",
                marginBottom: "0.35rem",
                fontSize: "0.78rem",
                color: "var(--mc-text-mid)",
              }}
            >
              <span
                title={p.ts}
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.68rem",
                  color: mine ? "var(--mc-cyan)" : "var(--mc-text-dim)",
                  marginRight: "0.5rem",
                }}
              >
                {formatNoteTs(p.ts)} · {mine ? "you" : p.sid}
              </span>
              {/* T-0835: pre-wrap so a note's decoded line breaks actually
                  render — decoding the escapes and then collapsing them in the
                  browser would preserve the bytes and lose the shape again, one
                  layer further out. */}
              <span style={{ whiteSpace: "pre-wrap" }}>{p.text}</span>
            </div>
          );
        })}
      </div>

      {/* ==================================================================
          TASK DETAILS — plumbing the user rarely touches. Process binding,
          TL context, related docs, and the session-history audit trail.
          T-0674: all read-only facts now.
          ================================================================== */}
      <div className="mc-task-zone mc-zone-details">
        <div className="mc-zone-tag">Task details</div>

        {/* Bound dev process — read-only. */}
        <div className="mb-3 d-flex align-items-center gap-2 flex-wrap" style={{ fontSize: "0.78rem" }}>
          <span
            style={{
              color: "var(--mc-text-dim)",
              fontFamily: "var(--mc-mono)",
              minWidth: "5.5rem",
            }}
          >
            dev process:
          </span>
          {!task.session && (
            <span style={{ color: "var(--mc-text-dim)" }}>— none —</span>
          )}
          {task.session && (
            <Link to={`/p/${slug}/sessions?sid=${encodeURIComponent(task.session.sid)}`}>
              {task.session.sid}
            </Link>
          )}
          {task.session && (() => {
            // T-0104: prefer the worker-derived activity (from /sessions
            // join via sessionsBySid) over the raw md status. Falls back
            // to the status-derived mapping in sessionActivity() when
            // the session isn't in the live list (e.g. suspended).
            const live = sessionsBySid[task.session.sid];
            const act = sessionActivity(live ?? task.session);
            const green = isRunning(act);
            return (
              <span
                style={{
                  fontFamily: "var(--mc-mono)",
                  fontSize: "0.7rem",
                  color: green
                    ? "var(--mc-accent-success, #4ade80)"
                    : "var(--mc-text-dim)",
                }}
                title={`session status: ${act}`}
              >
                {sessionGlyph(act)} {sessionLabel(act)}
              </span>
            );
          })()}
        </div>

        {/* Context — optional TL clarification. Read-only (T-0674). */}
        <div className="mb-4">
          <div className="mc-section-title" style={{ margin: 0, marginBottom: "0.5rem" }}>Context (optional)</div>
          {task.context?.trim() ? (
            <pre className="mc-pre">{task.context}</pre>
          ) : (
            <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>(no context yet)</p>
          )}
        </div>

        {/* Related docs — ticket→doc half of the T-0172 bidirectional mention.
            Read-only (T-0674): linking/unlinking moved to the TG dialog. */}
        <div className="mb-4">
          <div className="mc-section-title">Related docs ({(task.related_docs ?? []).length})</div>
          <div className="d-flex flex-wrap gap-2 align-items-center">
            {(task.related_docs ?? []).length === 0 && (
              <span style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
                No docs linked.
              </span>
            )}
            {(task.related_docs ?? []).map((d) => (
              <Link
                key={d}
                to={`/p/${slug}/docs?doc=${encodeURIComponent(d)}`}
                style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem" }}
              >
                {d}
              </Link>
            ))}
          </div>
        </div>

        {/* Session history — T-0106. Frontmatter `session_history` is an
            append-only ordered list of SIDs that have ever bound this task
            (spawn / bind_task / resume). Render chronologically; each row
            links to the Sessions page deep-linked to that SID, and shows the
            session's `started_at` (joined from the live sessions list) as a
            first-touch proxy. */}
        <div>
          <div className="mc-section-title">
            Process history ({(task.session_history ?? []).length})
          </div>
          {(task.session_history ?? []).length === 0 ? (
            <p style={{ fontSize: "0.8rem", color: "var(--mc-text-dim)" }}>
              No sessions have touched this task yet.
            </p>
          ) : (
            <div>
              {(task.session_history ?? []).map((sid, i) => {
                const row = sessionsBySid[sid];
                // T-0291: prefer the persisted first-touch ts (stamped at bind
                // time, survives suspend/archive) over the live-session
                // started_at join; only fall back to `—` when neither exists.
                const firstTouch = firstTouchTs(sid, task.session_history_ts, row?.started_at);
                return (
                  <div
                    key={`${sid}-${i}`}
                    style={{
                      background: "var(--mc-surface-raised)",
                      border: "1px solid var(--mc-border)",
                      borderRadius: "3px",
                      padding: "0.4rem 0.625rem",
                      marginBottom: "0.35rem",
                      fontSize: "0.78rem",
                      display: "flex",
                      alignItems: "center",
                      gap: "0.75rem",
                    }}
                  >
                    <Link
                      to={`/p/${slug}/sessions?sid=${encodeURIComponent(sid)}`}
                      style={{
                        fontFamily: "var(--mc-mono)",
                        fontSize: "0.78rem",
                        color: "var(--mc-accent)",
                        textDecoration: "none",
                      }}
                      title="Open this session on the Sessions page"
                    >
                      {sid}
                    </Link>
                    <span
                      style={{
                        fontFamily: "var(--mc-mono)",
                        fontSize: "0.7rem",
                        color: "var(--mc-text-dim)",
                        marginLeft: "auto",
                      }}
                      title={
                        firstTouch
                          ? `First touched: ${firstTouch}`
                          : "Session not in the current registry (suspended/archived/legacy) and no persisted first-touch ts"
                      }
                    >
                      {firstTouch
                        ? new Date(firstTouch).toLocaleString()
                        : "—"}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>

      {/* Actions — T-0674: "Delete task" moved to the TG dialog (R5). */}
      <div
        className="d-flex gap-2 align-items-center"
        style={{ borderTop: "1px solid var(--mc-border)", paddingTop: "1rem" }}
      >
        {task.from && (
          <Link to={`/p/${slug}/feedback`} className="btn btn-sm btn-outline-secondary">
            View source feedback ({task.from})
          </Link>
        )}
        {task.updated && (
          <span
            style={{
              marginLeft: "auto",
              fontFamily: "var(--mc-mono)",
              fontSize: "0.7rem",
              color: "var(--mc-text-dim)",
            }}
          >
            updated {new Date(task.updated).toLocaleString()}
          </span>
        )}
      </div>
    </div>
  );
}
