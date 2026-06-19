# deploy-recipes — version-controlled per-project deploy recipes (T-0205)

Each `<slug>/<target>.sh` here is the shell the bot-squad **worker** executes
on the host during a deploy of project `<slug>` to `<target>` (e.g.
`watchrobot/staging.sh`). Previously these lived ONLY as untracked runtime data
at `data/<slug>/deploy/<target>.sh`, so changes bypassed review/history and had
to be hand-edited live (the 2026-06-08 watchrobot staging wedge). They now have
a tracked SSOT here.

## How the worker picks the recipe

`worker/bot_squad_worker/deploy.py::_recipe_path` resolves, in order:

1. **Tracked** — `<install_root>/deploy-recipes/<slug>/<target>.sh` (this dir,
   shipped into the install at `/home/www/bot-squad` by the install's git
   ff-merge on every bot-squad deploy). **Preferred when present.**
2. **Legacy fallback** — `data/<slug>/deploy/<target>.sh` (untracked runtime
   copy). Used only for projects not yet migrated here, so adoption is
   incremental and un-tracked projects keep working unchanged.

Because the tracked file IS what runs, there is no copy step and no silent
drift: a hand-edit of the runtime `data/` copy is simply ignored once a tracked
copy exists.

## Changing a recipe (review, not live hand-edit)

1. Edit `deploy-recipes/<slug>/<target>.sh` in the dev clone.
2. `bsq commit -m '...' --ack deploy-recipes/<slug>/<target>.sh` → review →
   push `origin/bot_squad/dev`.
3. A **bot-squad** staging deploy ff-merges it into the install root.
4. The next deploy of `<slug>/<target>` runs the reviewed recipe.

So a recipe change for any project rides bot-squad's own review + deploy gate.

## Runtime conventions (unchanged)

The recipe runs via `bash <recipe_path>` with cwd = the exec clone for the
target (`project.repo_for_target`): the deploy clone when configured, else the
dev clone (staging) / master clone (prod). stdout+stderr → `data/<slug>/_jobs/
deploy/runs/<id>.log`. Recipes read their tree via `REPO="$(pwd)"`, so living
in the bot-squad repo does not change which project tree they operate on.
