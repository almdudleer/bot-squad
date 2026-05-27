/**
 * Pure helpers for the add-server install wizard (T-0031).
 *
 * The wizard is the four-step Q&A picker from Chapter I §§3.0–3.3 of
 * the multi-server-installation-process initiative. Each step has an
 * "outcome" panel that depends on the user's previous answers and on
 * the install_token returned by POST /api/m/servers.
 *
 * Everything in this module is pure: the React component in
 * `routes.tsx` consumes these helpers. Pure-helper isolation keeps the
 * vitest surface trivial and the JSX file shallow.
 *
 * Token-substitution keys match `routes_mothership._substitute_bundle`
 * so the FE-rendered chat-agent prompt produces a string byte-for-byte
 * identical to what the backend would emit if it served the template
 * (it doesn't, per T-0044 F-7: the wizard renders inline).
 */

export type Decision = "yes" | "no" | null;

/** Per-step answers. Held in one reducer so the user can flip a prior
 *  branch without losing later state.
 *  - `joining`        — §3.0 (invite-join). "yes" reveals the invite-paste
 *    step (T-0125); the q31..q33 chain stays hidden.
 *  - `claudeOnTarget` — §3.1 (claude already on target server).
 *  - `claudeViaSsh`   — §3.2 (claude on a machine with ssh access).
 *  - `sshTarget`      — freeform `user@host` for the §3.2 prompt. Kept
 *    independently of the §3.2 decision so flipping the answer back to
 *    "yes" restores the previously-typed target.
 *  - `invitePaste`    — raw user input on the §3.0-yes branch (URL the
 *    inviter shared, or just the token). Kept across flips so toggling
 *    §3.0 back to "yes" restores what was typed. */
export type WizardAnswers = {
  joining: Decision;
  claudeOnTarget: Decision;
  claudeViaSsh: Decision;
  sshTarget: string;
  invitePaste: string;
};

export type WizardSection = "q30" | "q31" | "q32" | "q33";

export function initialAnswers(): WizardAnswers {
  return {
    joining: null,
    claudeOnTarget: null,
    claudeViaSsh: null,
    sshTarget: "",
    invitePaste: "",
  };
}

/** Ordered list of sections to render given the current answers.
 *
 *  Visibility rules (mirrors the spec's "if no, the next question is
 *  displayed"):
 *  - §3.0 is always visible after mint.
 *  - §3.1 is revealed once §3.0 has been answered "no".
 *  - §3.2 is revealed once §3.1 has been answered "no".
 *  - §3.3 is revealed once §3.2 has been answered "no".
 *
 *  Answers persist if the user flips an earlier branch — §3.2's value is
 *  remembered even if §3.0 swaps back to "yes" and hides everything below,
 *  so flipping §3.0 back to "no" returns the user to where they were. */
export function visibleSections(answers: WizardAnswers): WizardSection[] {
  const out: WizardSection[] = ["q30"];
  if (answers.joining === "no") out.push("q31");
  else return out;
  if (answers.claudeOnTarget === "no") out.push("q32");
  else return out;
  if (answers.claudeViaSsh === "no") out.push("q33");
  return out;
}

/** §3.1 / §3.2 outcome. Self-contained text that a non-technical user
 *  can paste into a claude-code prompt without further edits. The
 *  installer's first action is to GET this same URL, so phrasing it as
 *  "fetch and follow" is accurate. */
export function buildClaudePromptOnTarget(opts: {
  mothershipBase: string;
  token: string;
}): string {
  const url = `${opts.mothershipBase.replace(/\/$/, "")}/i/${opts.token}/instructions.md`;
  return `Please fetch the bot-squad install instructions from ${url} and follow them to install bot-squad on this server. The instructions are tailored to me and contain a single-use install token — do not commit or share them.`;
}

/** §3.2 — same shape as §3.1 plus an ssh target so the prompt tells
 *  claude *which* remote box to operate on. `sshTarget` is rendered as
 *  a `<user@host>` placeholder if empty so the copy-pasta still reads
 *  sensibly. */
export function buildClaudePromptViaSsh(opts: {
  mothershipBase: string;
  token: string;
  sshTarget: string;
}): string {
  const url = `${opts.mothershipBase.replace(/\/$/, "")}/i/${opts.token}/instructions.md`;
  const target = opts.sshTarget.trim() || "<user@host>";
  return `Please ssh into ${target} and install bot-squad there. Fetch the install instructions from ${url} and follow them on the remote host (do not run them locally). The instructions are tailored to me and contain a single-use install token — do not commit or share them.`;
}

/** §3.3 self-help one-liner. Identical shape to the curl-pipe-bash that
 *  `chat-agent-prompt.txt` references, so a user who picks "I have a
 *  chat agent" and a user who picks "I'll do it myself" run the same
 *  command. */
export function buildCurlOneLiner(installUrl: string): string {
  return `curl -fsSL "${installUrl}" | bash`;
}

/** §3.3 chat-agent prompt. Identical key set + semantics as
 *  `_substitute_bundle` in `api/app/routes_mothership.py`. We only
 *  substitute the two keys the chat-agent template uses — the install
 *  bundle's other keys (`__CLONE_URL__`, `__REPO_REF__`) are
 *  build-time inputs for the shell script and never appear in the
 *  chat-agent variant. */
export function renderChatAgentPrompt(opts: {
  template: string;
  mothershipBase: string;
  token: string;
}): string {
  return opts.template
    .replace(/__INSTALL_TOKEN__/g, opts.token)
    .replace(/__MOTHERSHIP_URL__/g, opts.mothershipBase.replace(/\/$/, ""));
}

// ---------------------------------------------------------------------------
// §3.0-yes — invite-paste parser (T-0125)
// ---------------------------------------------------------------------------

/** Token prefixes mirror `api/app/install_tokens.py`. The FE only ever
 *  inspects the prefix to *classify* a pasted token; the canonical
 *  validation happens server-side (`/installer/join` for invites,
 *  `/installer/connect` for installs). */
export const INSTALL_TOKEN_PREFIX = "bsq_install_";
export const INVITE_TOKEN_PREFIX = "bsq_invite_";

export type ParsedInviteKind = "invite" | "install" | "unknown";

export type ParsedInvitePaste = {
  /** Extracted token (everything between `/i/` and the next path segment,
   *  OR the raw input if it looks like a bare token). `null` when the
   *  input is empty / unparseable. */
  token: string | null;
  /** Classification by prefix — drives the "what comes next" copy and
   *  whether the wizard treats the paste as a usable invite. */
  kind: ParsedInviteKind;
  /** Optional mothership URL recovered from a pasted full URL — `null`
   *  when the user pasted a bare token. The §3.1/§3.2-style "claude
   *  prompt" we build for the invite path needs this base. */
  mothershipBase: string | null;
  /** Human-readable problem with the input, or `null` when the parse
   *  succeeded enough to surface a token. */
  error: string | null;
};

/** Parse the §3.0-yes paste box. Accepts three shapes:
 *  - Full URL: `https://host/i/<token>/install.sh` (or `instructions.md`)
 *  - Bare token: `bsq_invite_<...>` / `bsq_install_<...>`
 *  - Anything else → `kind: "unknown"` so the UI can guide the user.
 *
 *  Intentionally permissive: we don't validate token length or character
 *  set on the client. The server is authoritative; a mistyped paste will
 *  bounce off `/installer/join` with a 400 or 410. We only need enough
 *  parse to (a) extract the token for display + downstream prompts and
 *  (b) classify by prefix so we can render the right "what comes next"
 *  text and refuse install tokens on the join branch.
 */
export function parseInvitePaste(raw: string): ParsedInvitePaste {
  const trimmed = (raw ?? "").trim();
  if (!trimmed) {
    return { token: null, kind: "unknown", mothershipBase: null, error: null };
  }
  // URL form. `URL` throws on invalid input — fall through to the bare-
  // token branch when parsing fails. Match `/i/<token>/...` per the
  // bundle router contract in routes_mothership.py.
  if (/^https?:\/\//i.test(trimmed)) {
    let parsed: URL;
    try {
      parsed = new URL(trimmed);
    } catch {
      return {
        token: null,
        kind: "unknown",
        mothershipBase: null,
        error: "That doesn't look like a valid URL.",
      };
    }
    const m = parsed.pathname.match(/^\/i\/([^/]+)(?:\/.*)?$/);
    if (!m) {
      return {
        token: null,
        kind: "unknown",
        mothershipBase: null,
        error:
          "URL must be of the form https://<mothership>/i/<token>/install.sh",
      };
    }
    const token = decodeURIComponent(m[1]);
    return {
      token,
      kind: classifyToken(token),
      mothershipBase: `${parsed.protocol}//${parsed.host}`,
      error: null,
    };
  }
  // Bare-token form: just check the prefix; no URL recovered.
  return {
    token: trimmed,
    kind: classifyToken(trimmed),
    mothershipBase: null,
    error: null,
  };
}

function classifyToken(token: string): ParsedInviteKind {
  if (token.startsWith(INVITE_TOKEN_PREFIX)) return "invite";
  if (token.startsWith(INSTALL_TOKEN_PREFIX)) return "install";
  return "unknown";
}

/** Compose the invite URL the joining user should paste into claude on
 *  the target server. Mirrors `buildClaudePromptOnTarget` but uses
 *  invite-flavored phrasing — the joining flow attaches an additional
 *  Linux user to an EXISTING install, so we never mention reverse-proxy,
 *  domain, or TLS guidance (those are settled by the inviter's install). */
export function buildInviteClaudePrompt(opts: {
  mothershipBase: string;
  token: string;
}): string {
  const url = `${opts.mothershipBase.replace(/\/$/, "")}/i/${opts.token}/install.sh`;
  return `Please join the existing bot-squad install at ${url}. Run that installer on this server under my Linux user — it adds my account to the running bot-squad and hands me off to my operator session. The URL contains a single-use invite token — do not commit or share it.`;
}

/** "What comes next" copy diverges by token type. Invite tokens skip
 *  reverse-proxy / domain / TLS guidance — those are settled by the
 *  install the user is joining. Returns plain text the section component
 *  renders inside a callout. */
export function inviteWhatComesNext(kind: ParsedInviteKind): string {
  if (kind === "invite") {
    return (
      "You're joining an existing bot-squad install. The installer will " +
      "add your Linux user to the running install and hand you off to " +
      "your operator session — it skips reverse-proxy, domain, and TLS " +
      "setup, since those are already in place on this server."
    );
  }
  if (kind === "install") {
    return (
      "That looks like a fresh-install token, not an invite. Use the " +
      'fresh-install flow ("No, I\'m doing a fresh install") instead — ' +
      "this branch is for joining an existing install."
    );
  }
  return (
    "Paste the URL the existing bot-squad admin shared with you. It " +
    "looks like `https://<mothership>/i/bsq_invite_…/install.sh`."
  );
}
