"""HTTP/JSON client for the worker, speaking over a Unix socket.

Phase 2 multi-user: WorkerRouter maps requests to the right worker socket.
Coordinator (single host) handles non-tmux actions; per-user workers
(one per Linux user) handle tmux ops in their own user context.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import httpx

log = logging.getLogger(__name__)


class WorkerError(Exception):
    """Raised on transport or protocol errors talking to the worker."""


class WorkerClient:
    def __init__(self, sock_path: Path) -> None:
        self.sock_path = Path(sock_path)

    def _client(self) -> httpx.AsyncClient:
        # The "http://w" host is arbitrary — httpx requires a host header but
        # the connection itself goes through the UDS transport.
        transport = httpx.AsyncHTTPTransport(uds=str(self.sock_path))
        return httpx.AsyncClient(transport=transport, base_url="http://w", timeout=5.0)

    async def health(self) -> dict:
        try:
            async with self._client() as c:
                r = await c.get("/health")
                r.raise_for_status()
                return r.json()
        except (httpx.RequestError, httpx.HTTPStatusError) as e:
            raise WorkerError(f"worker /health failed: {e}") from e

    async def call_action(self, name: str, params: dict, timeout: float | None = None) -> dict:
        try:
            transport = httpx.AsyncHTTPTransport(uds=str(self.sock_path))
            t = timeout if timeout is not None else 5.0
            async with httpx.AsyncClient(transport=transport, base_url="http://w", timeout=t) as c:
                r = await c.post(f"/actions/{name}", json=params or {})
                if r.status_code != 200:
                    detail = r.json().get("detail", r.text) if r.content else r.text
                    raise WorkerError(f"worker rejected {name}: {detail}")
                return r.json()
        except httpx.RequestError as e:
            raise WorkerError(f"worker request error for {name}: {e}") from e


def _user_sock(sock_dir: Path, linux_user: str) -> Path:
    return sock_dir / f"user-{linux_user}.sock"


class WorkerRouter:
    """Routes requests to either the coordinator socket or a user worker socket.

    - coordinator: `worker.sock` — runs scheduler + all coordinator-tagged
      actions. Also serves tmux ops for the coordinator's own linux user.
    - user-<linux_user>: `user-<linux_user>.sock` — tmux ops only for that
      user. Created by `systemd --user` on the user's account.

    The coordinator linux user is configurable; for users equal to the
    coordinator, `for_user()` returns the coordinator client (one process
    serves both roles).
    """

    def __init__(
        self,
        coordinator_sock: Path,
        coordinator_user: str,
        known_users: Iterable[str] = (),
    ) -> None:
        self._coordinator_sock = Path(coordinator_sock)
        self._sock_dir = self._coordinator_sock.parent
        self.coordinator_user = coordinator_user
        # All linux users we know about (from auth.toml user_meta).
        # Coordinator user is always included; deduped.
        self.users: list[str] = list({coordinator_user, *known_users})

    def coordinator(self) -> WorkerClient:
        return WorkerClient(self._coordinator_sock)

    def for_user(self, linux_user: str) -> WorkerClient:
        """Return the client for a user worker; fall back to coordinator.

        T-0080: when a non-coordinator user has no per-user worker socket
        installed, fall back to the coordinator socket so spawn / pause /
        resume actions still succeed. Physical tmux ops happen on the
        coordinator's linux user; the SessionMd owner field (not the SID
        prefix) is the source of truth for which UI user owns the
        session. Real per-user-tmux isolation remains deferred.
        """
        if linux_user == self.coordinator_user:
            return WorkerClient(self._coordinator_sock)
        user_sock = _user_sock(self._sock_dir, linux_user)
        if not user_sock.exists():
            log.warning(
                "WorkerRouter.for_user(%r): %s missing, falling back to coordinator",
                linux_user, user_sock,
            )
            return WorkerClient(self._coordinator_sock)
        return WorkerClient(user_sock)

    def for_user_strict(self, linux_user: str) -> WorkerClient:
        """Route to ``linux_user`` without cross-user coordinator fallback.

        Use this for identity-bound automatic spawns.  Falling back is unsafe
        there: it changes the Linux account, tmux server, credentials and agent
        provider while retaining the requested user's conversation identity.
        """
        if linux_user == self.coordinator_user:
            return WorkerClient(self._coordinator_sock)
        user_sock = _user_sock(self._sock_dir, linux_user)
        if not user_sock.exists():
            raise WorkerError(
                f"user worker unavailable for {linux_user}: {user_sock}"
            )
        return WorkerClient(user_sock)

    def for_sid(self, sid: str) -> WorkerClient:
        """Resolve via SID format S-<linux_user>-<rest>. Falls back to coordinator."""
        if sid.startswith("S-"):
            parts = sid.split("-", 2)
            if len(parts) >= 2 and parts[1]:
                return self.for_user(parts[1])
        return self.coordinator()

    def user_for_sid(self, sid: str) -> str | None:
        if sid.startswith("S-"):
            parts = sid.split("-", 2)
            if len(parts) >= 2 and parts[1]:
                return parts[1]
        return None

    def all_user_workers(self) -> list[tuple[str, WorkerClient]]:
        """Return (linux_user, client) for every known user with a reachable
        worker (coordinator, always; per-user meta only if its socket exists).

        Used by list_sessions fan-out. Unlike for_user(), this does NOT
        fall back to coordinator for a per-user socket that exists but errors
        — we want each declared user to be queried at their expected socket
        so a genuinely broken worker fails fast and surfaces, rather than
        re-hitting coordinator N times and double-counting sessions.

        T-0668: a user_meta entry whose per-user worker was never provisioned
        (no socket file ever created — e.g. a staged multi-user rollout not
        yet flipped on for that account) is excluded here instead of being
        queried anyway. That produced a GUARANTEED ENOENT on every single
        fan-out call forever (live repro: aqice/timpo, ~96% of fan-out
        failures in a 24h prod log sample) — not a transient reachability
        blip but a permanent, expected non-provisioning that isn't worth
        alerting on every page load. It loses no data: with no worker
        process for that account, its tmux panes were never visible to
        fan-out anyway.
        """
        out: list[tuple[str, WorkerClient]] = []
        for u in self.users:
            if u == self.coordinator_user:
                out.append((u, WorkerClient(self._coordinator_sock)))
                continue
            sock = _user_sock(self._sock_dir, u)
            if sock.exists():
                out.append((u, WorkerClient(sock)))
        return out
