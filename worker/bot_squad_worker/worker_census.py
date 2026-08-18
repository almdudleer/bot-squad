"""Which workers serve this install, and what code each one is holding (T-0880).

``restart_worker`` restarts the systemd unit ``bot-squad-worker`` — the
COORDINATOR. Per-user workers (``data/_sock/user-<name>.sock``) are separate
long-lived processes running FROM THE SAME INSTALL TREE, and nothing restarts
them, so they keep executing the files a deploy replaced underneath them for as
long as they live. Measured 2026-08-11 and again 2026-08-12: a per-user worker
four days old on an install that had just converged by every signal the system
offered, on the path that mattered — the user-conversation spawn runs in the
CALLEE, so a merged, deployed, correct fix was unreachable because the process
that had to run it was days old.

No existing signal can see that: ``/api/health`` reports one ``worker.git_sha``
and it is the coordinator's, so the convergence check that gates every deploy
report is blind to per-user workers by construction.

Why ASK each worker rather than have each publish a file: the quantity that
matters is what a process holds IN MEMORY. Python binds code at import, so a
file describes what a worker meant to load, while its own socket answer comes
from the live process. A worker that has died stops answering, which is the
correct reading of "not serving this install".

Four states, kept distinct on purpose (T-0880 exists because a two-state
"converged / not" hid a whole process class):

``converged``    the worker is running the deployed sha
``stale``        it answered with a DIFFERENT sha — the T-0880 condition
``unknown``      it is alive but cannot say what it is running
``unreachable``  no worker answered on that socket

``unknown`` is not a polite ``stale``. Until T-0880's ``deploy._git_argv`` fix,
every per-user worker answered ``""`` for all three sha fields, because git
refuses a tree owned by another linux user ("detected dubious ownership") and
the probe maps that to "". A caller that treats "" as a mismatch cries wolf
forever; one that treats it as a match reports a stale worker as converged.
Neither is true, so it gets its own name.
"""
from __future__ import annotations

import logging
import pwd
from pathlib import Path

import httpx

log = logging.getLogger("bot-squad-worker")

CONVERGED = "converged"
STALE = "stale"
UNKNOWN = "unknown"
UNREACHABLE = "unreachable"

#: Short. A census must not be able to stall the deploy report it annotates —
#: an unresponsive worker is itself an answer (``unreachable``).
PROBE_TIMEOUT_SEC = 3.0


def _sock_owner(sock_path: Path) -> str:
    """The linux user that created the socket, or "" if it can't be read."""
    try:
        return pwd.getpwuid(sock_path.stat().st_uid).pw_name
    except (OSError, KeyError):
        return ""


def worker_sockets(data_dir: Path) -> list[dict]:
    """Every worker socket serving this install: the coordinator's, then each
    per-user socket, sorted by user so the report is stable between runs."""
    sock_dir = Path(data_dir) / "_sock"
    found: list[dict] = []
    coordinator = sock_dir / "worker.sock"
    if coordinator.exists():
        found.append({
            "kind": "coordinator",
            "linux_user": _sock_owner(coordinator),
            "sock": str(coordinator),
        })
    for sock in sorted(sock_dir.glob("user-*.sock")):
        found.append({
            "kind": "user",
            # The filename is the contract (__main__._user_sock_path builds it
            # from getpass.getuser()); the socket's owner is the corroborating
            # measurement, used only when the name cannot be parsed.
            "linux_user": sock.name[len("user-"):-len(".sock")] or _sock_owner(sock),
            "sock": str(sock),
        })
    return found


def probe_worker(sock_path: str | Path, timeout: float = PROBE_TIMEOUT_SEC) -> dict:
    """GET /health on one worker socket. Never raises — a worker that cannot be
    reached is a RESULT, not an error, and is exactly what this ticket is
    about."""
    transport = httpx.HTTPTransport(uds=str(sock_path))
    try:
        with httpx.Client(transport=transport, base_url="http://w", timeout=timeout) as c:
            r = c.get("/health")
        if r.status_code != 200:
            return {"alive": False, "error": f"HTTP {r.status_code}"}
        body = r.json()
        if not isinstance(body, dict):
            return {"alive": False, "error": "non-object /health body"}
    except Exception as e:  # noqa: BLE001 — every failure is "not serving"
        return {"alive": False, "error": f"{type(e).__name__}: {e}"}
    return {
        "alive": True,
        "git_sha": str(body.get("git_sha") or ""),
        "boot_git_sha": str(body.get("boot_git_sha") or ""),
        "install_git_sha": str(body.get("install_git_sha") or ""),
        "uptime": body.get("uptime"),
    }


def classify(row: dict, deployed_sha: str) -> tuple[str, str]:
    """(state, human detail) for one probed worker against the deployed sha."""
    if not row.get("alive"):
        return UNREACHABLE, row.get("error", "no answer on socket")
    running = row.get("git_sha") or row.get("boot_git_sha") or ""
    if not running:
        return UNKNOWN, (
            "worker is alive but reports no git sha — it cannot say what code "
            "it is running"
        )
    if not deployed_sha:
        return UNKNOWN, "the deployed sha is unknown, so nothing can be compared"
    if running == deployed_sha:
        return CONVERGED, ""
    return STALE, f"running {running[:12]}, deployed {deployed_sha[:12]}"


def census(data_dir: Path, deployed_sha: str = "", timeout: float = PROBE_TIMEOUT_SEC) -> list[dict]:
    """Every worker serving this install, each with its own sha and verdict."""
    rows = []
    for entry in worker_sockets(data_dir):
        probed = probe_worker(entry["sock"], timeout=timeout)
        row = {**entry, **probed}
        # An explicit label, because "linux_user or kind" is ambiguous: the
        # coordinator's socket is owned by a real account too, so a coordinator
        # run by `flomaster` would print identically to flomaster's per-user
        # worker — the one distinction this whole report exists to make.
        row["label"] = f"{row['kind']}:{row['linux_user']}" if row["linux_user"] else row["kind"]
        row["state"], row["detail"] = classify(row, deployed_sha)
        rows.append(row)
    return rows


def summarize(rows: list[dict]) -> dict:
    """Counts plus a one-line verdict for the deploy report.

    The line NAMES the workers that did not converge. "release deployed" must
    not be able to mean "one of N workers reloaded", so the summary refuses to
    collapse to a bare boolean: ``all_converged`` is only true when every worker
    answered AND matched.
    """
    counts = {CONVERGED: 0, STALE: 0, UNKNOWN: 0, UNREACHABLE: 0}
    for r in rows:
        counts[r["state"]] = counts.get(r["state"], 0) + 1
    not_converged = [r for r in rows if r["state"] != CONVERGED]
    if not rows:
        line = "no worker sockets found — nothing could be measured"
    elif not not_converged:
        line = f"all {len(rows)} worker(s) converged"
    else:
        named = ", ".join(
            f"{r.get('label', r['kind'])}={r['state']}"
            + (f" ({r['detail']})" if r["detail"] else "")
            for r in not_converged
        )
        line = (
            f"{counts[CONVERGED]}/{len(rows)} worker(s) converged; "
            f"NOT converged: {named}"
        )
    return {
        "workers": rows,
        "counts": counts,
        "all_converged": bool(rows) and not not_converged,
        "line": line,
    }
