"""HTTP/JSON client for the worker, speaking over a Unix socket."""
from __future__ import annotations

from pathlib import Path

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

    async def call_action(self, name: str, params: dict) -> dict:
        try:
            async with self._client() as c:
                r = await c.post(f"/actions/{name}", json=params or {})
                if r.status_code != 200:
                    detail = r.json().get("detail", r.text) if r.content else r.text
                    raise WorkerError(f"worker rejected {name}: {detail}")
                return r.json()
        except httpx.RequestError as e:
            raise WorkerError(f"worker request error for {name}: {e}") from e
