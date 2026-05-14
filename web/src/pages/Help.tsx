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
          <li><strong>BOT·SQUAD wordmark</strong> (top of sidebar) — returns to the project picker. The LED dot beside it shows worker liveness (green = operational, red = offline).</li>
          <li><strong>[PROJECT] section</strong> — shown when you are inside a project. Lists the per-project pages (BOARD, VISION, FEEDBACK, SESSIONS, RUNS, AUTONOMOUS). The selected project stays pinned even if you click a [SYSTEM] item.</li>
          <li><strong>[SYSTEM] section</strong> — always visible. ALL PROJECTS returns to the picker; SCHEDULER shows worker jobs; HELP is this page.</li>
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
          The board at <code>/p/&lt;project&gt;</code> shows all tasks grouped into four columns:
        </p>
        <ul>
          <li><strong>Open</strong> — ready to be picked up by an agent or the stakeholder.</li>
          <li><strong>To Test</strong> — implementation complete; needs review / QA.</li>
          <li><strong>Reopened</strong> — was closed, but a follow-up issue was found.</li>
          <li><strong>Closed</strong> — done and merged to production.</li>
        </ul>
        <h3>Adding a task</h3>
        <p>
          Click <strong>+ New task</strong> (top-right of the board). Fill in a title (required),
          an optional body (markdown, used as the spec for agents), and choose an initial status.
        </p>
        <h3>Editing a task</h3>
        <p>
          Click the <code>⋯</code> menu on any task card to: change status, edit body, add a
          comment, or delete. Clicking the card itself navigates to the full task detail view.
        </p>
        <h3>Deleting a task</h3>
        <p>
          Open the <code>⋯</code> menu → <strong>Delete</strong>. A confirmation prompt appears.
          Deletion is permanent.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="taskdetail">
        <h2>Task detail</h2>
        <p>
          Navigate to any task card to open its detail view at <code>/p/&lt;slug&gt;/t/&lt;id&gt;</code>.
        </p>
        <ul>
          <li><strong>Title</strong> — click the title text to edit inline; press Enter or click away to save.</li>
          <li><strong>Status</strong> — change via the dropdown in the top-right of the detail view.</li>
          <li><strong>Body</strong> — click <strong>Edit</strong> to open a full-text editor (markdown). The body is the specification that agents read; keep it precise and actionable.</li>
          <li><strong>Comments</strong> — append timestamped notes. Comments are appended to the body as <code>### heading / text</code> blocks; they are never overwritten by agents.</li>
        </ul>
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
        <h2>Feedback</h2>
        <p>
          The feedback section at <code>/p/&lt;slug&gt;/feedback</code> holds raw markdown notes —
          user research, bug reports, observations — that have not yet been turned into tasks.
        </p>
        <h3>Promote to task</h3>
        <p>
          Click <strong>Promote to task</strong> on any feedback file. A modal appears with the
          extracted title (from the first H1) and the full content pre-filled as the task body.
          Adjust the title if needed and click <strong>Create task</strong>. The new task appears
          on the backlog board at status <em>open</em>, and the feedback file is retained for reference.
        </p>
        <p>
          This pattern makes it easy to turn evidence-backed observations into actionable work
          without losing the original context.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="sessions">
        <h2>Sessions</h2>
        <p>
          Sessions are Claude agent processes running inside tmux panes on the server.
          The sessions list at <code>/p/&lt;slug&gt;/sessions</code> shows all active and paused
          panes for a project.
        </p>
        <ul>
          <li><strong>active</strong> — Claude is running; the pane is live.</li>
          <li><strong>paused</strong> — the pane has been killed; Claude session state (the <code>--resume</code> UUID) is preserved. You can resume it later.</li>
        </ul>
        <h3>Pause</h3>
        <p>
          Click <strong>Pause</strong> on an active session. The tmux pane is destroyed but the
          Claude UUID is saved so the session can be resumed from where it left off.
        </p>
        <h3>Resume</h3>
        <p>
          Click <strong>Resume</strong> on a paused session. A new tmux pane is opened and
          Claude is launched with <code>--resume &lt;UUID&gt;</code>.
        </p>
        <h3>New session</h3>
        <p>
          Click <strong>+ New session</strong>. Provide a window name (used as the tmux window
          name, e.g. <code>spec7-work</code>) and an optional initial prompt that will be sent
          to Claude immediately after it starts.
        </p>
        <div className="mc-help-callout">
          <strong>Telegram tip:</strong> when Claude needs your input it sends a Telegram
          notification in the format <code>[SID] needs your input</code>. Reply directly to that
          message — your text is routed into that session automatically. No need to open the web
          UI just to answer a question.
        </div>

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
        <h2>Runs</h2>
        <p>
          The runs list at <code>/p/&lt;slug&gt;/runs</code> shows the deploy history.
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
          The scheduler dashboard at <code>/scheduler</code> shows the APScheduler worker
          running on the server. It displays:
        </p>
        <ul>
          <li><strong>Worker uptime</strong> — how long the worker process has been running.</li>
          <li><strong>Last heartbeat</strong> — seconds since the last heartbeat tick. Green (healthy) means &lt; 2 minutes. Red means the worker may be stuck or down.</li>
          <li><strong>Scheduled jobs</strong> — each registered job with its cron/interval trigger and next scheduled run time.</li>
        </ul>
        <p>
          The scheduler drives autonomous mode ticks, session health checks, and background
          maintenance tasks. It refreshes automatically every 30 seconds.
        </p>
      </section>

      {/* ------------------------------------------------------------------ */}
      <section className="mc-help-section" id="autonomous">
        <h2>Autonomous mode</h2>
        <p>
          Autonomous mode at <code>/p/&lt;slug&gt;/autonomous</code> allows the orchestrator to
          work through the backlog without manual session management.
        </p>
        <h3>What it does</h3>
        <ul>
          <li>Picks the highest-priority <em>open</em> task from the backlog.</li>
          <li>Spawns a Claude session to implement it.</li>
          <li>When the session ends, runs a definition-of-done (DOD) review.</li>
          <li>If passing, marks the task <em>to test</em>; if failing, marks it <em>reopened</em>.</li>
          <li>Sleeps between configurable UTC hours (default 22:00 – 08:00) — no new tasks are spawned during the sleep window, but in-flight work continues.</li>
        </ul>
        <h3>Enabling</h3>
        <p>
          Click <strong>Enable</strong>. A confirmation dialog summarises the sleep window.
          Confirm to start the orchestrator. You will receive Telegram notifications as tasks
          are started and completed.
        </p>
        <h3>Disabling</h3>
        <p>
          Click <strong>Disable</strong>. The current in-flight task finishes naturally; no new
          tasks are picked up after the current one completes.
        </p>
        <h3>Sleep window</h3>
        <p>
          Edit the <em>Sleep start</em> and <em>Sleep end</em> (UTC hours, 0–23) and click
          <strong>Save</strong>. The new window takes effect on the next tick.
        </p>
        <div className="mc-help-callout">
          <strong>Caution:</strong> once a task is in-flight (an agent pane is running) it
          cannot be stopped mid-flight. Disable autonomous mode before the orchestrator picks
          up the next task if you want to pause cleanly.
        </div>
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
          <code>prod</code>. The reason is logged and shown in the Runs table.
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
          Runs interface or directly.
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
