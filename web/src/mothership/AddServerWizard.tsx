/**
 * Add-server install wizard (T-0031). Replaces the minimal AddServer
 * form that T-0024 shipped: this is the §3.0–§3.3 answer-picker the
 * stakeholder spec'd in chapter I of the multi-server-installation-
 * process initiative.
 *
 * Shape:
 *   1. Display-name + base-url form → POST /api/m/servers (one mint,
 *      reused across every branch the user explores; no re-mint on
 *      branch flips).
 *   2. Four stacked Q sections, revealed lazily as the user answers:
 *      §3.0 invite-join (coming-soon stub, T-0026 dependency),
 *      §3.1 claude on target, §3.2 claude via ssh, §3.3 self-help.
 *   3. Auto-transition to /m/servers/:id (the ServerProgress view that
 *      T-0024 shipped) on the first SSE checkpoint event.
 *
 * Reversibility: all answers live in a single WizardAnswers reducer
 * (see ./wizard.ts), so the user can flip §3.0 to "yes" and back without
 * losing the ssh-target they typed under §3.2. The visibility rules are
 * a pure function of the answer state — see `visibleSections` for the
 * full chain.
 *
 * Idempotency on tab close: the install_token is single-issue per the
 * seam contract, so a reload past mint cannot resume the wizard. The
 * user lands on /m, sees the pending server, and clicks through to the
 * ServerProgress view (which SSE-replays its checkpoint log). This
 * matches the DoD's "resume to the right step" requirement at the
 * system level.
 *
 * Detach: this module is imported only from web/src/mothership/, which
 * the default build tree-shakes via VITE_MOTHERSHIP=0.
 */
import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { mothershipApi, type NewServer } from "./api";
import {
  buildClaudePromptOnTarget,
  buildClaudePromptViaSsh,
  buildCurlOneLiner,
  buildInviteClaudePrompt,
  initialAnswers,
  inviteWhatComesNext,
  parseInvitePaste,
  renderChatAgentPrompt,
  visibleSections,
  type WizardAnswers,
} from "./wizard";
// `?raw` ships the file contents as a literal string at build time.
// Source-of-truth template lives at scripts/install/chat-agent-prompt.txt
// (sibling to install.sh and bootstrap-claude-instructions.md). The
// mothership BE does NOT serve this file at /i/<token>/prompt.txt — the
// wizard substitutes it client-side instead (T-0044 F-7 resolution).
// `@install/...` is a vite alias (see web/vite.config.ts, T-0050) so
// Rollup can resolve the out-of-tree path during the production build.
import chatAgentPromptTemplate from "@install/chat-agent-prompt.txt?raw";

export function AddServerWizard() {
  const navigate = useNavigate();
  const [displayName, setDisplayName] = useState("");
  const [baseUrl, setBaseUrl] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [minted, setMinted] = useState<NewServer | null>(null);
  const [answers, setAnswers] = useState<WizardAnswers>(initialAnswers());

  // Auto-transition on first installer checkpoint. The SSE replay
  // emits a `: replay-complete` comment line (EventSource swallows
  // comment frames), so the very first `message` event we see is a
  // genuine checkpoint — exactly the trigger the DoD calls for.
  useEffect(() => {
    if (!minted) return;
    const url = `/api/m/servers/${encodeURIComponent(minted.id)}/checkpoints`;
    const es = new EventSource(url, { withCredentials: true });
    const onMessage = () => {
      es.close();
      navigate(`/m/servers/${encodeURIComponent(minted.id)}`);
    };
    es.addEventListener("message", onMessage);
    return () => {
      es.removeEventListener("message", onMessage);
      es.close();
    };
  }, [minted, navigate]);

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setSubmitError(null);
    try {
      const out = await mothershipApi.createServer(
        displayName.trim(),
        baseUrl.trim(),
      );
      setMinted(out);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  }

  if (!minted) {
    return (
      <ServerForm
        displayName={displayName}
        baseUrl={baseUrl}
        submitting={submitting}
        error={submitError}
        onDisplayName={setDisplayName}
        onBaseUrl={setBaseUrl}
        onSubmit={onSubmit}
        onCancel={() => navigate("/m")}
      />
    );
  }

  return (
    <WizardBody minted={minted} answers={answers} onAnswers={setAnswers} />
  );
}

// ---------------------------------------------------------------------------
// Form step
// ---------------------------------------------------------------------------

function ServerForm(props: {
  displayName: string;
  baseUrl: string;
  submitting: boolean;
  error: string | null;
  onDisplayName: (v: string) => void;
  onBaseUrl: (v: string) => void;
  onSubmit: (e: React.FormEvent) => void;
  onCancel: () => void;
}) {
  return (
    <div className="container py-4" style={{ maxWidth: 560 }}>
      {/* T-0132: page identity for screen-readers + heading-scan navigation.
          The visual `mc-section-title` chrome stays put; the h1 sits above
          it semantically so a11y tools land here first. */}
      <h1 className="visually-hidden">Add a server</h1>
      <div className="mc-section-title">Mothership · add server</div>
      <h2 className="mc-wizard-step-heading">Step 1 — Name your server</h2>
      <p style={{ color: "var(--mc-text-dim)", fontSize: "0.85rem", marginTop: "0.5rem" }}>
        Step 1 of 2 — name the server and tell us where it'll live. We'll
        mint a single-use install token, then walk you through getting
        bot-squad installed on it.
      </p>
      <form onSubmit={props.onSubmit} style={{ display: "grid", gap: "0.75rem", marginTop: "1rem" }}>
        <LabeledInput
          label="display name"
          autoFocus
          value={props.displayName}
          placeholder="e.g. my-cohort-server"
          onChange={props.onDisplayName}
        />
        <LabeledInput
          label="base url"
          value={props.baseUrl}
          placeholder="https://bot-squad.example.com"
          onChange={props.onBaseUrl}
        />
        {props.error && (
          <div className="mc-badge mc-badge-danger" style={{ alignSelf: "start" }}>
            {props.error}
          </div>
        )}
        <div style={{ display: "flex", gap: "0.5rem" }}>
          <button
            type="submit"
            disabled={props.submitting}
            className="mc-badge mc-badge-info"
            style={{ padding: "8px 18px", cursor: props.submitting ? "wait" : "pointer", background: "transparent", fontSize: 12 }}
          >
            {props.submitting ? "issuing…" : "issue install link →"}
          </button>
          <button
            type="button"
            onClick={props.onCancel}
            className="mc-badge mc-badge-dim"
            style={{ padding: "8px 18px", cursor: "pointer", background: "transparent", fontSize: 12 }}
          >
            cancel
          </button>
        </div>
      </form>
    </div>
  );
}

function LabeledInput(props: {
  label: string;
  value: string;
  placeholder?: string;
  autoFocus?: boolean;
  onChange: (v: string) => void;
}) {
  return (
    <label style={{ display: "grid", gap: 4, fontSize: 12, letterSpacing: "0.05em" }}>
      <span style={{ color: "var(--mc-text-dim)", textTransform: "uppercase" }}>{props.label}</span>
      <input
        required
        autoFocus={props.autoFocus}
        value={props.value}
        placeholder={props.placeholder}
        onChange={(e) => props.onChange(e.target.value)}
        style={{
          padding: "6px 10px",
          background: "var(--mc-surface)",
          border: "1px solid var(--mc-border)",
          color: "var(--mc-text)",
          fontFamily: "var(--mc-mono)",
          fontSize: 13,
          borderRadius: 3,
        }}
      />
    </label>
  );
}

// ---------------------------------------------------------------------------
// Wizard body — stacked §3.0…§3.3 sections
// ---------------------------------------------------------------------------

function WizardBody(props: {
  minted: NewServer;
  answers: WizardAnswers;
  onAnswers: (next: WizardAnswers) => void;
}) {
  const { minted, answers, onAnswers } = props;
  // Origin is the canonical mothership URL on this build (the wizard
  // is served from it). Stripping any trailing slash mirrors the BE's
  // _substitute_bundle, so prompts the wizard renders match what the
  // backend would emit.
  const mothershipBase = window.location.origin;
  const sections = visibleSections(answers);

  return (
    <div className="container py-4" style={{ maxWidth: 760 }}>
      {/* T-0132: same page identity as the form step — h1 stays "Add a
          server" across both wizard states so a screen-reader user
          hears one consistent label. */}
      <h1 className="visually-hidden">Add a server</h1>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem" }}>
        <div className="mc-section-title">Mothership · install wizard</div>
        <Link
          to={`/m/servers/${encodeURIComponent(minted.id)}`}
          className="mc-badge mc-badge-dim"
          style={{ textDecoration: "none", padding: "6px 12px", fontSize: 12 }}
        >
          watch install progress →
        </Link>
      </div>
      <p style={{ color: "var(--mc-text-dim)", fontSize: "0.85rem", marginTop: "0.5rem" }}>
        Install link issued for <code style={{ fontFamily: "var(--mc-mono)" }}>{minted.id}</code>.
        Pick the option that matches your setup — you can flip between
        them. As soon as the installer reports in, this page jumps to
        the live progress view.
      </p>

      <div style={{ display: "grid", gap: "1rem", marginTop: "1.25rem" }}>
        {sections.includes("q30") && (
          <Q30Section
            value={answers.joining}
            onChange={(v) => onAnswers({ ...answers, joining: v })}
            invitePaste={answers.invitePaste}
            onInvitePaste={(v) =>
              onAnswers({ ...answers, invitePaste: v })
            }
            mothershipBase={mothershipBase}
          />
        )}
        {sections.includes("q31") && (
          <Q31Section
            value={answers.claudeOnTarget}
            onChange={(v) => onAnswers({ ...answers, claudeOnTarget: v })}
            mothershipBase={mothershipBase}
            token={minted.install_token}
          />
        )}
        {sections.includes("q32") && (
          <Q32Section
            value={answers.claudeViaSsh}
            onChange={(v) => onAnswers({ ...answers, claudeViaSsh: v })}
            sshTarget={answers.sshTarget}
            onSshTarget={(v) => onAnswers({ ...answers, sshTarget: v })}
            mothershipBase={mothershipBase}
            token={minted.install_token}
          />
        )}
        {sections.includes("q33") && (
          <Q33Section
            mothershipBase={mothershipBase}
            token={minted.install_token}
            installUrl={minted.install_url}
          />
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Section primitives
// ---------------------------------------------------------------------------

function Section(props: {
  title: string;
  subtitle?: string;
  children: React.ReactNode;
}) {
  return (
    <section
      style={{
        border: "1px solid var(--mc-border)",
        borderRadius: 4,
        padding: "1rem 1.1rem",
        background: "var(--mc-surface)",
      }}
    >
      {/* T-0132: promote each visible wizard step to an <h2> so screen-
          reader users and heading-scan tools land on the section labels.
          Visual styling is preserved by keeping the inline styles. */}
      <h2 style={{ fontSize: 13, color: "var(--mc-text)", margin: "0 0 0.25rem", fontWeight: 600 }}>
        {props.title}
      </h2>
      {props.subtitle && (
        <div style={{ fontSize: 12, color: "var(--mc-text-dim)", marginBottom: "0.6rem" }}>
          {props.subtitle}
        </div>
      )}
      {props.children}
    </section>
  );
}

function YesNoButtons(props: {
  value: "yes" | "no" | null;
  onChange: (v: "yes" | "no") => void;
  yesLabel?: string;
  noLabel?: string;
  yesDisabled?: boolean;
  yesDisabledHint?: string;
}) {
  const baseStyle: React.CSSProperties = {
    padding: "6px 16px",
    cursor: "pointer",
    background: "transparent",
    fontSize: 12,
  };
  return (
    <div style={{ display: "flex", gap: "0.5rem", marginTop: "0.5rem" }}>
      <button
        type="button"
        onClick={() => !props.yesDisabled && props.onChange("yes")}
        disabled={props.yesDisabled}
        title={props.yesDisabled ? props.yesDisabledHint : undefined}
        className={
          props.value === "yes"
            ? "mc-badge mc-badge-info"
            : "mc-badge mc-badge-dim"
        }
        style={{ ...baseStyle, cursor: props.yesDisabled ? "not-allowed" : baseStyle.cursor, opacity: props.yesDisabled ? 0.6 : 1 }}
      >
        {props.yesLabel ?? "Yes"}
      </button>
      <button
        type="button"
        onClick={() => props.onChange("no")}
        className={
          props.value === "no"
            ? "mc-badge mc-badge-info"
            : "mc-badge mc-badge-dim"
        }
        style={baseStyle}
      >
        {props.noLabel ?? "No"}
      </button>
    </div>
  );
}

function CopyBlock(props: { text: string; label?: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <div style={{ marginTop: "0.6rem" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem", marginBottom: "0.25rem" }}>
        <span style={{ fontSize: 11, color: "var(--mc-text-dim)", textTransform: "uppercase", letterSpacing: "0.05em" }}>
          {props.label ?? "copyable"}
        </span>
        <button
          type="button"
          onClick={() => {
            navigator.clipboard
              .writeText(props.text)
              .then(() => {
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              })
              .catch(() => {
                // Clipboard access can be blocked (insecure context,
                // permissions). Surface failure inline; the user can
                // still select + copy by hand from the <pre>.
                setCopied(false);
              });
          }}
          className={copied ? "mc-badge mc-badge-ok" : "mc-badge mc-badge-info"}
          style={{ padding: "2px 10px", cursor: "pointer", background: "transparent", fontSize: 11 }}
        >
          {copied ? "copied" : "copy"}
        </button>
      </div>
      <pre
        className="mc-code-block"
        style={{
          padding: "0.6rem 0.8rem",
          margin: 0,
          background: "var(--mc-bg)",
          border: "1px solid var(--mc-border)",
          borderRadius: 3,
          overflowX: "auto",
          fontSize: 12,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {props.text}
      </pre>
    </div>
  );
}

// ---------------------------------------------------------------------------
// §3.0 — invite-join: paste an invite URL from an existing admin (T-0125)
// ---------------------------------------------------------------------------

function Q30Section(props: {
  value: "yes" | "no" | null;
  onChange: (v: "yes" | "no") => void;
  invitePaste: string;
  onInvitePaste: (v: string) => void;
  mothershipBase: string;
}) {
  return (
    <Section
      title="§3.0  Is someone already running bot-squad on this server?"
      subtitle="If yes, you're joining an existing install (invite link). If no, this is a fresh install."
    >
      <YesNoButtons
        value={props.value}
        onChange={props.onChange}
        yesLabel="Joining an existing install"
        noLabel="Fresh install"
      />
      {props.value === "yes" && (
        <InvitePasteStep
          raw={props.invitePaste}
          onRaw={props.onInvitePaste}
          mothershipBase={props.mothershipBase}
        />
      )}
    </Section>
  );
}

function InvitePasteStep(props: {
  raw: string;
  onRaw: (v: string) => void;
  mothershipBase: string;
}) {
  const parsed = parseInvitePaste(props.raw);
  const whatNext = inviteWhatComesNext(parsed.kind);
  // The §3.1 claude prompt for an invite reuses the inviter's mothership
  // URL when the paste was a full URL; otherwise we fall back to the
  // current origin (the wizard's mothership), which is correct when the
  // joining user is on the same install.
  const promptBase = parsed.mothershipBase ?? props.mothershipBase;
  return (
    <div style={{ marginTop: "0.6rem", display: "grid", gap: "0.6rem" }}>
      <div style={{ fontSize: 12, color: "var(--mc-text-dim)" }}>
        Paste the invite URL the existing admin shared with you. It looks
        like <code style={{ fontFamily: "var(--mc-mono)" }}>https://&lt;mothership&gt;/i/bsq_invite_…/install.sh</code>.
        You can also paste just the token.
      </div>
      <LabeledInput
        label="invite url or token"
        value={props.raw}
        placeholder="https://example.com/i/bsq_invite_…/install.sh"
        onChange={props.onRaw}
      />
      {parsed.error && (
        <div className="mc-badge mc-badge-warn" style={{ alignSelf: "start" }}>
          {parsed.error}
        </div>
      )}
      {parsed.token && (
        <ParsedInviteSummary
          token={parsed.token}
          kind={parsed.kind}
          mothershipBase={parsed.mothershipBase}
        />
      )}
      <div
        style={{
          padding: "0.6rem 0.8rem",
          border: "1px dashed var(--mc-border)",
          borderRadius: 3,
          color: "var(--mc-text-dim)",
          fontSize: 12,
        }}
        data-testid="invite-what-comes-next"
      >
        {whatNext}
      </div>
      {parsed.kind === "invite" && parsed.token && (
        <CopyBlock
          text={buildInviteClaudePrompt({
            mothershipBase: promptBase,
            token: parsed.token,
          })}
          label="claude prompt (invite-join)"
        />
      )}
    </div>
  );
}

function ParsedInviteSummary(props: {
  token: string;
  kind: "invite" | "install" | "unknown";
  mothershipBase: string | null;
}) {
  const badge =
    props.kind === "invite"
      ? "mc-badge mc-badge-ok"
      : props.kind === "install"
        ? "mc-badge mc-badge-danger"
        : "mc-badge mc-badge-warn";
  const label =
    props.kind === "invite"
      ? "invite token"
      : props.kind === "install"
        ? "install token (wrong branch)"
        : "unknown token";
  return (
    <div
      style={{
        display: "grid",
        gap: "0.25rem",
        padding: "0.5rem 0.7rem",
        background: "var(--mc-bg)",
        border: "1px solid var(--mc-border)",
        borderRadius: 3,
        fontSize: 12,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
        <span className={badge}>{label}</span>
        <code style={{ fontFamily: "var(--mc-mono)" }}>{props.token}</code>
      </div>
      {props.mothershipBase && (
        <div style={{ color: "var(--mc-text-dim)" }}>
          mothership:{" "}
          <code style={{ fontFamily: "var(--mc-mono)" }}>
            {props.mothershipBase}
          </code>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// §3.1 — claude already on target server
// ---------------------------------------------------------------------------

function Q31Section(props: {
  value: "yes" | "no" | null;
  onChange: (v: "yes" | "no") => void;
  mothershipBase: string;
  token: string;
}) {
  const prompt = buildClaudePromptOnTarget({
    mothershipBase: props.mothershipBase,
    token: props.token,
  });
  return (
    <Section
      title="§3.1  Do you already have claude-code on the target server?"
      subtitle="Installed for your user account on the server you want bot-squad on."
    >
      <YesNoButtons value={props.value} onChange={props.onChange} />
      {props.value === "yes" && (
        <>
          <div style={{ fontSize: 12, color: "var(--mc-text-dim)", marginTop: "0.6rem" }}>
            Open claude on the target server and paste the prompt below.
            It points claude at a tokenised instructions file the mothership
            generated for you (24 h expiry; treat as a secret).
          </div>
          <CopyBlock text={prompt} label="claude prompt" />
        </>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// §3.2 — claude on a machine with ssh access
// ---------------------------------------------------------------------------

function Q32Section(props: {
  value: "yes" | "no" | null;
  onChange: (v: "yes" | "no") => void;
  sshTarget: string;
  onSshTarget: (v: string) => void;
  mothershipBase: string;
  token: string;
}) {
  const prompt = buildClaudePromptViaSsh({
    mothershipBase: props.mothershipBase,
    token: props.token,
    sshTarget: props.sshTarget,
  });
  return (
    <Section
      title="§3.2  Do you have claude-code on a machine that can ssh into this server?"
      subtitle="The wizard will tell claude to ssh in and install bot-squad remotely."
    >
      <YesNoButtons value={props.value} onChange={props.onChange} />
      {props.value === "yes" && (
        <>
          <div style={{ marginTop: "0.6rem" }}>
            <LabeledInput
              label="ssh target (user@host)"
              value={props.sshTarget}
              placeholder="alice@host.example.com"
              onChange={props.onSshTarget}
            />
          </div>
          <CopyBlock text={prompt} label="claude prompt (with ssh target)" />
        </>
      )}
    </Section>
  );
}

// ---------------------------------------------------------------------------
// §3.3 — self-help: curl|bash + chat-agent prompt
// ---------------------------------------------------------------------------

function Q33Section(props: {
  mothershipBase: string;
  token: string;
  installUrl: string;
}) {
  const curl = buildCurlOneLiner(props.installUrl);
  const chatPrompt = renderChatAgentPrompt({
    template: chatAgentPromptTemplate,
    mothershipBase: props.mothershipBase,
    token: props.token,
  });
  return (
    <Section
      title="§3.3  No claude available — pick the route that suits you"
      subtitle="Both options run the same idempotent installer; the chat-agent prompt is for when you'd rather have an AI walk you through the steps."
    >
      <div style={{ fontSize: 12, color: "var(--mc-text-dim)" }}>
        <strong style={{ color: "var(--mc-text)" }}>Option A — I'll run it myself.</strong>{" "}
        Paste this into a shell on the target server. The installer
        prints a structured failure block if anything goes wrong; rerun
        until it succeeds.
      </div>
      <CopyBlock text={curl} label="curl install" />
      <div style={{ fontSize: 12, color: "var(--mc-text-dim)", marginTop: "1rem" }}>
        <strong style={{ color: "var(--mc-text)" }}>Option B — I have a chat-based AI agent.</strong>{" "}
        Paste this prompt into any chat-based coding assistant and follow
        its instructions. The prompt teaches the agent how to drive the
        same installer, diagnose failure blocks, and hand you off to the
        bot-squad operator session at the end.
      </div>
      <CopyBlock text={chatPrompt} label="chat-agent prompt" />
    </Section>
  );
}

export default AddServerWizard;
