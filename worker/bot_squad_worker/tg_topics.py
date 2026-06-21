"""Per-project Telegram forum-topic routing (T-0386 / INI-04 Phase 1).

Each project's chat (``project.tg_chat``) is a forum-enabled supergroup. Inside
it the bot owns a small set of topics — one per message *class* — so deploy
logs, needs-input pings and feedback land in separate threads instead of a
single jumbled DM stream.

The thread-ids are created at runtime via the Bot API (``createForumTopic``) and
persisted here, in the worker's own state dir, NOT in projects.toml — the API
owns projects.toml (static config: the supergroup id), the worker owns the
dynamic, runtime-allocated topic map. Keeps a single writer per file.

``resolve()`` maps a message class → message_thread_id, falling back to the
project's legacy single ``tg_topic_id`` (T-0156) and then to ``None`` (the
group's general feed), so an unprovisioned project behaves exactly as before.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# Message class → human-facing topic title. The canonical set the bot
# provisions on project-create and GCs on archive. Add a class here to add a
# routed topic.
STANDARD_TOPICS: dict[str, str] = {
    "feedback": "💬 feedback",
    "deploy_logs": "🚚 deploy-logs",
    "team_queries": "🙋 team-queries",
}


def topics_path(cfg: Any, slug: str) -> Path:
    """Where the per-project class→thread-id map is persisted."""
    return Path(cfg.data_dir) / slug / "_worker" / "tg_topics.json"


def load(cfg: Any, slug: str) -> dict[str, int]:
    """Return the persisted class→thread-id map, or {} when none exists."""
    p = topics_path(cfg, slug)
    if not p.exists():
        return {}
    try:
        raw = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        log.warning("tg_topics: unreadable map at %s — treating as empty", p)
        return {}
    return {str(k): int(v) for k, v in raw.items() if v is not None}


def save(cfg: Any, slug: str, mapping: dict[str, int]) -> None:
    """Atomically persist the class→thread-id map."""
    p = topics_path(cfg, slug)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({k: int(v) for k, v in mapping.items()}, indent=2))
    os.replace(tmp, p)


def resolve(cfg: Any, slug: str, msg_class: str) -> int | None:
    """Map ``msg_class`` → the forum thread-id to deliver it to.

    Resolution order: the per-class map → the project's legacy ``tg_topic_id``
    → ``None`` (general feed). Unknown slug → None.
    """
    mapping = load(cfg, slug)
    if msg_class in mapping:
        return mapping[msg_class]
    project = (getattr(cfg, "projects", {}) or {}).get(slug)
    if project is not None:
        return getattr(project, "tg_topic_id", None)
    return None
