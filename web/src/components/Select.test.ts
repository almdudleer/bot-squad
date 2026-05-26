/**
 * T-0102: pure helpers powering the keyboard nav in Select.tsx. The
 * component itself can't be exercised under vitest (no DOM env), so
 * everything that determines the highlight cursor lives in pure
 * functions that this file covers.
 */
import { describe, expect, test } from "vitest";

import {
  findSelectedIndex,
  isActionItem,
  nextEnabledIndex,
  type SelectOption,
} from "./Select";

function v(value: string, disabled = false): SelectOption {
  return { value, label: value, disabled };
}

function a(key: string, disabled = false): SelectOption {
  return { action: true, key, label: key, onSelect: () => {}, disabled };
}

describe("Select / nextEnabledIndex", () => {
  test("steps forward, skipping disabled", () => {
    const opts = [v("a"), v("b", true), v("c"), v("d")];
    expect(nextEnabledIndex(opts, 0, 1)).toBe(2);
    expect(nextEnabledIndex(opts, 2, 1)).toBe(3);
  });

  test("wraps forward past the last item", () => {
    const opts = [v("a"), v("b"), v("c")];
    expect(nextEnabledIndex(opts, 2, 1)).toBe(0);
  });

  test("steps backward, skipping disabled", () => {
    const opts = [v("a"), v("b", true), v("c"), v("d")];
    expect(nextEnabledIndex(opts, 3, -1)).toBe(2);
    expect(nextEnabledIndex(opts, 2, -1)).toBe(0);
  });

  test("wraps backward before the first item", () => {
    const opts = [v("a"), v("b"), v("c")];
    expect(nextEnabledIndex(opts, 0, -1)).toBe(2);
  });

  test("Home convention: from=-1 dir=1 → first enabled", () => {
    // Used by trigger's Home key handler. -1 is "above the list".
    const opts = [v("a", true), v("b"), v("c")];
    expect(nextEnabledIndex(opts, -1, 1)).toBe(1);
  });

  test("End convention: from=0 dir=-1 → last enabled", () => {
    // Used by trigger's End key handler. The wrap brings us to the
    // tail end without depending on caller knowing the length.
    const opts = [v("a"), v("b"), v("c", true)];
    expect(nextEnabledIndex(opts, 0, -1)).toBe(1);
  });

  test("returns -1 when every option is disabled", () => {
    const opts = [v("a", true), v("b", true)];
    expect(nextEnabledIndex(opts, 0, 1)).toBe(-1);
    expect(nextEnabledIndex(opts, 0, -1)).toBe(-1);
  });

  test("returns -1 on empty list", () => {
    expect(nextEnabledIndex([], 0, 1)).toBe(-1);
    expect(nextEnabledIndex([], 0, -1)).toBe(-1);
  });

  test("action items participate in navigation (until disabled)", () => {
    const opts = [v("a"), a("new"), v("b")];
    // Forward from "a" lands on the action item, not skipping over it.
    expect(nextEnabledIndex(opts, 0, 1)).toBe(1);
    // Forward from the action lands on "b".
    expect(nextEnabledIndex(opts, 1, 1)).toBe(2);
  });

  test("disabled action items are skipped", () => {
    const opts = [v("a"), a("new", true), v("b")];
    expect(nextEnabledIndex(opts, 0, 1)).toBe(2);
  });
});

describe("Select / findSelectedIndex", () => {
  test("finds the value option matching `value`", () => {
    const opts = [v("a"), v("b"), v("c")];
    expect(findSelectedIndex(opts, "b")).toBe(1);
  });

  test("returns -1 when no value option matches", () => {
    const opts = [v("a"), v("b")];
    expect(findSelectedIndex(opts, "z")).toBe(-1);
  });

  test("ignores action items even when their key looks like a value", () => {
    // Action items don't carry a value field; an action with key="b"
    // must not be returned when searching for value "b".
    const opts = [v("a"), a("b")];
    expect(findSelectedIndex(opts, "b")).toBe(-1);
  });

  test("empty placeholder value returns -1 (no value option matches)", () => {
    // Callers use value="" for "nothing selected"; this returns -1 so
    // the closed-state renderer can fall through to the placeholder.
    const opts = [v("a"), v("b")];
    expect(findSelectedIndex(opts, "")).toBe(-1);
  });
});

describe("Select / isActionItem", () => {
  test("distinguishes action items from value options", () => {
    expect(isActionItem(v("a"))).toBe(false);
    expect(isActionItem(a("new"))).toBe(true);
  });
});
