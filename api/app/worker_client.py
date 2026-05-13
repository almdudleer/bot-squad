"""HTTP/JSON client for the worker, speaking over a Unix socket.

Phase 2 multi-user: WorkerRouter maps requests to the right worker socket.
Coordinator (single host) handles non-tmux actions; per-user workers
(one per Linux user) handle tmux ops in their own user context.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import httpx


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
        if linux_user == self.coordinator_user:
            return WorkerClient(self._coordinator_sock)
        return WorkerClient(_user_sock(self._sock_dir, linux_user))

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
        """Return (linux_user, client) for every known user (coordinator + meta)."""
        return [(u, self.for_user(u)) for u in self.users]
