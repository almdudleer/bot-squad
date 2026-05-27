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
          "owner_user": "<botsquad.dev username>",
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

T-0024 adds the install-token + connect lifecycle helpers + a per-server
checkpoint log under ``DATA_DIR/_mothership/checkpoints/<server_id>.jsonl``.
T-0023 will grow this with ``update_projects_cache``.

Detach build: delete this file alongside ``routes_mothership.py``.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.install_tokens import (
    hash_token,
    mint_install_token,
    mint_invite_token,
    mint_server_bearer,
)


SCHEMA_VERSION = 1
INSTALL_TOKEN_TTL_SECONDS = 24 * 3600
# Invite tokens (T-0026) share the install-token shape but are per-user, not
# per-server. Same 24h TTL — same blast radius if the link leaks; the user
# who needs to use it is presumed reachable within that window. Single-use
# (burned on /installer/join) regardless.
INVITE_TOKEN_TTL_SECONDS = 24 * 3600


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_utc(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


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
    # T-0055: marks the mothership's own entry in its own registry, so the
    # unified all-projects view can render it with a "this server" affordance
    # distinct from attached peers. Defaults to False so existing registry
    # rows on disk (no key) deserialise unchanged via the ``**s`` splat in
    # ``list_servers``; we tolerate the missing key in ``_read`` below.
    is_self: bool = False
    # T-0088: latest release-telemetry snapshot reported by the consumer's
    # autoupdate poller. None until the first ``POST /api/releases/_telemetry``
    # lands for this server. Shape (free-form dict, validated at the route
    # layer): ``{installed_version, last_check_at, last_apply_at,
    # last_apply_outcome, current_git_sha}``. We store the dict verbatim so
    # the GET handler can fan it back out as-is without a second migration
    # if the consumer ever adds a field.
    release: dict | None = None
    # T-0026: outstanding invite tokens for additional Linux users to join
    # this server. Each invite is single-use (burned on /installer/join) and
    # TTL'd. Plaintexts NEVER land here — only SHA-256 hashes.
    # Shape: list of dicts {hash, expires_at, target_username, role,
    # created_by, created_at}; role ∈ {"admin","non-admin"}.
    invites: list[dict] = field(default_factory=list)

    def to_public(self) -> dict:
        d = asdict(self)
        d.pop("install_token_hash", None)
        d.pop("server_bearer_hash", None)
        # Invite hashes are credentials too — strip them from the public
        # projection. We surface only the non-secret fields so the wizard
        # can render a "this server's outstanding invites" list.
        d["invites"] = [
            {k: v for k, v in inv.items() if k != "hash"}
            for inv in d.get("invites", [])
        ]
        return d


class MothershipStore:
    """File-backed registry with a process-wide mutation lock.

    Reads are lockless — the JSON file is rewritten atomically via rename
    so a concurrent reader either sees the old snapshot or the new one,
    never a partial write. Mutating helpers (``register_server``,
    ``consume_install_token``, ``set_server_bearer``) acquire a class-level
    threading lock so multiple HTTP requests in the same process can't
    interleave their read-modify-write windows.
    """

    _lock = threading.Lock()

    def __init__(self, root: Path) -> None:
        self.root = root
        self.servers_path = root / "servers.json"

    # ---- raw read / write ----------------------------------------------------

    def _read(self) -> dict:
        if not self.servers_path.exists():
            return {"version": SCHEMA_VERSION, "servers": []}
        with self.servers_path.open(encoding="utf-8") as f:
            return json.load(f)

    def list_servers(self) -> list[AttachedServer]:
        # Use a known-field allowlist so a future schema field added on disk
        # by a newer process doesn't 500 this reader on rollback. Unknown
        # keys are dropped; missing keys (e.g. ``is_self`` on pre-T-0055 rows)
        # take the dataclass default.
        known = {f.name for f in fields(AttachedServer)}
        return [
            AttachedServer(**{k: v for k, v in s.items() if k in known})
            for s in self._read().get("servers", [])
        ]

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

    # ---- lookups -------------------------------------------------------------

    def get_server(self, server_id: str) -> AttachedServer | None:
        for s in self.list_servers():
            if s.id == server_id:
                return s
        return None

    def server_by_install_token(self, token: str) -> AttachedServer | None:
        """Hash + TTL check on the install_token, no burn.

        Returns ``None`` for unknown OR expired tokens — callers should
        not distinguish the two over the wire (always 410 Gone) so the
        installer's structured "what + how" message is identical in
        both cases.
        """
        h = hash_token(token)
        for s in self.list_servers():
            if s.install_token_hash != h:
                continue
            if not s.install_token_expires_at:
                return None
            if datetime.now(timezone.utc) > _parse_utc(s.install_token_expires_at):
                return None
            return s
        return None

    def server_by_bearer(self, bearer: str) -> AttachedServer | None:
        h = hash_token(bearer)
        for s in self.list_servers():
            if s.server_bearer_hash == h:
                return s
        return None

    # ---- mutations -----------------------------------------------------------

    def register_server(
        self,
        *,
        display_name: str,
        base_url: str,
        owner_user: str,
        ttl_seconds: int = INSTALL_TOKEN_TTL_SECONDS,
    ) -> tuple[AttachedServer, str]:
        """Create a pending server entry, mint a fresh install_token.

        Returns ``(entry, plaintext_install_token)``. The plaintext is the
        ONLY copy that ever exits the API process — it's embedded in the
        install URL we hand back to the UI, and the registry only ever
        stores its SHA-256.
        """
        token = mint_install_token()
        token_hash = hash_token(token)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        entry = AttachedServer(
            id=f"srv_{secrets.token_hex(12)}",
            display_name=display_name,
            base_url=base_url,
            owner_user=owner_user,
            created_at=_utc_now_iso(),
            install_state="pending",
            install_token_hash=token_hash,
            install_token_expires_at=expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
        )
        with self._lock:
            servers = self.list_servers()
            servers.append(entry)
            self.write(servers)
        return entry, token

    def register_self_if_missing(
        self,
        *,
        base_url: str,
        display_name: str,
        owner_user: str = "system",
    ) -> AttachedServer | None:
        """Self-register the mothership server in its own registry (T-0055).

        Idempotent: dedup by ``base_url`` (after rstrip("/")). If an entry
        already exists for this URL, no-op and return the existing entry
        with the ``is_self`` flag re-asserted (so a manually-added row gets
        promoted to "this server" on next boot rather than living as a
        zombie peer that proxies to itself).

        The self entry skips the install_token lifecycle entirely:
        ``install_state`` lands as ``ready``, ``install_token_hash`` /
        ``server_bearer_hash`` stay None. The cross-server FE proxy at
        ``/api/m/servers/{id}/api/*`` will refuse to forward without a
        bearer; that's deliberate — T-0049 will wire local projects in
        without the proxy hop.
        """
        canonical = base_url.rstrip("/")
        if not canonical:
            return None
        with self._lock:
            servers = self.list_servers()
            # Pass 1: at-most-one is_self invariant. Domain renames change
            # MOTHERSHIP_BASE_URL between deploys; the old register code
            # deduped only by base_url, so a rename created a SECOND
            # is_self row instead of migrating. Find any existing self
            # entry first and migrate it to the new URL.
            self_idx = next(
                (i for i, s in enumerate(servers) if s.is_self), None
            )
            if self_idx is not None:
                existing = servers[self_idx]
                if (
                    existing.base_url.rstrip("/") == canonical
                    and existing.display_name == display_name
                ):
                    return existing
                servers[self_idx] = replace(
                    existing,
                    base_url=canonical,
                    display_name=display_name,
                )
                self.write(servers)
                return servers[self_idx]
            # Pass 2: no self entry yet. If a row with this base_url
            # already exists (e.g. manually POSTed), promote it.
            for i, s in enumerate(servers):
                if s.base_url.rstrip("/") == canonical:
                    servers[i] = replace(s, is_self=True)
                    self.write(servers)
                    return servers[i]
            # Pass 3: create fresh.
            entry = AttachedServer(
                id=f"srv_{secrets.token_hex(12)}",
                display_name=display_name,
                base_url=canonical,
                owner_user=owner_user,
                created_at=_utc_now_iso(),
                install_state="ready",
                is_self=True,
            )
            servers.append(entry)
            self.write(servers)
            return entry

    def consume_install_token(
        self,
        token: str,
        server_meta: dict | None = None,  # noqa: ARG002 — reserved for later
    ) -> tuple[AttachedServer, str] | None:
        """Validate + burn the install_token, mint a server_bearer.

        Returns ``(updated_entry, plaintext_server_bearer)`` on success or
        ``None`` if the token is unknown, expired, or already burned
        (all map to 410 Gone at the HTTP layer — see the bearer rotation
        section in ``mothership-seam.md``).
        """
        h = hash_token(token)
        bearer = mint_server_bearer()
        bearer_hash = hash_token(bearer)
        now = datetime.now(timezone.utc)
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.install_token_hash != h:
                    continue
                if not s.install_token_expires_at:
                    return None
                if now > _parse_utc(s.install_token_expires_at):
                    return None
                updated = replace(
                    s,
                    install_state="connected",
                    install_token_hash=None,
                    install_token_expires_at=None,
                    server_bearer_hash=bearer_hash,
                    last_seen_at=_utc_now_iso(),
                )
                servers[i] = updated
                self.write(servers)
                # T-0023 proxy contract: persist the plaintext to the
                # bearer sidecar inside the same lock so the registry row
                # and the on-disk plaintext never disagree. If the sidecar
                # write raises, the install_token is already burned —
                # documented as a v1 limitation in mothership-seam.md.
                self.store_server_bearer(updated.id, bearer)
                return updated, bearer
        return None

    # ---- invites (T-0026) ----------------------------------------------------
    # Invites attach a TTL'd, single-use token to a (server, target_username,
    # role) triple. The installer's invite-mode branch trades the plaintext
    # at /installer/join for the target_username + role it needs to drive the
    # user-scoped install. Stored under the server's ``invites`` list (not as
    # a parallel registry) so a server delete sweeps its invites with it.

    def register_invite(
        self,
        *,
        server_id: str,
        target_username: str,
        role: str,
        created_by: str,
        ttl_seconds: int = INVITE_TOKEN_TTL_SECONDS,
    ) -> tuple[AttachedServer, str] | None:
        """Mint a fresh invite token tied to ``server_id``.

        Returns ``(updated_entry, plaintext_invite_token)`` on success or
        ``None`` if no such server. The plaintext is the ONLY copy that
        ever exits the API process — only its SHA-256 lands on disk.
        """
        if role not in ("admin", "non-admin"):
            raise ValueError(f"invite role must be 'admin' or 'non-admin', got {role!r}")
        token = mint_invite_token()
        token_hash = hash_token(token)
        now = datetime.now(timezone.utc)
        expires = now + timedelta(seconds=ttl_seconds)
        invite = {
            "hash": token_hash,
            "expires_at": expires.isoformat(timespec="seconds").replace("+00:00", "Z"),
            "target_username": target_username,
            "role": role,
            "created_by": created_by,
            "created_at": _utc_now_iso(),
        }
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.id == server_id:
                    servers[i] = replace(s, invites=[*s.invites, invite])
                    self.write(servers)
                    return servers[i], token
        return None

    def server_by_invite_token(
        self, token: str
    ) -> tuple[AttachedServer, dict] | None:
        """Hash + TTL check on an invite token, no burn.

        Returns ``(server, invite_dict)`` for an unburned, unexpired invite or
        ``None`` for unknown/expired/burned tokens (uniform 410 at the route
        layer — keeps the installer's "what + how" recipe identical to the
        install-token 410 case).
        """
        h = hash_token(token)
        now = datetime.now(timezone.utc)
        for s in self.list_servers():
            for inv in s.invites:
                if inv.get("hash") != h:
                    continue
                expires_at = inv.get("expires_at")
                if not expires_at:
                    return None
                if now > _parse_utc(expires_at):
                    return None
                return s, inv
        return None

    def consume_invite_token(self, token: str) -> tuple[AttachedServer, dict] | None:
        """Validate + burn an invite token.

        Returns ``(server, burned_invite_dict)`` on success or ``None`` if the
        token is unknown, expired, or already burned (all map to 410 Gone at
        the HTTP layer, matching the install-token contract).
        """
        h = hash_token(token)
        now = datetime.now(timezone.utc)
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                for j, inv in enumerate(s.invites):
                    if inv.get("hash") != h:
                        continue
                    expires_at = inv.get("expires_at")
                    if not expires_at:
                        return None
                    if now > _parse_utc(expires_at):
                        return None
                    burned = {**inv, "burned_at": _utc_now_iso()}
                    new_invites = [*s.invites[:j], *s.invites[j + 1 :]]
                    servers[i] = replace(s, invites=new_invites)
                    self.write(servers)
                    return servers[i], burned
        return None

    def set_server_bearer(self, server_id: str, bearer_hash: str | None) -> None:
        """Replace the server_bearer_hash (or clear it on revoke)."""
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.id == server_id:
                    servers[i] = replace(s, server_bearer_hash=bearer_hash)
                    self.write(servers)
                    return

    # ---- bearer sidecar (T-0023 contract) -----------------------------------
    # The registry stores only the SHA-256 of the server_bearer. The T-0023
    # per-server proxy needs the plaintext to forward as a bearer to the
    # target server. We keep it in a sidecar file mode 0600 next to the
    # registry — never in the JSON itself, so a registry leak via logging
    # or an audit dump still doesn't disclose the active bearers.

    def _bearers_dir(self) -> Path:
        return self.root / "bearers"

    def _bearer_path(self, server_id: str) -> Path:
        return self._bearers_dir() / server_id

    def store_server_bearer(self, server_id: str, bearer: str) -> None:
        d = self._bearers_dir()
        d.mkdir(parents=True, exist_ok=True)
        path = self._bearer_path(server_id)
        tmp = path.with_suffix(".tmp")
        # Owner-only mode 0600. We chmod the tempfile before rename so the
        # final file inherits the restrictive mode on inode reuse.
        with os.fdopen(
            os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
            encoding="utf-8",
        ) as f:
            f.write(bearer)
        os.chmod(tmp, 0o600)
        os.rename(tmp, path)

    def read_server_bearer(self, server_id: str) -> str | None:
        path = self._bearer_path(server_id)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def forget_server_bearer(self, server_id: str) -> None:
        path = self._bearer_path(server_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    # ---- projects cache (T-0023) --------------------------------------------

    def update_projects_cache(
        self, server_id: str, projects: list[dict]
    ) -> list[dict] | None:
        """Replace the cached project list for ``server_id``.

        ``projects`` is the raw upstream response (each item has ``slug`` +
        ``display_name``; status is not yet on the single-install API — see
        T-0016). We normalise to the schema fixed in ``mothership-seam.md``
        and default ``status`` to ``"idle"`` until the status-sync stream
        lands.

        Returns the persisted list, or ``None`` if no such server.
        """
        normalised = [
            {
                "slug": p.get("slug", ""),
                "display_name": p.get("display_name", p.get("slug", "")),
                "status": p.get("status", "idle"),
            }
            for p in projects
            if p.get("slug")
        ]
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.id == server_id:
                    servers[i] = replace(s, projects_cache=normalised)
                    self.write(servers)
                    return normalised
        return None

    # ---- release telemetry (T-0088) -----------------------------------------

    # Allowlist mirrors the spec'd POST body (minus ``install_id``, which is
    # the key, not part of the payload). Anything else the consumer ships is
    # dropped here so we never leak unexpected fields back through the GET.
    _RELEASE_FIELDS = (
        "installed_version",
        "last_check_at",
        "last_apply_at",
        "last_apply_outcome",
        "current_git_sha",
    )

    def set_release_telemetry(
        self, server_id: str, telemetry: dict
    ) -> AttachedServer | None:
        """Replace the ``release`` snapshot for ``server_id``.

        Persists only the allowlisted keys; values are stored verbatim (the
        route layer is responsible for sane type-checking of the payload).
        Returns the updated entry, or ``None`` if no such server.
        """
        normalised = {k: telemetry.get(k) for k in self._RELEASE_FIELDS}
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.id == server_id:
                    servers[i] = replace(s, release=normalised)
                    self.write(servers)
                    return servers[i]
        return None

    def touch_last_seen(self, server_id: str) -> None:
        """Stamp ``last_seen_at`` on the registry entry."""
        now = _utc_now_iso()
        with self._lock:
            servers = self.list_servers()
            for i, s in enumerate(servers):
                if s.id == server_id:
                    servers[i] = replace(s, last_seen_at=now)
                    self.write(servers)
                    return

    # ---- checkpoint log ------------------------------------------------------

    def _checkpoints_dir(self) -> Path:
        return self.root / "checkpoints"

    def append_checkpoint(self, server_id: str, event: dict) -> dict:
        """Append a checkpoint event to the per-server JSONL log.

        The event is stamped with a server-side ``received_at`` so the SSE
        consumer has a trustworthy ordering signal — installer ``ts`` is
        advisory because the installer's clock can drift.
        """
        stamped = {**event, "received_at": _utc_now_iso()}
        log_dir = self._checkpoints_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{server_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(stamped) + "\n")
        return stamped

    def read_checkpoints(self, server_id: str) -> list[dict]:
        path = self._checkpoints_dir() / f"{server_id}.jsonl"
        if not path.exists():
            return []
        out: list[dict] = []
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    # A partial line from a torn write would be the worst
                    # case here, and we'd rather show the user the events
                    # we can parse than 500 the SSE handshake.
                    continue
        return out
