# Backlog board UI editing — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox syntax for tracking.

**Goal:** Add backlog/vision/feedback edit endpoints and the UI surfaces (modals, in-place edits, task detail page, promote-to-task) so the bot-squad board is fully usable from the browser.

**Spec:** `docs/superpowers/specs/2026-05-10-spec4-backlog-ui-editing-design.md`

**Architecture:** Pure FS edits via API. Worker not involved. New atomic-write helper handles frontmatter merge + safe writes. Frontend gains a Modal component and a TaskDetail page; Vision and Feedback pages grow inline edit mode.

**Tech Stack:** Same as spec #1/#3. No new deps.

---

## File structure (additions only)

```
api/app/
├── markdown_writer.py             ← NEW — atomic write + frontmatter merge for tasks
├── routes_backlog.py              ← MODIFIED — add POST/PATCH/DELETE/comments
├── routes_vision.py               ← MODIFIED — add PUT + POST new initiative
└── routes_feedback.py             ← MODIFIED — add PUT + promote

api/tests/
├── test_markdown_writer.py        ← NEW
├── test_routes_backlog.py         ← MODIFIED — write tests
├── test_routes_vision.py          ← MODIFIED — write tests
└── test_routes_feedback.py        ← MODIFIED — write tests

web/src/
├── api.ts                         ← MODIFIED — write methods
├── App.tsx                        ← MODIFIED — register /p/:slug/t/:id
├── components/
│   ├── Modal.tsx                  ← NEW
│   └── TaskCard.tsx               ← NEW (extracted from Project.tsx)
├── pages/
│   ├── Project.tsx                ← MODIFIED — new-task modal + card menu
│   ├── TaskDetail.tsx             ← NEW
│   ├── Vision.tsx                 ← MODIFIED — edit toggle
│   └── Feedback.tsx               ← MODIFIED — edit toggle + promote modal
└── utils/
    └── relativeTime.ts            ← NEW
```

---

## Phase 1 — markdown_writer + backlog write endpoints

### Task 1: `markdown_writer.py` + tests

**Files:**
- Create: `api/app/markdown_writer.py`
- Create: `api/tests/test_markdown_writer.py`

Behavior:
- `write_task(path: Path, frontmatter: dict, body: str)` — writes `<path>.tmp`, then `os.rename` to atomic. Frontmatter is `yaml.safe_dump`'d; body unchanged.
- `merge_task_update(path: Path, updates: dict, body: str | None = None) -> dict` — reads existing task with `parse_task`, merges `updates` into frontmatter (keys: title/status/body permitted; others rejected if not whitelisted), bumps `updated` to now-UTC, writes atomically. Returns the new frontmatter dict.
- `append_comment(path: Path, comment_body: str, author: str) -> None` — reads body, locates `## Comments` (creates if absent), appends `### <UTC date YYYY-MM-DD> <author>\n\n<body>\n` plus blank line, writes atomically. Bumps `updated`.
- `allocate_next_id(backlog_dir: Path) -> str` — scans `T-NNNN-*.md`, returns `T-{max+1:04d}` (or `T-0001` if empty). Caller takes a flock on `backlog_dir/.lock` around allocate+write.
- `slugify(s: str, max_len: int = 60) -> str` — kebab-case ASCII-only.

Tests (≥10):
- `write_task` produces parseable file
- `write_task` writes atomically (file never partial — easier: just verify success; don't simulate crash)
- `merge_task_update` rejects unknown keys
- `merge_task_update` bumps `updated` timestamp
- `append_comment` adds `## Comments` header if missing
- `append_comment` appends to existing comments section
- `append_comment` rejects empty body
- `allocate_next_id` empty dir → `T-0001`
- `allocate_next_id` with `T-0001`, `T-0042` → `T-0043`
- `slugify` strips punctuation, lowercases, truncates to max_len
- File-level lock via `fcntl.flock` correctness (test acquires twice non-blocking, second blocks)

TDD: failing test → run → impl → run.

Commit: `api: markdown_writer with atomic writes + frontmatter merge`

### Task 2: backlog write endpoints + tests

**Files:**
- Modify: `api/app/routes_backlog.py` — add `POST /`, `PATCH /{id}`, `DELETE /{id}`, `POST /{id}/comments`
- Modify: `api/tests/test_routes_backlog.py`

Endpoints (existing prefix `/projects/{slug}/backlog`):

- `POST ""` — body `{title: str, body?: str, status?: "open"|"totest"|"reopened"|"closed"}` (default `open`). 400 on invalid status, 400 on empty title. Allocates next id, writes file, returns the parsed task.
- `PATCH "/{id}"` — body subset of `{title, body, status}`. 404 if missing, 400 on empty/invalid. Returns updated task.
- `DELETE "/{id}"` — removes file. 404 if missing. Returns `{ok: true, deleted_id: …}`.
- `POST "/{id}/comments"` — body `{body: str}`. 400 if empty. Author = `username` from JWT claims. Returns updated task.

Tests cover happy path + each error case. Use `client.post("/api/auth/login", json={"username":"testuser","password":"test"})` to authenticate.

Commit: `api: backlog write endpoints (create/edit/comment/delete)`

---

## Phase 2 — vision + feedback write endpoints

### Task 3: vision write endpoints

**Files:**
- Modify: `api/app/routes_vision.py` — add `PUT /{name}`, `POST /` (new initiative)
- Modify: `api/tests/test_routes_vision.py`

`PUT "/{name}"` — body `{content: str}`. 400 on empty content, content > 200 KB, or invalid UTF-8. Path traversal protection: name must match `^[A-Za-z0-9_-]+\.md$` OR `^initiatives/[A-Za-z0-9_-]+\.md$`. 404 if file missing (PUT replaces existing only). Returns `{ok: true}`.

`POST ""` — body `{kind: "initiative", name: str, content?: str}`. Creates `initiatives/<slug-of-name>.md` with given content (default `"# {name}\n"`). 409 if already exists.

Tests cover: write, length limit, traversal rejection, unknown name, initiative create, name collision.

Commit: `api: vision write endpoints (PUT + POST initiative)`

### Task 4: feedback write endpoints + promote

**Files:**
- Modify: `api/app/routes_feedback.py`
- Modify: `api/tests/test_routes_feedback.py`

`PUT "/{name}"` — same pattern as vision. Path validation: `^F-[A-Za-z0-9_-]+\.md$`.

`POST "/{name}/promote"` — body `{title?: str, body?: str}`. Reads the feedback file, derives default title from its first H1 or filename. Allocates a new backlog task ID. Writes the task with frontmatter `from: F-NNNN`. Appends a footer to the feedback file: `\n\n---\nPromoted to backlog task [<task_id>](../backlog/<task_id>-…md) on <date>.\n`. Returns `{ok: true, task_id}`.

Tests:
- promote with default title (from H1)
- promote with explicit title
- promote multiple times — each promote creates a new task and appends another footer
- write content-length / traversal rejection

Commit: `api: feedback write endpoints + promote-to-task`

---

## Phase 3 — frontend write methods + components

### Task 5: extend `web/src/api.ts`

**Files:**
- Modify: `web/src/api.ts`

Add typed functions for every new endpoint. Each follows the `call(...)` pattern already there. Keep existing read functions untouched.

```ts
export const api = {
  // ...existing...
  createTask: (slug: string, t: Partial<Task>) =>
    call<Task>(`/api/projects/${slug}/backlog`, { method: "POST", body: JSON.stringify(t) }),
  patchTask: (slug: string, id: string, t: Partial<Task>) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}`, { method: "PATCH", body: JSON.stringify(t) }),
  deleteTask: (slug: string, id: string) =>
    call(`/api/projects/${slug}/backlog/${id}`, { method: "DELETE" }),
  addComment: (slug: string, id: string, body: string) =>
    call<Task>(`/api/projects/${slug}/backlog/${id}/comments`, { method: "POST", body: JSON.stringify({ body }) }),
  putVision: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  newInitiative: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/vision`, { method: "POST", body: JSON.stringify({ kind: "initiative", name, content }) }),
  putFeedback: (slug: string, name: string, content: string) =>
    call(`/api/projects/${slug}/feedback/${name}`, { method: "PUT", body: JSON.stringify({ content }) }),
  promoteFeedback: (slug: string, name: string, title?: string, body?: string) =>
    call<{ task_id: string }>(`/api/projects/${slug}/feedback/${name}/promote`, { method: "POST", body: JSON.stringify({ title, body }) }),
};
```

Commit: `web: api client write methods`

### Task 6: `Modal.tsx` + `TaskCard.tsx` + `relativeTime.ts`

**Files:**
- Create: `web/src/components/Modal.tsx`
- Create: `web/src/components/TaskCard.tsx`
- Create: `web/src/utils/relativeTime.ts`
- Modify: `web/src/components/BoardColumn.tsx` to use `TaskCard`

`Modal.tsx` — Bootstrap modal wrapper, props `{ open, title, onClose, children, footer }`. Uses Bootstrap classes for show/hide; renders only when `open` is true to keep the DOM clean.

`TaskCard.tsx` — props `{ task, onMenu }`. Renders title / id / last-updated relative time / comment count. ⋯ button calls `onMenu(task, anchorEl)`.

`relativeTime.ts` — pure function, no deps:
```ts
export function relativeTime(iso: string | undefined): string {
  if (!iso) return "";
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 60_000) return "just now";
  if (ms < 3_600_000) return `${Math.floor(ms / 60_000)}m`;
  if (ms < 86_400_000) return `${Math.floor(ms / 3_600_000)}h`;
  return `${Math.floor(ms / 86_400_000)}d`;
}
```

Commit: `web: Modal + TaskCard components`

### Task 7: Project.tsx — new-task modal + card menu

**Files:**
- Modify: `web/src/pages/Project.tsx`

Top-right `+ New task` button opens a Modal with title (required), body (textarea, optional), status (dropdown). On submit, calls `api.createTask`, closes modal, refreshes board.

Each card's ⋯ button opens a small Bootstrap dropdown menu: change-status (4 options, each fires `api.patchTask` then refreshes), edit body (modal with textarea), add comment (modal with textarea), delete (confirm; calls `api.deleteTask`).

Card click (anywhere except ⋯) navigates to `/p/${slug}/t/${task.id}`.

Commit: `web: Project page modal + card menu`

### Task 8: TaskDetail.tsx

**Files:**
- Create: `web/src/pages/TaskDetail.tsx`
- Modify: `web/src/App.tsx` — register route `/p/:slug/t/:id`

Page layout:
- Title (click-to-edit input) + status dropdown in a row
- Body (textarea with Save/Cancel, only shows when in edit mode)
- Comments list (rendered as readable chunks split by `### date author` heading)
- "Add comment" textarea + Submit
- Delete + (if it has `from: F-NNNN`) "View source feedback" link
- Breadcrumb: ← Board

Loads task via `api.backlog(slug)` and finds by id (no per-task GET endpoint — keep it simple). Persists edits via `api.patchTask`.

Commit: `web: TaskDetail page with inline edits`

### Task 9: Vision.tsx + Feedback.tsx — edit mode

**Files:**
- Modify: `web/src/pages/Vision.tsx`
- Modify: `web/src/pages/Feedback.tsx`

Vision: each file gets an Edit button. Edit mode replaces the rendered `<pre>` with a `<textarea>` plus Save / Cancel. Save calls `api.putVision`; Cancel reverts to original. Top-of-page has `+ New initiative` button (Modal: name + content).

Feedback: same edit toggle. Each item also has a "Promote to task" button → Modal pre-filled with title (from feedback) and body (verbatim feedback content). Submit calls `api.promoteFeedback`. After success, navigate to the new task's detail page.

Commit: `web: Vision + Feedback edit modes + promote`

---

## Phase 4 — build, deploy, smoke

### Task 10: build + redeploy + smoke

```bash
cd /home/www/bot-squad
.venv-skip..  # api venv (no skip needed, no new deps)
cd api && .venv/bin/pytest -v       # all green, ~50 tests
cd ../web && npm run build          # tsc + vite both green
cd .. && docker compose up -d --build
sleep 5
curl -k -s https://bot-squad.dev.uzinvestapi.com/api/health
```

Browser smoke (manual, optional in autonomous mode):
- Sign in with `alexey` / `<from INITIAL_LOGIN.txt>`
- Create a task, edit it, comment on it, delete it
- Edit `vision/north-star.md` (revert immediately so you don't break the AGENTS.md auto-render that's coming in spec #Z)
- Promote a feedback item to a task

Commit: not separate — Task 10 is verification only.

Push to origin.

---

## Self-review checklist

- API: 50+ tests pass (was 32 after auth swap; +18 expected here)
- Web: `npm run build` green
- Live URL: signing in works; creating/editing tasks works; vision edit works
- No regressions on existing read endpoints
- All new endpoints are behind `require_auth` (no anonymous writes)
- All write endpoints validate input length / format
