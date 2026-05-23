#!/usr/bin/env python3
"""One-shot migration: split UserMeta into GlobalUser + Attachment (T-0066).

Walks ``config/auth.toml`` on this install, mints a GlobalUser per
ServerUser row in the mothership ``users.json`` registry (re-using the
existing ServerUser bcrypt hash as the GlobalUser's canonical password
hash), then writes an Attachment per (user × this server's is_self id)
carrying the existing ``tg_chat_id`` and ``seen_steps`` fields.

Finally rewrites ``auth.toml`` with the new ``attached_to_global_user``
field set on each migrated row so subsequent boots see the binding.

Idempotent:
- Already-minted GlobalUsers (by username) are reused, not re-minted.
- Already-attached ServerUsers (non-empty ``attached_to_global_user``) are
  skipped entirely — no re-mint, no Attachment overwrite of fresh state.
- Re-running the script with no pending rows is a no-op.

MUST be run on the MOTHERSHIP install (the only one with a populated
``_mothership/`` directory). Single-install consumer servers will pick up
attachment via ``/api/auth/attach`` instead — out of scope here.

Usage:
    python scripts/migrate/users_split.py [--dry-run] \\
        --config-dir /path/to/config \\
        --data-dir   /path/to/data \\
        [--server-id srv_<hex>]

If ``--server-id`` is omitted, the script picks the ``is_self`` row from
``_mothership/servers.json`` — that's the mothership's own server_id, which
is the right binding target for users defined in its own ``auth.toml``.
"""
from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import asdict, replace
from pathlib import Path

# Make ``api/app/*`` importable when running this script standalone.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "api"))

from app.config import AuthConfig, UserMeta  # noqa: E402
from app.mothership_store import MothershipStore  # noqa: E402
from app.mothership_users_store import MothershipUsersStore  # noqa: E402
from app.routes_users import _serialize_auth_toml, _ttl_to_string  # noqa: E402


def _resolve_self_server_id(data_dir: Path) -> str | None:
    store = MothershipStore(data_dir / "_mothership")
    for s in store.list_servers():
        if s.is_self:
            return s.id
    return None


def migrate(
    *,
    config_dir: Path,
    data_dir: Path,
    server_id: str | None,
    dry_run: bool,
) -> dict:
    """Walk auth.toml + mint GlobalUsers + write Attachments. Returns a stats dict."""
    auth_path = config_dir / "auth.toml"
    if not auth_path.is_file():
        raise SystemExit(f"auth.toml not found at {auth_path}")
    cfg = AuthConfig.load(config_dir)

    if server_id is None:
        server_id = _resolve_self_server_id(data_dir)
        if server_id is None:
            raise SystemExit(
                "No is_self server in _mothership/servers.json — pass --server-id "
                "explicitly, or boot the mothership API once to self-register."
            )

    users_store = MothershipUsersStore(data_dir / "_mothership")

    stats = {
        "scanned": 0,
        "skipped_already_attached": 0,
        "global_users_minted": 0,
        "global_users_reused": 0,
        "attachments_created": 0,
        "attachments_updated": 0,
        "server_id": server_id,
        "dry_run": dry_run,
    }

    new_user_meta: dict[str, UserMeta] = dict(cfg.user_meta)
    dirty = False

    for username, password_hash in cfg.users.items():
        stats["scanned"] += 1
        meta = cfg.meta_for(username)
        if meta.attached_to_global_user:
            stats["skipped_already_attached"] += 1
            continue

        if dry_run:
            # Dry-run: do not touch the registry; only count what we'd do.
            existing = users_store.user_by_username(username)
            if existing is None:
                stats["global_users_minted"] += 1
                stats["attachments_created"] += 1
            else:
                stats["global_users_reused"] += 1
                existing_attachment = users_store.get_attachment(existing.id, server_id)
                if existing_attachment is None:
                    stats["attachments_created"] += 1
                else:
                    stats["attachments_updated"] += 1
            continue

        gu, created = users_store.upsert_user_by_username(
            username=username,
            password_hash=password_hash,
            # Display name + email + timezone aren't in the legacy schema.
            # The /api/m/users super-admin UI (T-0062) will let an operator
            # fill them in post-migration.
            display_name="",
            email="",
            timezone_name="UTC",
            # is_super_admin is intentionally NOT lifted from is_admin: the
            # ServerUser admin flag is per-server, while super-admin gates
            # the cross-server MOTHERSHIP section. Operators promote
            # explicitly via the super-admin UI after migration.
            is_super_admin=False,
        )
        if created:
            stats["global_users_minted"] += 1
        else:
            stats["global_users_reused"] += 1

        _, created_attach = users_store.upsert_attachment(
            global_user_id=gu.id,
            server_id=server_id,
            server_username=username,
            tg_chat_id=meta.tg_chat_id,
            seen_steps=meta.seen_steps,
            last_seen_at=None,
        )
        if created_attach:
            stats["attachments_created"] += 1
        else:
            stats["attachments_updated"] += 1

        new_user_meta[username] = replace(meta, attached_to_global_user=gu.id)
        dirty = True

    if dirty and not dry_run:
        text = _serialize_auth_toml(
            dict(cfg.users),
            new_user_meta,
            _ttl_to_string(cfg.session_ttl_seconds),
        )
        tmp = auth_path.with_suffix(".toml.tmp")
        tmp.write_text(text)
        os.rename(tmp, auth_path)

    return stats


def _summary(stats: dict) -> str:
    lines = [
        f"server_id:               {stats['server_id']}",
        f"dry_run:                 {stats['dry_run']}",
        f"users scanned:           {stats['scanned']}",
        f"already attached (skip): {stats['skipped_already_attached']}",
        f"global users minted:     {stats['global_users_minted']}",
        f"global users reused:     {stats['global_users_reused']}",
        f"attachments created:     {stats['attachments_created']}",
        f"attachments updated:     {stats['attachments_updated']}",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config-dir", required=True, type=Path)
    ap.add_argument("--data-dir", required=True, type=Path)
    ap.add_argument("--server-id", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    stats = migrate(
        config_dir=args.config_dir,
        data_dir=args.data_dir,
        server_id=args.server_id,
        dry_run=args.dry_run,
    )
    print(_summary(stats))
    return 0


# Touch the imported helper so a future lint pass doesn't drop tomllib here —
# auth.toml validation lives in AuthConfig.load, but a follow-up may want to
# pre-validate before mutating the registry.
_ = tomllib

if __name__ == "__main__":
    raise SystemExit(main())
