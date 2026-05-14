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
 *  - `joining`        — §3.0 (invite-join). "yes" is the coming-soon stub.
 *  - `claudeOnTarget` — §3.1 (claude already on target server).
 *  - `claudeViaSsh`   — §3.2 (claude on a machine with ssh access).
 *  - `sshTarget`      — freeform `user@host` for the §3.2 prompt. Kept
 *    independently of the §3.2 decision so flipping the answer back to
 *    "yes" restores the previously-typed target. */
export type WizardAnswers = {
  joining: Decision;
  claudeOnTarget: Decision;
  claudeViaSsh: Decision;
  sshTarget: string;
};

export type WizardSection = "q30" | "q31" | "q32" | "q33";

export function initialAnswers(): WizardAnswers {
  return {
    joining: null,
    claudeOnTarget: null,
    claudeViaSsh: null,
    sshTarget: "",
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
