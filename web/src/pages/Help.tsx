import { Link } from "react-router-dom";

/**
 * /help — Public usage manual. Accessible without login.
 */
export function Help() {
  return (
    <div className="mc-help-page">
      {/* Hero */}
      <div className="mc-help-hero">
        <h1>BOT-SQUAD</h1>
        <p>
          Usage manual and reference. Use the section headings below to navigate.
          This page is always reachable at <code>/help</code> — no login required.
        </p>
      </div>

      {/* Back link (shown only when logged in context) */}
      <div style={{ marginBottom: "2rem" }}>
        <Link to="/" style={{ fontSize: "0.78rem", color: "var(--mc-text-dim)" }}>
          ← Back to projects
        </Link>
      </div>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="what-is">
        <h2>What is bot-squad?</h2>
        <p>
          bot-squad is a private mission-control dashboard for managing a team of autonomous
          Claude agents that work on software projects. You maintain a prioritised backlog and
          a north-star vision; the agents pick up tasks, implement them inside tmux panes, and
          request deploy approvals — all visible from a single interface. The stakeholder steers
          direction through the web UI and Telegram notifications; agents do the implementation
          work without requiring hand-holding for every step.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="login">
        <h2>Login</h2>
        <p>
          Navigate to <code>/login</code>. Enter your username and password (no Telegram OAuth —
          plain credentials). Credentials are stored as bcrypt hashes in <code>auth.toml</code>
          on the server; contact your sysadmin to add or rotate accounts.
        </p>
        <p>After a successful login you are redirected to the project picker at <code>/</code>.</p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="navigating">
        <h2>Navigating</h2>
        <p>
          A sticky header bar is visible on every authenticated page. It shows:
        </p>
        <ul>
          <li><strong>BOT·SQUAD wordmark</strong> (top of sidebar) — returns to the project picker.</li>
          <li><strong>Header icon cluster</strong> beside the wordmark — <strong>▦ Fleet / admin</strong> (super-admin only, on multi-server installs), <strong>⚙ Server settings</strong> (admin-only — scheduler jobs, global resource-cap notes), and <strong>? Help</strong> (this page, always visible).</li>
          <li><strong>Worker-health pill</strong> — appears below the header only when something&apos;s wrong (no recent heartbeat, or the worker&apos;s running build drifted from the API image). Silent when healthy.</li>
          <li><strong>Project section</strong> — shown when you are inside a project. Primary rail: <strong>BOARD, VISION, PROCESSES</strong>. A collapsible <strong>More / Ops</strong> disclosure holds the lower-frequency lookup surfaces: DOCS (includes Feedback), ANALYTICS, DEPLOYMENT QUEUE. The selected project stays pinned even if you click a header icon.</li>
          <li><strong>Footer</strong> — your username + Sign out.</li>
        </ul>
        <p>
          The active page has an amber left-border in the sidebar so it&apos;s easy to see where you are.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="backlog">
        <h2>Backlog board</h2>
        <p>
          The board at <code>/p/&lt;project&gt;</code> presents the canonical task
          lifecycle as <strong>four states</strong> — <strong>Backlog → In progress →
          Validating → Done</strong> — shown as the overview strip above the columns.
          Each state groups one or more of the richer internal statuses the system
          tracks underneath:
        </p>
        <ul>
          <li><strong>Backlog</strong> — queued, not yet being worked. Internal statuses:{" "}
            <em>Planned</em> (drafted, not yet ready), <em>Open</em> (ready to pick up), and{" "}
            <em>Reopened</em> (was closed, but a follow-up issue was found).</li>
          <li><strong>In progress</strong> — actively being worked by an agent. Internal status:{" "}
            <em>In progress</em>.</li>
          <li><strong>Validating</strong> — implementation complete; needs review / QA. Internal
            status: <em>To Test</em>.</li>
          <li><strong>Done</strong> — finished and merged. Internal status: <em>Closed</em>.</li>
        </ul>
        <p>
          The four canonical states are the conceptual model; the internal statuses are a
          non-destructive refinement (no task is renamed).
        </p>
        <p>
          The board is <strong>read-only</strong>: click a card to open its full detail
          view. Creating a task, editing its body/status, or adding a comment all happen
          via the Telegram dialog — reply in chat and the change lands on the task.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="taskdetail">
        <h2>Task detail</h2>
        <p>
          Navigate to any task card to open its detail view at <code>/p/&lt;slug&gt;/t/&lt;id&gt;</code>.
        </p>
        <ul>
          <li><strong>Title, status, initiative binding</strong> — read-only facts.</li>
          <li><strong>The ask</strong> — the original verbatim request, read-only.</li>
          <li><strong>Process working area</strong> — the progress-note / negotiation log, plus past comments (newest first), read-only.</li>
        </ul>
        <p>
          Editing the body, changing status, or adding a comment all happen via the
          Telegram dialog now — the task detail page is for looking things up, not
          driving them.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="vision">
        <h2>Vision</h2>
        <p>
          The vision is a set of markdown files at <code>/p/&lt;slug&gt;/vision</code> that define
          the product direction. Organised in layers:
        </p>
        <ul>
          <li><strong>North-star</strong> — the ultimate purpose of the project in one sentence.</li>
          <li><strong>Strategy</strong> — the 6–18 month bet: what to build and what not to build.</li>
          <li><strong>Tactical</strong> — near-term priorities; what agents should focus on this week/sprint.</li>
          <li><strong>Initiatives</strong> — individual initiatives you can add with <strong>+ New initiative</strong>. Each is a separate markdown file you name and edit freely.</li>
        </ul>
        <p>
          Agents read the vision files at the start of each session to stay aligned with the
          product direction. Edit liberally — changes take effect on the next session spawn.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="feedback">
        <h2>Docs &amp; Feedback</h2>
        <p>
          The <strong>Docs &amp; Artifacts</strong> view at <code>/p/&lt;slug&gt;/docs</code>{" "}
          is one read-first lookup surface over a shared, cross-linked tree of two artifact
          kinds: <strong>📄 Docs</strong> (design docs, decisions) and{" "}
          <strong>💬 Feedback</strong> (raw notes — user research, bug reports, observations —
          not yet turned into tasks). Use the <strong>All / 📄 Docs / 💬 Feedback</strong>{" "}
          filter to narrow the tree to one kind; a <strong>show closed</strong> checkbox
          reveals resolved feedback when the Feedback (or All) filter is active.
        </p>
        <p>
          This page is pure read/lookup — creating a doc, editing one, promoting a
          feedback item to a task, or linking a doc to a ticket all happen via the
          Telegram dialog now.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      {/* T-0383: heading uses the process vocab (matches the Processes nav). */}
      <section className="mc-help-section" id="sessions">
        <h2>Processes</h2>
        <p>
          Sessions are Claude agent processes running inside tmux panes on the server.
          The processes list at <code>/p/&lt;slug&gt;/sessions</code> is a live, read-only
          status board for all active and paused panes whose CWD is this project&apos;s
          repo — the system spawns, reuses, and reaps them for you.
        </p>
        <ul>
          <li><strong>active</strong> — Claude is running; the pane is live.</li>
          <li><strong>paused</strong> — the pane has been killed; Claude session state (the <code>--resume</code> UUID) is preserved so the system can resume it later.</li>
        </ul>
        <p>
          Pausing, resuming, and starting new sessions are no longer web actions —
          steer day-to-day via the Telegram dialog instead.
        </p>
        <div className="mc-help-callout">
          <strong>Telegram tip:</strong> when Claude needs your input it sends a Telegram
          notification in the format <code>[SID] needs your input</code>. Reply directly to that
          message — your text is routed into that session automatically. No need to open the web
          UI just to answer a question.
        </div>

        <h3>Resource caps</h3>
        <p>
          One write control remains on this page: the <strong>Resources &amp; quota</strong>{" "}
          panel lets an admin edit <strong>Max parallel sessions</strong>,{" "}
          <strong>Max total tokens</strong>, and the <strong>Idle-suspend window</strong> in
          place, next to the usage numbers it&apos;s constraining. Click{" "}
          <strong>Save caps</strong> to apply — it&apos;s a fresh read on each spawn, no
          restart needed.
        </p>

        <h3 id="tmux-cheatsheet">Tmux cheatsheet</h3>
        <p>
          Sessions run inside <code>tmux</code> on the server. Connect over SSH and use these
          commands to move around. All shortcuts assume the default prefix <code>Ctrl-b</code>
          (press the prefix, release, then press the next key).
        </p>
        <ul>
          <li><code>tmux ls</code> — list sessions on this host.</li>
          <li><code>tmux a -t &lt;name&gt;</code> — attach to a session by name.</li>
          <li><code>Ctrl-b d</code> — detach from the current session (leaves it running).</li>
          <li><code>Ctrl-b w</code> — interactive window/session picker.</li>
          <li><code>Ctrl-b n</code> / <code>Ctrl-b p</code> — next / previous window.</li>
          <li><code>Ctrl-b &lt;0-9&gt;</code> — switch directly to window N.</li>
          <li><code>Ctrl-b c</code> — open a new window.</li>
          <li><code>Ctrl-b %</code> / <code>Ctrl-b &quot;</code> — split pane vertically / horizontally.</li>
          <li><code>Ctrl-b [</code> — enter scroll-back / copy mode (arrow keys + PageUp; press <code>q</code> to exit).</li>
          <li><code>Ctrl-b &amp;</code> — kill the current window (asks for confirmation).</li>
        </ul>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="runs">
        <h2>Deployment Queue</h2>
        <p>
          The deployment queue at <code>/p/&lt;slug&gt;/runs</code> shows the deploy history.
          Each row is a deploy request with status, target environment, reason, timing, and a
          link to the full log.
        </p>
        <ul>
          <li><strong>queued</strong> — deploy request received, not yet started.</li>
          <li><strong>processing</strong> — deploy script is running.</li>
          <li><strong>ok</strong> — deploy succeeded.</li>
          <li><strong>fail</strong> — deploy script exited non-zero; check the log.</li>
        </ul>
        <h3>Viewing a log</h3>
        <p>
          Click <strong>Log</strong> on any row. The full stdout/stderr from the deploy script
          is shown. Logs over 2 MB are truncated by default; click <strong>Load full log</strong>
          to see everything.
        </p>
        <h3>Triggering a deploy (agent command)</h3>
        <p>
          Agents queue deploys by running from inside the project repo:
        </p>
        <pre><code>ops/bot-squad-bin/deploy &lt;target&gt; "&lt;reason&gt;"
# example:
ops/bot-squad-bin/deploy staging "feat: add session pause endpoint"</code></pre>
        <p>
          Before deploying, agents should squash their branch commits:
        </p>
        <pre><code>BASE=$(git merge-base HEAD master)
git reset --soft "$BASE"
git commit -m "feat: concise description of what this branch does"</code></pre>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="messages">
        <h2>Messages</h2>
        <p>
          Click the SID (session ID) link in the sessions table to open the message transcript
          at <code>/p/&lt;slug&gt;/sessions/&lt;uuid&gt;/messages</code>. This shows the full
          Claude conversation in chronological order: user turns, assistant turns, tool calls
          (collapsed by default), and tool results.
        </p>
        <p>
          Useful for auditing what an agent did, reviewing reasoning, or copying a prompt that
          worked well. Transcripts are paginated; click <strong>Load earlier messages</strong>
          at the top to fetch older messages.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="scheduler">
        <h2>Scheduler</h2>
        <p>
          Scheduler jobs live on <code>/system-settings</code> (admin-only) — expand the{" "}
          <strong>▸ Scheduler jobs</strong> disclosure near the bottom of the page. It lists
          every registered job with its <strong>trigger</strong> (cron/interval) and{" "}
          <strong>next run</strong> time, refreshed every 30 seconds.
        </p>
        <p>
          Worker uptime and heartbeat health aren&apos;t shown there — that&apos;s the
          failure-only worker-health pill in the sidebar header instead (see{" "}
          <a href="#navigating">Navigating</a>), silent while the worker is healthy.
        </p>
        <p>
          The scheduler drives autopilot watchdog ticks, session health checks, and background
          maintenance tasks.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="telegram">
        <h2>Telegram bot (@bot_squad_bot)</h2>
        <p>
          The bot handles outbound notifications and inbound replies. It does not replace the
          web UI — it complements it for quick interactions while away from a desk.
        </p>
        <h3>Notification format</h3>
        <pre><code>[SID] needs your input
&lt;optional context message from Claude&gt;</code></pre>
        <p>
          Replying to that specific message routes your text directly into the session identified
          by SID. Claude receives it as a user turn and continues.
        </p>
        <h3>Bot commands</h3>
        <ul>
          <li><code>/sessions</code> — list active and paused sessions across all projects.</li>
          <li><code>/say &lt;sid&gt; &lt;text&gt;</code> — send text to a specific session by SID.</li>
          <li><code>/state</code> — drive on/off, quota target, core lifecycle state.</li>
          <li><code>/help</code> — show bot command reference.</li>
        </ul>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="agent-commands">
        <h2>Agent commands</h2>
        <p>
          These are commands that Claude sessions run inside the project repo, not commands for
          the human operator.
        </p>
        <h3>Deploy</h3>
        <pre><code>ops/bot-squad-bin/deploy &lt;target&gt; "&lt;reason&gt;"</code></pre>
        <p>
          Queues a deploy run. <code>target</code> is typically <code>staging</code> or
          <code>prod</code>. The reason is logged and shown in the Deployment Queue table.
        </p>
        <h3>Squash before deploy</h3>
        <pre><code>BASE=$(git merge-base HEAD master)
git reset --soft "$BASE"
git commit -m "feat(scope): one-line summary of the branch"</code></pre>
        <p>
          This squashes all branch commits into one clean commit before the deploy script runs.
          Run this once, then call the deploy command.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="branching">
        <h2>Branching</h2>
        <p>
          Agents work on the branch <code>bot_squad/dev</code> (or project-specific variant).
          They <strong>never</strong> push directly to <code>master</code>, force-push, or merge
          branches — those operations are reserved for the stakeholder.
        </p>
        <p>
          When a feature is complete and deployed to staging, the stakeholder reviews and merges
          the branch to <code>master</code> manually, then triggers a production deploy via the
          Deployment Queue interface or directly.
        </p>
        <p>
          Agents also never amend commits on <code>master</code> or rewrite shared history.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="hard-rules">
        <h2>Hard rules</h2>
        <div className="mc-help-rule">
          <strong>Never edit constitution.md.</strong> The constitution defines the operating
          constraints for all agents. Edits require explicit stakeholder instruction — not
          inference from a task body.
        </div>
        <div className="mc-help-rule">
          <strong>No destructive operations without explicit confirmation.</strong> Dropping
          databases, deleting files outside the project scope, or resetting git history require
          an explicit written instruction from the stakeholder in the current session context.
        </div>
        <div className="mc-help-rule">
          <strong>Do not push --force to shared branches.</strong> Never force-push to
          <code>master</code>, <code>staging</code>, or any shared branch. Force-push to your
          own feature branch is acceptable only when explicitly asked.
        </div>
      </section>

      {/* Footer */}
      <div
        style={{
          marginTop: "3rem",
          paddingTop: "1.5rem",
          borderTop: "1px solid var(--mc-border)",
          fontSize: "0.72rem",
          color: "var(--mc-text-dim)",
          fontFamily: "var(--mc-mono)",
        }}
      >
        bot-squad · mission control manual · <Link to="/">return to projects</Link>
      </div>
    </div>
  );
}
