# `api/app/resources/specs/` — framework specs that are NOT role contracts

Git SSOT for framework-level canonical markdown that shipped code cites but
that **no session is ever briefed from**. Sibling of `../roles/`, deliberately
separate from it:

| dir | what lives there | who reads it |
|-----|------------------|--------------|
| `../roles/` | **spawnable role contracts** — `dev.md`, `operator.md`, … | `bsq _assemble_prompt` / `bsq brief` / the SessionStart pointer, keyed by role name (D-0043) |
| `specs/` | framework specs cited as canonical by code + skills | humans and the code that implements them; **never** the spawn assembler |

Keeping them apart matters: `roles/*.md` is *enumerated* (the pointer lint globs
it to learn the role list, and `project_scaffold._seed_vision_roles` seeds it
into every project's Vision tab). A non-contract dropped in there would be read
as a role.

Current contents (both moved here by T-0730 from
`data/<slug>/vision/roles/`, where they had no git counterpart at all):

- **`role-hierarchy.md`** — the six fixed user/permission roles
  (global/server/project × admin/member). Stakeholder-verbatim, 2026-06-02.
  Cited as the canonical spec by `api/app/roles.py` and `web/src/components/
  Shell.tsx` (the sidebar IA is built on it). Treat as authoritative: if code
  or UI disagrees, file a ticket and fix the code, not the spec.
- **`session-lifecycle-contract.md`** — the operator → TL → dev chain and the
  lifecycle it runs on. Cited as the full contract by the `bot-squad-lifecycle`
  framework skill and `scripts/templates/teamlead.settings.local.json`.

**Stable alias for prose:** `$BOT_SQUAD/api/app/resources/specs/<name>.md`
(D-0043 addendum 1 — sessions of any project read these, so a bare
repo-relative path only resolves inside a bot-squad clone). Inside a clone the
repo-relative path is the same directory and is what code comments use.

`scripts/lint/role_doc_pointers.py` fails any `vision/roles/<name>.md` pointer
for a stem covered here, so the old live-tree path can never come back.
