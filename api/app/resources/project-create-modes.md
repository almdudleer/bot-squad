# Project creation modes

bot-squad organises every project around a **mother directory** that
holds two git clones side by side:

```
~/<slug>/
  dev/      → editing surface for agents, on the deploy_branch
  master/   → separate working tree, on master, used for prod deploys
              and hotfixes without disturbing dev
  ops/      → symlink into the install's per-project data dir
              (`/home/www/bot-squad/data/<slug>`)
  .bot-squad.toml  → per-project config: paths + identity
```

The dev/master split exists so the agent team can keep working on
`dev` while an operator (or hotfix flow) deploys from `master`. The
ops symlink is shipped into BOTH clones so agents working in either
tree see the same backlog / vision / sessions data the UI sees.

When you create a project from the `+ New project` button you pick
one of three **modes**:

## 1. New from scratch (recommended for new code)

bot-squad creates everything:

- `<mother>/dev` — either `git init` locally OR `git clone <remote-url>`
  if you supplied one.
- `<mother>/master` — a second clone cut from `<mother>/dev`.
- Ops symlinks in both clones pointing at the install's data dir for
  this slug.
- The per-project `<mother>/.bot-squad.toml`.

Use this for a brand-new project, or when you're happy to bring an
existing remote URL into the canonical layout.

## 2. Attach existing, move into structure (destructive)

You have an existing repo somewhere and you want bot-squad to take
ownership of its physical layout. bot-squad will **rename** your
existing path to live inside the mother dir. Sub-choices:

- **existing → dev** — your repo becomes `<mother>/dev`; a fresh
  `<mother>/master` clone is cut from it.
- **existing → master** — your repo becomes `<mother>/master`; a
  fresh `<mother>/dev` clone is cut from it.

This mode renames a user-controlled directory and can break absolute
paths in shell history, editor sessions, and other tooling. The
wizard requires an explicit confirmation flag before proceeding and
prints the rollback command (a single `mv` back) up front.

## 3. Attach existing, paths-as-they-are (recommended for attach)

You have an existing dev clone and master clone somewhere and you
don't want bot-squad touching their physical location. bot-squad
creates only the mother dir + writes a `.bot-squad.toml` recording
the unorthodox `repo_path` and `repo_master` paths. The ops symlinks
are still planted into each clone so the agents working there see
the install's per-project data dir.

This is the stakeholder's preferred attach path — no rename, no
risk to the user's existing workflow. The trade-off is that the
mother dir doesn't physically contain the clones; tools that walk
`<mother>/dev` won't find anything. Most bot-squad code paths read
`repo_path` from `.bot-squad.toml` and don't care.

## Why does bot-squad require this structure?

- **Two clones.** dev/master split lets the deploy flow advance
  prod (from `master`) without disturbing the agent team working in
  `dev`. Same reason every multi-environment shop separates a
  working tree from a release tree.
- **Mother dir.** A stable cwd that resolves to the slug for any
  session opened at or under it (the per-user worker's slug-resolver
  walks up from cwd). Without it, two clones on opposite sides of
  the filesystem can't be addressed as one project.
- **Ops symlink in each clone.** Agents and shell tooling running
  inside a clone need a relative path (`./ops/...`) to the install's
  per-project state (backlog, vision, sessions, deploy queue). The
  symlink in the mother dir alone isn't enough — `cd dev && ls ops`
  must work.
- **`.bot-squad.toml` per project.** Records the canonical paths
  (`repo_path`, `repo_master`, `repo_workspace`). The single source
  of truth a future bot-squad install can read to attach to this
  mother dir without re-running the wizard.
