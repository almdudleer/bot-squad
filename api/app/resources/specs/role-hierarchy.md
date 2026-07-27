---
name: role-hierarchy
status: canonical
created: 2026-06-02
authority: stakeholder verbatim
---

# Role hierarchy — canonical spec

This is the canonical role / permission model for bot-squad. Stakeholder defined verbatim on 2026-06-02. Treat as authoritative — if any code or UI surface disagrees, file a ticket and fix the code, not the spec.

## Two-layer architecture

- **Mothership** (botsquad.dev): a thin connectivity / global-user / easy-installation layer above the actual bot-squad logic. Links server users together, makes them available through one UI, simplifies auth (so users don't keep N passwords for N servers).
- **Server** (bot-squad installation, runs on a specific host): the actual bot-squad logic and storage. Each server can be attached to a mothership, or detached and standalone.

When attached: notifications via mothership's TG bot (mothership delivers to/from TG). When detached: server binds its own TG bot (server gains a TG-bot-config field; locked while attached). Detaching loses global connectivity from your botsquad global account, loses availability via botsquad.dev, gives you a UI hosted from that server stripped of multi-server features.

## Roles (highest → lowest)

### 1. Global (mothership) admin
- **Who**: just the stakeholder for now.
- **Can**: review mothership state · list global users · invite global users · grant admin / send-invites permissions to other global users · see number/list of servers connected to the mothership.
- **Cannot**: access projects inside servers they don't own. The specific server's backend authorizes users via a secret from the user — even if the mothership were compromised, the attacker cannot pivot into all the servers.
- **List-servers / list-projects** may stay global (for the All Projects view), but **entering a project** on a server must be protected at the installation level.

### 2. Global (mothership) member ( = any bot-squad user, "global user")
- **Can**: create servers · view all their servers and their projects · use the system · set up personal `chat_id` (+ `topic_id`) for personal notifications.
- **Sync**: personal `chat_id` / `topic_id` is synced to their server-member presence on each server by default via an API (servers fetch from mothership). Can be overridden per-server and per-project later.

### 3. Server admin ( = creator of the server / bot-squad installation, by default)
- **Can**: add new servers · manage their bot-squad installation · detach the server from the mothership to get a standalone version · grant users access to this server · add projects.
- **From this role down**, auth and permissions are managed by the **server** rather than the mothership.

### 4. Server member
- **Can**: manage their own server worker · add projects (if server admin gave permission) · configure personal `chat_id` (+ `topic_id`) for notifications about their own agents.
- **Linkage**: in an attached installation, server users are linked to the global botsquad user. Admins are server members too in this sense.

### 5. Project admin ( = project creator by default)
- **Can**: manage project settings · add new project admins · bind `chat_id` (+ `topic_id`) for project-wide notifications: deployment queue, incoming notifications and problems, agents sending project-wide questions to all members.

### 6. Project member
- **Can**: work with the board, initiatives, agents · view all sessions, all tasks, all initiatives on this project (no visibility controls inside a project) · override their own `chat_id` (+ `topic_id`) per-project (defaults match their server-member's setting, with an override button on the field).
- **Working tree**: all project members are assumed to work on the same working tree.
- **OS-level isolation**: members have their own OS users; by default only they can attach to their tmux. This is enforced by OS permissions, not bot-squad.

## Notifications config inheritance

Personal chat_id / topic_id: global user → server member (sync, can override) → project member (sync from server member, can override).
Project-wide chat_id / topic_id: set by project admin, applies to deployment queue, problems, agent-to-all questions.

## UI implications

- The mothership-admin view (server list / global users / invites) is **distinct** from the "All Projects" view. All Projects is a user's section. The mothership view is admin-only.
- "Attachment" as a section name is undefined / misscoped — refactor sidebar groupings to match the role-hierarchy, not the historical accidents.
- TG-bot-config field on server is visible/editable only when detached (or grayed/locked when attached).

## Open audit items
See [[T-0169]] for codebase audit + fixes against this spec, [[T-0170]] for sidebar v3 to reflect it, [[T-0171]] for detached-install TG-bot field.
