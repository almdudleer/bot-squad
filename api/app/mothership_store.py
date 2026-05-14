"""Persistence for the mothership centralization layer.

Stores the attached-server registry at ``DATA_DIR/_mothership/servers.json``.
Single-writer in the API process; atomic via rename. Token plaintexts never
land here — only SHA-256 hashes.

Schema (frozen by ``vision/architecture/mothership-seam.md``)::

    {
      "version": 1,
      "servers": [
        {
          "id": "srv_<ulid>",
          "display_name": "...",
          "base_url": "https://...",
          "owner_user": "<bot-squad.org username>",
          "created_at": "<ISO-8601 UTC>",
          "install_state": "pending|connected|ready|failed",
          "install_token_hash": "<sha256 hex, null after /connect>",
          "install_token_expires_at": "<ISO-8601 UTC, null after /connect>",
          "server_bearer_hash": "<sha256 hex, set on /connect>",
          "last_seen_at": "<ISO-8601 UTC, null until first ping>",
          "projects_cache": [
            {"slug": "...", "display_name": "...", "status": "working|needs-input|idle"}
          ]
        }
      ]
    }

This module ships the read path + write helper only. T-0024 grows it with
``register_server`` / ``consume_install_token`` / ``set_server_bearer``;
T-0023 grows it with ``update_projects_cache``. Detach build: delete this
file alongside ``routes_mothership.py``.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class AttachedServer:
    id: str
    display_name: str
    base_url: str
    owner_user: str
    created_at: str
    install_state: str
    install_token_hash: str | None = None
    install_token_expires_at: str | None = None
    server_bearer_hash: str | None = None
    last_seen_at: str | None = None
    projects_cache: list[dict] = field(default_factory=list)

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("install_token_hash", None)
        d.pop("server_bearer_hash", None)
        return d


class MothershipStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.servers_path = root / "servers.json"

    def _read(self) -> dict:
        if not self.servers_path.exists():
            return {"version": SCHEMA_VERSION, "servers": []}
        with self.servers_path.open(encoding="utf-8") as f:
            return json.load(f)

    def list_servers(self) -> list[AttachedServer]:
        return [AttachedServer(**s) for s in self._read().get("servers", [])]

    def write(self, servers: list[AttachedServer]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.servers_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(
                {"version": SCHEMA_VERSION, "servers": [asdict(s) for s in servers]},
                indent=2,
            ),
            encoding="utf-8",
        )
        os.rename(tmp, self.servers_path)
