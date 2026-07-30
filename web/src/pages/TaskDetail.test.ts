/**
 * T-0238: the task is the board centerpiece — TaskDetail splits a user-facing
 * header (the ask) from the agent WORKING AREA (the reused progress-notes feed,
 * where the user's comments are recorded too, with no new schema field).
 *
 * These pin the pure logic behind that:
 *  - `parseProgressList` turns the "- <ts> · <sid> · <text>" lines into
 *    structured entries (newest-first is applied by the caller via .reverse()).
 *  - stakeholder comments carry `STAKEHOLDER_SID`, so the feed can label them
 *    "you" and accent them distinctly from agent notes.
 *  - `countNotes` (board card) counts those working-area entries.
 */
import { describe, expect, test } from "vitest";

import {
  decodeNoteText,
  parseProgressList,
  STAKEHOLDER_SID,
  firstTouchTs,
} from "./TaskDetail";
import { countNotes } from "../components/TaskCard";

const FEED = [
  "- 2026-06-19T17:58:59Z · S-almdudleer-multi_server-TL-p67 · FORK CALL: reuse progress-notes.",
  "- 2026-06-19T19:00:00Z · S-stakeholder · Looks good — ship it.",
].join("\n");

describe("parseProgressList", () => {
  test("parses ts · sid · text triples", () => {
    const out = parseProgressList(FEED);
    expect(out).toHaveLength(2);
    expect(out[0]).toEqual({
      ts: "2026-06-19T17:58:59Z",
      sid: "S-almdudleer-multi_server-TL-p67",
      text: "FORK CALL: reuse progress-notes.",
    });
  });

  // T-0835: the browser is the RENDER half of the note round-trip. The worker
  // stores a note on one physical line with its newlines escaped
  // (`task_body.encode_progress_text`); if this parser did not expand them the
  // shape would be lost one layer further out, with every byte still present —
  // which is exactly the failure being fixed, just relocated.
  test("expands escaped structure back into a multi-line note", () => {
    const stored =
      "- 2026-07-30T20:00:00Z · S-x · sweep:\\n\\n```bash\\nset -e\\ngrep -c x f\\n```";
    const out = parseProgressList(stored);
    expect(out).toHaveLength(1); // still ONE entry, not one per line
    expect(out[0].text).toBe("sweep:\n\n```bash\nset -e\ngrep -c x f\n```");
  });

  test("a pre-T-0835 note renders exactly as it always did", () => {
    // GREEN CONTROL: `\s` is not an escape this codec defines, so a note
    // quoting a regex must survive character-for-character. Nine thousand
    // existing notes depend on this arm, not on the one above.
    const out = parseProgressList("- 2026-07-30T20:00:00Z · S-x · re.sub(r'\\s+', ' ')");
    expect(out[0].text).toBe("re.sub(r'\\s+', ' ')");
  });

  test("decodeNoteText never decodes its own output", () => {
    // A literal backslash-n the author typed is stored doubled and must come
    // back as two characters — a chained-replace decoder returns a line break.
    expect(decodeNoteText("the two characters \\\\n, written out")).toBe(
      "the two characters \\n, written out",
    );
  });

  test("empty / blank progress yields no entries", () => {
    expect(parseProgressList("")).toEqual([]);
    expect(parseProgressList("\n\n")).toEqual([]);
  });

  test("preserves ' · ' inside the text body", () => {
    const out = parseProgressList("- 2026-06-19T19:00:00Z · S-x · a · b · c");
    expect(out[0].text).toBe("a · b · c");
  });

  test("stakeholder comments are identifiable by SID", () => {
    const out = parseProgressList(FEED);
    const mine = out.filter((p) => p.sid === STAKEHOLDER_SID);
    expect(mine).toHaveLength(1);
    expect(mine[0].text).toBe("Looks good — ship it.");
  });
});

describe("firstTouchTs (T-0291 session-history first-touch resolution)", () => {
  const TS = { "S-legacy": "2026-06-10T08:00:00Z", "S-live": "2026-06-15T09:00:00Z" };

  test("prefers the persisted ts even when the session is absent from /sessions (legacy)", () => {
    expect(firstTouchTs("S-legacy", TS, undefined)).toBe("2026-06-10T08:00:00Z");
  });

  test("persisted ts wins over the live started_at join", () => {
    expect(firstTouchTs("S-live", TS, "2026-06-22T18:00:00Z")).toBe("2026-06-15T09:00:00Z");
  });

  test("falls back to live started_at when no persisted ts", () => {
    expect(firstTouchTs("S-x", TS, "2026-06-22T18:00:00Z")).toBe("2026-06-22T18:00:00Z");
  });

  test("returns null (renders —) when neither exists", () => {
    expect(firstTouchTs("S-x", TS, undefined)).toBeNull();
    expect(firstTouchTs("S-x", null, undefined)).toBeNull();
    expect(firstTouchTs("S-x", undefined, undefined)).toBeNull();
  });
});

describe("countNotes (board card working-area count)", () => {
  test("counts each working-area entry", () => {
    expect(countNotes(FEED)).toBe(2);
  });

  test("undefined / empty progress counts zero", () => {
    expect(countNotes(undefined)).toBe(0);
    expect(countNotes("")).toBe(0);
  });

  test("ignores non-entry lines (headings, blanks)", () => {
    const mixed = "## Progress\n\n- 2026-01-01T00:00:00Z · S-x · one\nnot a note\n";
    expect(countNotes(mixed)).toBe(1);
  });
});
