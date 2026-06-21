import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, ProjectDetail } from "../api";
import { PersonalNotificationPanel } from "../components/PersonalNotificationPanel";

// T-0156: per-project Telegram binding settings.
//
// tg_chat may be a normal DM chat id or a (negative) group/supergroup id.
// tg_topic_id is the optional forum-thread (topic) id — only meaningful for
// a forum-enabled group, so it's disabled until a chat id is entered. Both
// are persisted to projects.toml via PUT /api/projects/:slug/tg and the
// worker re-reads them so project-bound sends (tg_notify, stall escalation)
// land in the right thread.

export function ProjectSettings() {
  const { slug = "" } = useParams<{ slug: string }>();
  const [proj, setProj] = useState<ProjectDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);

  const [chat, setChat] = useState<string>("");
  const [topic, setTopic] = useState<string>("");

  function load() {
    setError(null);
    api
      .project(slug)
      .then((p) => {
        setProj(p);
        setChat(p.tg_chat ?? "");
        setTopic(p.tg_topic_id == null ? "" : String(p.tg_topic_id));
      })
      .catch((e) => setError(String(e)));
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function validate(): string | null {
    const c = chat.trim();
    if (c && !/^-?\d+$/.test(c)) {
      return "Chat id must be an integer (negative for groups) or empty.";
    }
    const t = topic.trim();
    if (t) {
      if (!/^\d+$/.test(t) || Number.parseInt(t, 10) <= 0) {
        return "Topic id must be a positive integer or empty.";
      }
      if (!c) {
        return "Topic id requires a group chat id.";
      }
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
      const t = topic.trim();
      const result = await api.setProjectTg(slug, {
        tg_chat: chat.trim(),
        tg_topic_id: t ? Number.parseInt(t, 10) : null,
      });
      setProj((p) =>
        p ? { ...p, tg_chat: result.tg_chat, tg_topic_id: result.tg_topic_id } : p,
      );
      setNotice("Saved. The bot will now post project messages here.");
    } catch (e) {
      setError(String(e));
    } finally {
      setSaving(false);
    }
  }

  async function testPing() {
    setNotice(null);
    setError(null);
    setTesting(true);
    try {
      const r = await api.testProjectTg(slug);
      setNotice(
        r.sent
          ? "Test ping sent — check the bound chat/thread."
          : "Worker accepted the ping but suppressed it (debounce or quiet hours). Try again shortly.",
      );
    } catch (e) {
      setError(String(e));
    } finally {
      setTesting(false);
    }
  }

  return (
    <div className="container py-4" style={{ maxWidth: "720px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "1rem" }}>
        {proj?.display_name ?? slug} — settings
      </h2>

      {error && <div className="alert alert-danger">{error}</div>}
      {notice && <div className="alert alert-success py-2">{notice}</div>}

      {proj === null && !error && <div className="mc-loading">Loading</div>}

      {proj && (
        <section className="mb-4">
          <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
            Telegram binding
          </h3>
          <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.75rem" }}>
            Bind this project to a Telegram chat. Use a negative id for a group
            or supergroup. For a forum (topic-enabled) group, add the topic id
            so the bot posts into a specific thread.
          </p>

          <div className="mb-3">
            <label htmlFor="ps-tg-chat" className="form-label" style={{ fontSize: "0.72rem" }}>
              Chat id
            </label>
            <input
              id="ps-tg-chat"
              type="text"
              className="form-control"
              value={chat}
              onChange={(e) => setChat(e.target.value)}
              placeholder="e.g. -1001234567890 (group) or 404580642 (DM)"
              style={{ width: "22rem", fontFamily: "var(--mc-mono)" }}
            />
          </div>

          <div className="mb-3">
            <label htmlFor="ps-tg-topic" className="form-label" style={{ fontSize: "0.72rem" }}>
              Topic id <span style={{ color: "var(--mc-text-dim)" }}>(optional, forum thread)</span>
            </label>
            <input
              id="ps-tg-topic"
              type="text"
              className="form-control"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              placeholder="e.g. 42 — leave blank for the group's general feed"
              disabled={!chat.trim()}
              style={{ width: "12rem", fontFamily: "var(--mc-mono)" }}
            />
            <div>
              <small style={{ color: "var(--mc-text-dim)" }}>
                Find a topic id by copying a message link from the thread — it's
                the second number in <code>t.me/c/&lt;chat&gt;/&lt;topic&gt;/&lt;msg&gt;</code>.
              </small>
            </div>
          </div>

          <div className="d-flex gap-2">
            <button
              type="button"
              className="btn btn-primary"
              onClick={save}
              disabled={saving}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              className="btn btn-outline-secondary"
              onClick={testPing}
              disabled={testing || !proj.tg_chat?.trim()}
              title={proj.tg_chat?.trim() ? "Send a test message to the saved binding" : "Save a chat id first"}
            >
              {testing ? "Sending…" : "Send test ping"}
            </button>
          </div>
          <small style={{ color: "var(--mc-text-dim)", display: "block", marginTop: "0.5rem" }}>
            Test ping uses the <em>saved</em> binding — save first, then test.
          </small>
        </section>
      )}

      {/* T-0339: caps/budget reference. Resource caps are a server-wide policy
          (not per-project), but the operator-facing read+set home now lives in
          this project's process (sessions) view — so point there from settings
          instead of leaving caps unmentioned here. */}
      {proj && (
        <>
          <hr style={{ borderColor: "var(--mc-border, #333)", margin: "1.5rem 0" }} />
          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Resource caps &amp; budget
            </h3>
            <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.5rem" }}>
              Caps (max parallel sessions + the per-quota-period output-token
              budget) are a <strong>server-wide</strong> policy the worker
              enforces at spawn-time. The operator-facing read+set controls — and
              the budget-anchor status — live in this project&apos;s{" "}
              <Link to={`/p/${slug}/sessions`}>process view</Link> (the
              Agent-sessions page, the Task-Manager home). The underlying
              enforcement config is in <Link to="/system-settings">System Settings</Link>.
            </p>
          </section>
        </>
      )}

      {/* T-0358: agent-instruction editing relocated here as a thin link (it
          used to be the "AGENT WORKFLOW" nav page — a ~30k-px raw dump of the
          agents' manuals). The page itself now collapses each manual by
          default; this is the only nav affordance into it. Admin-leaning
          internals live under this "Advanced" heading. */}
      {proj && (
        <>
          <hr style={{ borderColor: "var(--mc-border, #333)", margin: "1.5rem 0" }} />
          <section className="mb-4">
            <h3 style={{ fontSize: "0.85rem", fontWeight: 600, marginBottom: "0.5rem" }}>
              Advanced
            </h3>
            <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.4rem" }}>
              <Link to={`/p/${slug}/workflow`}>Agent instructions &amp; workflow</Link>{" "}
              — view / edit the manuals and role briefings each session receives
              (AGENTS.md, AGENT_INSTRUCTIONS.md, constitution, roles).
            </p>
            {/* T-0359: dev/prod clone topology — git/devops plumbing, demoted
                here from the per-project rail. */}
            <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: "0.4rem" }}>
              <Link to={`/p/${slug}/clones`}>Clones &amp; installation topology</Link>{" "}
              — dev/prod clone health (ahead/behind, working-tree state, paths).
              Admin-only actions are gated inside the page.
            </p>
            {/* T-0361: Autonomous was an orphan URL (no nav entry). Reach it
                here; the page collapses to an enable toggle + sleep window
                while disabled. */}
            <p style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)", marginBottom: 0 }}>
              <Link to={`/p/${slug}/autonomous`}>Autonomous</Link>{" "}
              — one-task-at-a-time orchestrator (off by default; enable + set a
              sleep window here).
            </p>
          </section>
        </>
      )}

      {/* T-0218 — personal (per-user) notification target, inherited
          global → server → project. Distinct from the project-wide binding
          above (which notifies everyone). */}
      {proj && (
        <>
          <hr style={{ borderColor: "var(--mc-border, #333)", margin: "1.5rem 0" }} />
          <PersonalNotificationPanel slug={slug} />
        </>
      )}
    </div>
  );
}
