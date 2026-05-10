# Backlog board UI editing — design

**Spec date:** 2026-05-10
**Status:** awaiting user review
**Implements:** chunk #4 of the bot-squad decomposition

## 1. Goals

Make the bot-squad UI useful for stakeholder day-to-day, not just a read-only viewer:

- **Backlog board**: status edits (dropdown on each card), inline comment add, body edit, create new task, delete task
- **Vision**: edit each vision file (north-star / strategy / tactical / initiative) in a textarea-with-Save
- **Feedback**: read remains; add a one-click **Promote to task** button that creates a linked backlog task
- All edits write to the existing on-disk markdown files (`/home/www/bot-squad/data/<slug>/{backlog,vision,feedback}/`); no DB

Concurrency: last-write-wins. v1 stakeholder use is single-user mostly. Deferred: real-time sync, edit history, rich text editor, attachments.

## 2. Architecture

API stays the only writer. Worker is not involved (pure FS ops).

```
Browser ──form/click──► bot-squad-api ──PATCH/POST/PUT/DELETE──► /home/www/bot-squad/data/<slug>/...
```

### 2.1 New API endpoints (all behind `require_auth`)

```
POST   /api/projects/{slug}/backlog                  body: {title, body?, status?}
PATCH  /api/projects/{slug}/backlog/{id}             body: any subset of {title, status, body}
DELETE /api/projects/{slug}/backlog/{id}
POST   /api/projects/{slug}/backlog/{id}/comments    body: {body}
PUT    /api/projects/{slug}/vision/{name}            body: {content}
PUT    /api/projects/{slug}/feedback/{name}          body: {content}
POST   /api/projects/{slug}/feedback/{name}/promote  body: {title?, body?}  → creates backlog task, links via "from" frontmatter field
```

Authentication: every route depends on `require_auth` (the existing username/password JWT cookie).

### 2.2 On-disk schema additions

Backlog frontmatter grows two timestamp fields:

```yaml
---
id: T-0042
title: …
status: open
created: 2026-05-10T18:00:00Z
updated: 2026-05-10T18:30:00Z
from: F-0042                  # optional, only on tasks promoted from feedback
---
```

Existing tasks without `created`/`updated` get those filled with the file's mtime on first read+write. Frontmatter parser already tolerates extra keys.

ID format unchanged (`T-NNNN`). New IDs allocated as `max(existing) + 1`. Race tolerance: at the API layer, an O(1) directory scan picks the next free number; if two writes collide, the second gets the next free slot.

Comments stay as the `## Comments` section with `### YYYY-MM-DD <username>` subheadings. New comments append at the bottom; empty body rejected.

Vision and feedback files are plain markdown — PUT replaces the file content verbatim. The API rejects writes that don't pass a basic sanity check (non-empty, ≤ 200 KB, valid UTF-8).

## 3. UI

### 3.1 Backlog board (`/p/:slug`)

Existing 4-column layout, plus:

- **+ New task** button top-right → modal with title (required), body (textarea, optional), status (dropdown, default `open`)
- Each card has a small ⋯ menu opening: change status (4 statuses listed), edit body (modal), add comment (modal), delete (confirm)
- Status change is one click — no modal
- Comment add appends a `### YYYY-MM-DD <username>` block to the task body's `## Comments` section
- Cards show: title, ID, last-updated relative time, comment count badge

### 3.2 Task detail page (`/p/:slug/t/:id`)

Used when a card is clicked (instead of inline-only edit). Shows:

- Title (edit-in-place: click to edit, blur or Enter saves)
- Status dropdown
- Body (textarea with Save / Cancel)
- Comments list (rendered) with add-comment form below
- Delete + promote actions

### 3.3 Vision page (`/p/:slug/vision`)

For each file: current rendered markdown with an Edit button. Edit mode = textarea + Save / Cancel. New "+ New initiative" button creates `vision/initiatives/<slug>.md`.

### 3.4 Feedback page (`/p/:slug/feedback`)

Each item: rendered view + Edit button (textarea + Save) + **Promote to task** button (opens modal pre-filled with feedback title/body, on submit creates backlog task with `from: F-NNNN` and adds a backlinks line in the feedback file).

## 4. Components & files

### API
- `api/app/routes_backlog.py` — extend with POST/PATCH/DELETE/comments
- `api/app/routes_vision.py` — extend with PUT, POST (new initiative)
- `api/app/routes_feedback.py` — extend with PUT, promote
- `api/app/markdown_writer.py` — NEW: serialize task dict back to MD with frontmatter; idempotent
- `api/tests/test_routes_backlog.py` — new test cases for each endpoint
- `api/tests/test_routes_vision.py` — same
- `api/tests/test_routes_feedback.py` — same
- `api/tests/test_markdown_writer.py` — NEW

### Web
- `web/src/api.ts` — add the new endpoints
- `web/src/pages/Project.tsx` — add `+ New task` modal, card menu
- `web/src/pages/TaskDetail.tsx` — NEW
- `web/src/pages/Vision.tsx` — add edit mode per file
- `web/src/pages/Feedback.tsx` — add edit mode + promote modal
- `web/src/components/TaskCard.tsx` — extracted from inline render in Project.tsx; adds menu
- `web/src/components/Modal.tsx` — NEW (Bootstrap modal wrapper)
- `web/src/App.tsx` — register `/p/:slug/t/:id` route
- `web/src/utils/relativeTime.ts` — NEW (cheap formatter; no deps)

## 5. Risks

| Risk | Mitigation |
|---|---|
| Two browser tabs edit same task → last write wins | Acceptable for v1 single-stakeholder use; spec #6 may add etag/version |
| New task ID collision under concurrent creates | API takes a directory-level lock (`flock` on `<backlog>/.lock` file) for the duration of the new-id allocation + write |
| Markdown writer corrupts a file | Write to `<file>.tmp` then `os.rename` atomically; never leaves a half-written file |
| Vision file replaced with garbage | API validates non-empty UTF-8 ≤ 200 KB; rejects if violated. Stakeholder can lose content on mistype but file is gitignored — they restore from another agent's session via Edit |
| Comment-add appends to a malformed task | Markdown writer reads existing frontmatter+body, validates, appends; rejects on parse error |

## 6. Out of scope (deferred)

- Real-time multi-user sync (websockets)
- Edit history / versioning / undo
- Rich text editor / WYSIWYG
- File attachments
- Drag-and-drop status changes (dropdown only in v1)
- Per-task labels / tags / priority (frontmatter stays minimal)
- Markdown rendering of Russian text — already works via Bootstrap default fonts
- Search / filter / sort beyond the 4-status grouping

## 7. Implementation order

1. `markdown_writer.py` + tests — atomic write, frontmatter merge
2. Backlog endpoints (POST, PATCH, DELETE, comments) + tests
3. Vision endpoints (PUT, POST initiative) + tests
4. Feedback endpoints (PUT, promote) + tests
5. Frontend: API client extensions
6. Frontend: TaskCard + Modal components
7. Frontend: Project.tsx new-task modal + card menu
8. Frontend: TaskDetail.tsx page
9. Frontend: Vision.tsx edit mode
10. Frontend: Feedback.tsx edit mode + promote modal
11. Build + test in browser
12. Deploy via `ops/bot-squad-bin/deploy staging "spec #4 ..."` (eats own dogfood; the bot-squad UI doesn't deploy itself, but proves the deploy flow still works for signal-tracker's recipes)

Each step gets a task in the plan with explicit acceptance criteria.
