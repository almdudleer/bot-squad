"""Shared field coercion for the hand-rolled ``payload: dict`` routes (T-0600).

The F2 500-class: ``(payload.get("x") or "").strip()`` lets any truthy
non-string (``123``, ``["a"]``, ``true``) survive the ``or ""`` and crash on
``.strip()`` → an unhandled AttributeError → 500, reachable pre-auth on
/auth/login. Routes that declare a Pydantic body model already get a clean
422; these helpers give the ~30 dict-payload routes the same guarantee (a
4xx naming the field) without converting every route to a model.

Keep this module tiny and dependency-light: every routes_*.py imports it.
"""
from __future__ import annotations

from fastapi import HTTPException


def str_field(payload: dict, key: str, *, strip: bool = True) -> str:
    """Read an optional string field from a JSON-dict payload.

    Missing / ``None`` → ``""``; a string → stripped (unless ``strip=False``,
    for secrets and file bodies where whitespace is significant); any other
    type → 400 naming the field — never a 500 off ``.strip()``.
    """
    val = payload.get(key)
    if val is None:
        return ""
    if not isinstance(val, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    return val.strip() if strip else val


def opt_str_field(payload: dict, key: str) -> str | None:
    """Like :func:`str_field` but preserves the absent-vs-empty distinction:
    missing / ``None`` → ``None`` (callers branch on "field not sent"), a
    string → as-is (no strip — used for bodies), any other type → 400."""
    val = payload.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise HTTPException(status_code=400, detail=f"{key} must be a string")
    return val
