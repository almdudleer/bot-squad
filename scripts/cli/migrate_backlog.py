#!/usr/bin/env python3
"""Decompose a legacy BACKLOG.md into per-task and per-feedback files.

Output schema:
- tasks: list of {id, title, status, body}
- feedback: list of {id, date, body}
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


SECTION_TO_STATUS = {
    "Must": "open",
    "Should": "open",
    "Needs Thinking": "open",
    "In Progress": "open",
    "Review": "open",
    "Reopened": "reopened",
    "To Test": "totest",
    "Released": "closed",
    "Could": "open",
    "Deferred to v0.6 — UI overhaul (topic monitoring)": "closed",
    "Deferred to v0.6": "closed",
}

# Lines like "- **Title**." or "- [ ] Title." or "- [x] Title."
ITEM_HEAD_RE = re.compile(
    r"^- (?:\[(?P<box>[ x])\]\s+)?(?:\*\*(?P<bold>[^*]+)\*\*\.?|(?P<plain>.+?))(?:\.\s|\n|$)"
)


def split_sections(text: str) -> dict[str, str]:
    """Return {section_name: section_body_text}."""
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^## (.+?)\s*$", line)
        if m:
            if current is not None:
                sections[current] = "".join(buf)
            current = m.group(1).strip()
            buf = []
        else:
            if current is not None:
                buf.append(line)
    if current is not None:
        sections[current] = "".join(buf)
    return sections


def split_items(section_body: str) -> list[tuple[str | None, str]]:
    """Yield (title_or_None, full_item_text) tuples."""
    out: list[tuple[str | None, str]] = []
    current_title: str | None = None
    current_lines: list[str] = []
    done = False
    for line in section_body.splitlines(keepends=True):
        m = ITEM_HEAD_RE.match(line)
        if m:
            if current_lines:
                out.append((current_title, "".join(current_lines)))
            title = (m.group("bold") or m.group("plain") or "").strip().rstrip(".")
            current_title = title
            current_lines = [line]
            done = m.group("box") == "x"
        elif line.startswith("- ") or line.strip() == "":
            # Boundary or blank — accumulate; bare "- " bullets without ** are
            # just sub-bullets of the current item.
            current_lines.append(line)
        else:
            current_lines.append(line)
    if current_lines:
        out.append((current_title, "".join(current_lines)))
    return out


def slugify(s: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s.lower()).strip("-")
    return s[:60]


def decompose(backlog_path: Path) -> dict:
    text = backlog_path.read_text()
    sections = split_sections(text)
    tasks: list[dict] = []
    feedback: list[dict] = []
    seq = 0
    for section, body in sections.items():
        if section == "User Feedback":
            for title, item in split_items(body):
                if not title:
                    continue
                seq += 1
                feedback.append({
                    "id": f"F-{seq:04d}",
                    "title": title,
                    "body": item.strip(),
                })
            continue
        status = SECTION_TO_STATUS.get(section)
        if status is None:
            # Unknown section — skip; ops/feature_v06.md handles spec'd items.
            continue
        for title, item in split_items(body):
            if not title:
                continue
            seq += 1
            done = item.strip().startswith("- [x]")
            t_status = "closed" if (status == "closed" or done) else status
            tasks.append({
                "id": f"T-{seq:04d}",
                "title": title,
                "status": t_status,
                "body": item.strip(),
                "section": section,
            })
    return {"tasks": tasks, "feedback": feedback}


def write_outputs(data: dict, out_dir: Path) -> None:
    backlog_dir = out_dir / "backlog"
    feedback_dir = out_dir / "feedback"
    backlog_dir.mkdir(parents=True, exist_ok=True)
    feedback_dir.mkdir(parents=True, exist_ok=True)
    for t in data["tasks"]:
        fn = f"{t['id']}-{slugify(t['title'])}.md"
        (backlog_dir / fn).write_text(
            f"---\nid: {t['id']}\ntitle: {t['title']}\nstatus: {t['status']}\n---\n\n"
            f"{t['body']}\n"
        )
    for f in data["feedback"]:
        fn = f"{f['id']}-{slugify(f['title'])}.md"
        (feedback_dir / fn).write_text(
            f"# {f['title']}\n\n{f['body']}\n"
        )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("backlog", type=Path, help="legacy BACKLOG.md path")
    p.add_argument("--out", type=Path, required=True, help="output data/<slug>/ dir")
    args = p.parse_args(argv)
    data = decompose(args.backlog)
    write_outputs(data, args.out)
    print(f"wrote {len(data['tasks'])} tasks and {len(data['feedback'])} feedback items to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
