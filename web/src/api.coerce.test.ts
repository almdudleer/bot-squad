/**
 * T-0206: a legacy/malformed backlog md once served `title` as an object
 * (`{"Recheck model switch": "..."}`) instead of a string. Rendered as a raw
 * React child it threw React error #31 and white-screened the whole board.
 * The md parser is fixed server-side; these pin the client-side defense so a
 * future field-shape regression degrades to readable text, never a crash.
 */
import { describe, expect, test } from "vitest";

import { coerceTaskText, normalizeTask, type Task } from "./api";

describe("coerceTaskText", () => {
  test("passes a normal string through untouched", () => {
    expect(coerceTaskText("Recheck model switch")).toBe("Recheck model switch");
  });

  test("null / undefined collapse to empty string (never an object child)", () => {
    expect(coerceTaskText(null)).toBe("");
    expect(coerceTaskText(undefined)).toBe("");
  });

  test("the exact #31 mapping renders its keys as text, not a crash", () => {
    // This is the real shape the broken parser produced for T-0012.
    const bad = { "Recheck model switch": "budget caps + usage-API reconciliation" };
    const out = coerceTaskText(bad as unknown);
    expect(typeof out).toBe("string");
    expect(out).toBe("Recheck model switch");
  });

  test("coerces other primitives to string", () => {
    expect(coerceTaskText(42 as unknown)).toBe("42");
    expect(coerceTaskText(true as unknown)).toBe("true");
  });
});

describe("normalizeTask", () => {
  test("forces a non-string title to a string so the SPA can't crash on it", () => {
    const raw = {
      id: "T-0012",
      title: { "Recheck model switch": "budget caps + usage-API reconciliation" },
      status: "open",
      body: "",
      path: "x.md",
    } as unknown as Task;
    const norm = normalizeTask(raw);
    expect(typeof norm.title).toBe("string");
    expect(norm.title).toBe("Recheck model switch");
    // Other fields are left intact.
    expect(norm.id).toBe("T-0012");
    expect(norm.status).toBe("open");
  });

  test("leaves a well-formed string title unchanged", () => {
    const raw: Task = {
      id: "T-0001",
      title: "Daily quiet-recheck digest",
      status: "open",
      body: "",
      path: "x.md",
    };
    expect(normalizeTask(raw).title).toBe("Daily quiet-recheck digest");
  });
});
