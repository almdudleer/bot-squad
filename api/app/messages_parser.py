"""Parse Claude .jsonl session files into a stable message shape.

IMPORTANT: This parser MUST iterate line-by-line via ``for line in open(path)``.
It must NEVER call ``f.read()`` or load the entire file into memory.
Some session logs are 35 MB; full-load across multiple requests is a disaster.

Each yielded dict has shape::

    {
        "role": "user" | "assistant" | "tool" | "system",
        "ts": <iso-timestamp>,
        "text": <str, truncated to TEXT_MAX_BYTES by default>,
        "tool_uses": [{"id": str, "name": str, "input": dict}],   # assistant only
        "tool_result": {"tool_use_id": str, "output": str},       # tool only
    }

Unrecognised record types are silently skipped.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

TEXT_MAX_BYTES = 5 * 1024  # 5 KB per-message cap


def _truncate(text: str, max_bytes: int) -> str:
    """Truncate text to at most max_bytes UTF-8 bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[truncated]"


def _extract_text_from_content(content) -> str:
    """Extract plain text from a content block or list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("type", "")
                if t == "text":
                    parts.append(block.get("text", ""))
                elif t == "thinking":
                    parts.append(f"<thinking>{block.get('thinking', '')}</thinking>")
        return "\n".join(p for p in parts if p)
    return ""


def parse_messages(
    path: Path,
    limit: int = 200,
    offset: int = 0,
    full: bool = False,
) -> list[dict]:
    """Parse a .jsonl file line-by-line and return a list of message dicts.

    Args:
        path:   Path to the .jsonl file.
        limit:  Maximum number of messages to return.
        offset: Number of messages to skip from the start.
        full:   If True, do not truncate text fields.

    Returns:
        List of message dicts (see module docstring for shape).
    """
    out: list[dict] = []
    skipped = 0
    max_bytes = None if full else TEXT_MAX_BYTES

    with open(path) as f:  # noqa: WPS111 — intentional line-by-line iteration
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            rec_type = rec.get("type")

            # Codex rollout record. Keep the endpoint's stable message shape;
            # tool telemetry can be added later without blocking readable
            # user/assistant transcripts for provider-switched sessions.
            if rec_type == "response_item":
                payload = rec.get("payload") or {}
                if payload.get("type") != "message":
                    continue
                raw_role = payload.get("role")
                if raw_role not in ("user", "assistant"):
                    continue
                parts = []
                for block in payload.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") in ("input_text", "output_text", "text"):
                        parts.append(str(block.get("text") or ""))
                text = "\n".join(part for part in parts if part)
                if max_bytes:
                    text = _truncate(text, max_bytes)
                if not text.strip():
                    continue
                mapped = {
                    "role": raw_role,
                    "ts": rec.get("timestamp", ""),
                    "text": text,
                }
                if skipped < offset:
                    skipped += 1
                    continue
                if len(out) >= limit:
                    return out
                out.append(mapped)
                continue

            # ----------------------------------------------------------------
            # user record — may contain plain text or tool_result content
            # ----------------------------------------------------------------
            if rec_type == "user":
                msg = rec.get("message", {})
                content = msg.get("content")
                ts = rec.get("timestamp", "")

                if isinstance(content, list):
                    # Check if this is a tool result batch
                    tool_results = [
                        c for c in content
                        if isinstance(c, dict) and c.get("type") == "tool_result"
                    ]
                    plain_parts = [
                        c for c in content
                        if isinstance(c, dict) and c.get("type") != "tool_result"
                    ]

                    # Emit one "tool" message per tool_result
                    for tr in tool_results:
                        output = tr.get("content", "")
                        if isinstance(output, list):
                            output = "\n".join(
                                b.get("text", "") for b in output
                                if isinstance(b, dict) and b.get("type") == "text"
                            )
                        elif not isinstance(output, str):
                            output = str(output)

                        if max_bytes:
                            output = _truncate(output, max_bytes)

                        mapped: dict = {
                            "role": "tool",
                            "ts": ts,
                            "text": output,
                            "tool_result": {
                                "tool_use_id": tr.get("tool_use_id", ""),
                                "output": output,
                            },
                        }
                        if skipped < offset:
                            skipped += 1
                            continue
                        if len(out) >= limit:
                            return out
                        out.append(mapped)

                    # If there are plain text parts too, emit a user message
                    if plain_parts or isinstance(content, str):
                        text = _extract_text_from_content(
                            plain_parts if plain_parts else content
                        )
                        if max_bytes:
                            text = _truncate(text, max_bytes)
                        if text.strip():
                            mapped = {"role": "user", "ts": ts, "text": text}
                            if skipped < offset:
                                skipped += 1
                                continue
                            if len(out) >= limit:
                                return out
                            out.append(mapped)

                else:
                    # Plain string content
                    text = _extract_text_from_content(content)
                    if max_bytes:
                        text = _truncate(text, max_bytes)
                    if text.strip():
                        mapped = {"role": "user", "ts": ts, "text": text}
                        if skipped < offset:
                            skipped += 1
                            continue
                        if len(out) >= limit:
                            return out
                        out.append(mapped)

            # ----------------------------------------------------------------
            # assistant record
            # ----------------------------------------------------------------
            elif rec_type == "assistant":
                msg = rec.get("message", {})
                content = msg.get("content", [])
                ts = rec.get("timestamp", "")

                tool_uses = []
                text_parts = []

                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        bt = block.get("type", "")
                        if bt == "text":
                            text_parts.append(block.get("text", ""))
                        elif bt == "thinking":
                            text_parts.append(
                                f"<thinking>{block.get('thinking', '')}</thinking>"
                            )
                        elif bt == "tool_use":
                            tool_uses.append({
                                "id": block.get("id", ""),
                                "name": block.get("name", ""),
                                "input": block.get("input", {}),
                            })
                elif isinstance(content, str):
                    text_parts.append(content)

                text = "\n".join(p for p in text_parts if p)
                if max_bytes:
                    text = _truncate(text, max_bytes)

                # Skip pure-thinking messages with no text and no tool_uses
                if not text.strip() and not tool_uses:
                    continue

                mapped = {
                    "role": "assistant",
                    "ts": ts,
                    "text": text,
                    "tool_uses": tool_uses,
                }
                if skipped < offset:
                    skipped += 1
                    continue
                if len(out) >= limit:
                    return out
                out.append(mapped)

            # ----------------------------------------------------------------
            # All other types (queue-operation, attachment, etc.) — skip
            # ----------------------------------------------------------------
            else:
                continue

    return out
