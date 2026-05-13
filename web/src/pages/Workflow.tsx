import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { api, VisionFile } from "../api";
import { PageHelp } from "../components/PageHelp";

/**
 * Workflow — stakeholder-set governance + per-role briefings.
 * Constitution: who has authority, how decisions are made.
 * Roles: the per-role context each session receives at startup.
 */
type RoleKind = "teamlead" | "dev";

// A context reference: a file or synthetic source the agent receives or is
// asked to consult. Renders as an in-page anchor, a cross-page route link,
// a plain path label, or a synthetic (non-file) line.
type CtxRef =
  | { kind: "anchor"; label: string; anchor: string }
  | { kind: "route"; label: string; to: string }
  | { kind: "synthetic"; label: string; title?: string }
  | { kind: "path"; label: string };

// AGENTS.md (repo) — only file that Claude Code reads on EVERY turn (cached).
function everyTurnRefs(_role: RoleKind): CtxRef[] {
  return [
    { kind: "anchor", label: "AGENTS.md (repo)", anchor: "wf-agents-md" },
  ];
}

// Loaded at SessionStart (once per pane, plus once after /compact). Kept
// small on purpose — heavy specs are pointed at, not pasted in.
function sessionStartRefs(role: RoleKind, slug: string): CtxRef[] {
  const roleFile: CtxRef =
    role === "teamlead"
      ? { kind: "anchor", label: "vision/roles/teamlead.md", anchor: "wf-role-teamlead" }
      : { kind: "anchor", label: "vision/roles/dev.md", anchor: "wf-role-dev" };
  const refs: CtxRef[] = [
    { kind: "route", label: "vision/product.md (Roadmap)", to: `/p/${slug}/vision` },
    roleFile,
  ];
  if (role === "teamlead") {
    // Devs are focused on one task — listing the whole backlog is noise + a
    // scope-expansion temptation. Hook gates this section to TL only.
    refs.push({
      kind: "synthetic",
      label: "Open backlog headlines (status open/in_progress/reopened, up to 30)",
    });
  }
  refs.push(
    { kind: "synthetic", label: "Active sessions list" },
    { kind: "anchor", label: "vision/team_protocol.md", anchor: "wf-team-protocol" },
    { kind: "synthetic", label: "Pointers to the on-demand block documents (see below)" },
  );
  return refs;
}

// Heavy or situational — agents are told to read these on their first action
// or when they need them. NOT piped into context.
function onDemandRefs(role: RoleKind, slug: string): CtxRef[] {
  const common: CtxRef[] = [
    { kind: "anchor", label: "AGENT_INSTRUCTIONS.md (bot-squad — recipes / gotchas)", anchor: "wf-agent-instructions" },
    { kind: "route", label: "Active initiative full spec (Roadmap)", to: `/p/${slug}/vision` },
    { kind: "anchor", label: "vision/constitution.md", anchor: "wf-constitution" },
  ];
  if (role === "teamlead") {
    return [
      ...common,
      { kind: "path", label: `data/${slug}/backlog/T-*.md (open tasks)` },
      { kind: "path", label: `data/${slug}/feedback/ (raw JTBD evidence)` },
    ];
  }
  return [
    ...common,
    { kind: "path", label: `data/${slug}/backlog/<your task_id>-*.md (your scope + DoD)` },
  ];
}

export function Workflow() {
  const { slug = "" } = useParams();
  const [agentsMd, setAgentsMd] = useState<{ name: string; content: string } | null>(null);
  const [agentInstructions, setAgentInstructions] = useState<VisionFile | null>(null);
  const [teamProtocol, setTeamProtocol] = useState<VisionFile | null>(null);
  const [constitution, setConstitution] = useState<VisionFile | null>(null);
  const [roles, setRoles] = useState<VisionFile[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [editing, setEditing] = useState<{ name: string; draft: string } | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Collapse state for per-role context-source rows (closed by default).
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  function reload() {
    api
      .vision(slug)
      .then((list) => {
        setConstitution(list.find((f) => f.name === "constitution.md") ?? null);
        setRoles(list.filter((f) => f.name.startsWith("roles/")));
        setAgentInstructions(list.find((f) => f.name === "AGENT_INSTRUCTIONS.md") ?? null);
        setTeamProtocol(list.find((f) => f.name === "team_protocol.md") ?? null);
      })
      .catch((e) => setError(String(e)));
    api
      .repoAgentsMd(slug)
      .then((r) => setAgentsMd({ name: "AGENTS.md", content: r.content }))
      .catch((e) => {
        // 404 means file missing — show empty editor seed so user can create it.
        if (String(e).includes("404")) {
          setAgentsMd({ name: "AGENTS.md", content: "" });
        } else {
          setError(String(e));
        }
      });
  }

  useEffect(() => {
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slug]);

  function startEdit(name: string, content: string) {
    setEditing({ name, draft: content });
    setSaveError(null);
  }

  async function saveEdit() {
    if (!editing) return;
    setSaving(true);
    setSaveError(null);
    try {
      if (editing.name === "AGENTS.md") {
        await api.putRepoAgentsMd(slug, editing.draft);
      } else {
        await api.putVision(slug, editing.name, editing.draft);
      }
      setEditing(null);
      reload();
    } catch (e) {
      setSaveError(String(e));
    } finally {
      setSaving(false);
    }
  }

  function renderEditor(name: string, content: string, rows = 14) {
    const isEditing = editing?.name === name;
    if (!isEditing) return <pre className="mc-pre">{content}</pre>;
    return (
      <>
        {saveError && <div className="alert alert-danger py-1 small">{saveError}</div>}
        <textarea
          className="form-control mb-2"
          rows={rows}
          value={editing!.draft}
          onChange={(e) => setEditing({ ...editing!, draft: e.target.value })}
          autoFocus
        />
        <div className="d-flex gap-2">
          <button type="button" className="btn btn-primary btn-sm" onClick={saveEdit} disabled={saving}>
            {saving ? "Saving…" : "Save"}
          </button>
          <button type="button" className="btn btn-secondary btn-sm" onClick={() => setEditing(null)}>
            Cancel
          </button>
        </div>
      </>
    );
  }

  function renderDocSection(opts: {
    id: string;
    name: string;
    label: string;
    content: string;
  }) {
    const { id, name, label, content } = opts;
    const isEditing = editing?.name === name;
    return (
      <section key={id} id={id} className="mb-4">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            {label} <span style={{ color: "var(--mc-text-mid)" }}>· {name}</span>
          </div>
          {!isEditing && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => startEdit(name, content)}
            >
              Edit
            </button>
          )}
        </div>
        {renderEditor(name, content)}
      </section>
    );
  }

  function renderCtxRef(ref: CtxRef, idx: number) {
    if (ref.kind === "synthetic" || ref.kind === "path") {
      return (
        <div
          key={idx}
          title={ref.kind === "synthetic" ? ref.title : undefined}
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.78rem",
            color: "var(--mc-text-dim)",
            padding: "0.15rem 0",
          }}
        >
          {ref.label}
        </div>
      );
    }
    if (ref.kind === "route") {
      return (
        <div key={idx} style={{ padding: "0.15rem 0" }}>
          <Link
            to={ref.to}
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.78rem",
              color: "var(--mc-accent)",
              textDecoration: "none",
            }}
          >
            {ref.label}
          </Link>
        </div>
      );
    }
    return (
      <div key={idx} style={{ padding: "0.15rem 0" }}>
        <a
          href={`#${ref.anchor}`}
          style={{
            fontFamily: "var(--mc-mono)",
            fontSize: "0.78rem",
            color: "var(--mc-accent)",
            textDecoration: "none",
          }}
        >
          {ref.label}
        </a>
      </div>
    );
  }

  function renderCollapsibleRow(key: string, label: string, refs: CtxRef[]) {
    const isOpen = !!expanded[key];
    return (
      <div className="mb-2" style={{ borderTop: "1px solid var(--mc-border, #444)" }}>
        <button
          type="button"
          className="btn btn-link p-0 py-2"
          style={{ fontFamily: "var(--mc-mono)", fontSize: "0.78rem", textAlign: "left" }}
          onClick={() => setExpanded((s) => ({ ...s, [key]: !s[key] }))}
        >
          {isOpen ? "▾" : "▸"} {label}
        </button>
        {isOpen && (
          <div style={{ paddingLeft: "1.2rem", paddingBottom: "0.5rem" }}>
            {refs.map((r, i) => renderCtxRef(r, i))}
          </div>
        )}
      </div>
    );
  }

  function renderRoleSection(r: VisionFile) {
    const base = r.name.replace(/^roles\//, "").replace(/\.md$/, "");
    const label = base.charAt(0).toUpperCase() + base.slice(1);
    const role: RoleKind = base === "dev" ? "dev" : "teamlead";
    const anchorId = role === "dev" ? "wf-role-dev" : "wf-role-teamlead";
    const isEditing = editing?.name === r.name;
    return (
      <section key={r.name} id={anchorId} className="mb-4">
        <div className="d-flex justify-content-between align-items-center mb-2">
          <div
            style={{
              fontFamily: "var(--mc-mono)",
              fontSize: "0.72rem",
              color: "var(--mc-text-dim)",
              textTransform: "uppercase",
              letterSpacing: "0.06em",
            }}
          >
            Role · {label} <span style={{ color: "var(--mc-text-mid)" }}>· {r.name}</span>
          </div>
          {!isEditing && (
            <button
              type="button"
              className="btn btn-outline-secondary btn-sm"
              style={{ fontSize: "0.72rem" }}
              onClick={() => startEdit(r.name, r.content)}
            >
              Edit
            </button>
          )}
        </div>
        {renderEditor(r.name, r.content)}
        <div className="mt-2">
          {renderCollapsibleRow(
            `${role}-every-turn`,
            "Always-on (every turn, Claude Code native)",
            everyTurnRefs(role),
          )}
          {renderCollapsibleRow(
            `${role}-session-start`,
            "Loaded at session start (once per pane, plus once after /compact)",
            sessionStartRefs(role, slug),
          )}
          {renderCollapsibleRow(
            `${role}-ondemand`,
            "Asked to consult on demand",
            onDemandRefs(role, slug),
          )}
        </div>
      </section>
    );
  }

  return (
    <div className="container py-4" style={{ maxWidth: "860px" }}>
      <h2 style={{ fontSize: "1rem", fontWeight: 600, marginBottom: "0.5rem" }}>
        Workflow
        <span style={{ fontFamily: "var(--mc-mono)", fontWeight: 400, color: "var(--mc-text-dim)", fontSize: "0.78rem", marginLeft: "0.5rem" }}>/ {slug}</span>
      </h2>
      <PageHelp>
        Stakeholder-set governance + per-role briefings.
        <strong> AGENTS.md</strong> (repo) is the only file Claude Code reads on
        every turn. <strong>AGENT_INSTRUCTIONS.md</strong> (bot-squad) is heavier
        long-form context — agents are pointed at it and read it on demand.
        <strong> Constitution</strong>: governance + hard rules.
        <strong> Roles</strong>: the per-role briefing each session sees at
        startup. Teamlead role applies to sessions without a task; dev role
        applies to sessions spawned for a specific task. Expand the rows under
        each role to see exactly which files land in context, when, and which
        are consulted on demand.
      </PageHelp>

      {error && <div className="alert alert-danger">{error}</div>}
      {agentsMd === null && agentInstructions === null && constitution === null && roles === null && !error && (
        <div className="mc-loading">Loading</div>
      )}

      {agentsMd && renderDocSection({
        id: "wf-agents-md",
        name: "AGENTS.md",
        label: "Repo · AGENTS.md (always-cached, every turn)",
        content: agentsMd.content,
      })}

      {agentInstructions && renderDocSection({
        id: "wf-agent-instructions",
        name: agentInstructions.name,
        label: "Bot-squad · AGENT_INSTRUCTIONS.md (on-demand reference)",
        content: agentInstructions.content,
      })}

      {teamProtocol && renderDocSection({
        id: "wf-team-protocol",
        name: teamProtocol.name,
        label: "Bot-squad · team_protocol.md (loaded at session start)",
        content: teamProtocol.content,
      })}

      {constitution && renderDocSection({
        id: "wf-constitution",
        name: constitution.name,
        label: "Constitution",
        content: constitution.content,
      })}

      {roles && roles.length > 0 && (
        <>
          <h3 style={{ fontSize: "0.86rem", fontWeight: 600, marginTop: "1.5rem", marginBottom: "0.5rem" }}>
            Roles
          </h3>
          {roles.map((r) => renderRoleSection(r))}
        </>
      )}
    </div>
  );
}
