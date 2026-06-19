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

import { parseProgressList, STAKEHOLDER_SID } from "./TaskDetail";
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
