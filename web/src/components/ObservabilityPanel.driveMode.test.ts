import { describe, expect, it } from "vitest";
import { driveModeCopy } from "./ObservabilityPanel";
import type { DriveMode } from "../api";

/**
 * T-0828 / D-0069 — the DRIVE MODE card's copy.
 *
 * This is the surface the stakeholder actually looks at («для меня»), and the
 * WORDING is the deliverable, so it is pinned as a pure function rather than
 * through a render (the `liveSessionsCardCopy` precedent in the same file).
 *
 * The load-bearing case is `describe("a rejected stored value")` below: it
 * guards the defect the T-0828 manual walkthrough found, which no per-field
 * assertion could see. An unpinned fix is one refactor from regressing.
 */

const base: DriveMode = {
  scope: "all",
  stop_when: "scope_exhausted",
  on_stop: "nothing",
  set_by: null,
  set_at: null,
  source_text: null,
  configured: false,
  invalid: {},
};

const HIS_WORDS = "закончить всё что в опен";

describe("driveModeCopy — the three states", () => {
  it("reports UNKNOWN, not 'no mode set', when the server did not send the field", () => {
    // A pre-T-0828 api. Rendering this as "not set" would be a claim about his
    // settings the server never made — the silent-None failure D-0069 names.
    const copy = driveModeCopy(undefined);
    expect(copy.state).toBe("unknown");
    expect(copy.headline).toMatch(/unknown/i);
    expect(copy.headline).not.toMatch(/not set/i);
    expect(copy.provenance).toBeNull();
  });

  it("distinguishes 'never set' from 'deliberately set to the widest'", () => {
    // THE point of `configured`. Both read scope=all, and telling them apart is
    // literally the question he asked («какой режим драйва щас стоит»).
    const never = driveModeCopy({ ...base, configured: false });
    const chosen = driveModeCopy({
      ...base,
      configured: true,
      set_at: "2026-07-30T10:00:00Z",
      set_by: "S-op",
    });

    expect(never.state).toBe("unset");
    expect(chosen.state).toBe("set");
    expect(never.provenance).toBeNull();
    expect(chosen.provenance).toContain("set 2026-07-30");

    // Same effective scope, different reported state — if these two ever
    // collapse to one rendering, his question is unanswerable again.
    expect(never.headline).toContain("scope: all");
    expect(chosen.headline).toContain("scope: all");
    expect(never.state).not.toBe(chosen.state);
  });

  it("shows all three axes and the verbatim words that set them", () => {
    const copy = driveModeCopy({
      ...base,
      scope: "open_reopened",
      stop_when: "spend_quota",
      on_stop: "alert",
      configured: true,
      set_by: "S-almdudleer-operator-p533",
      set_at: "2026-07-30T14:52:31Z",
      source_text: HIS_WORDS,
    });
    expect(copy.headline).toBe(
      "scope: open_reopened · stop when: spend_quota · on stop: alert",
    );
    // The provenance line IS the visibility half — it answers "did my
    // instruction land" instead of leaving him to infer it from behaviour.
    expect(copy.provenance).toContain("set 2026-07-30 14:52");
    expect(copy.provenance).toContain("by S-almdudleer-operator-p533");
    expect(copy.provenance).toContain(`«${HIS_WORDS}»`);
    expect(copy.warnings).toEqual([]);
  });
});

describe("a rejected stored value — the walkthrough defect", () => {
  /**
   * WHAT THIS GUARDS. With a hand-edited `scope: "opne"` the card rendered:
   *
   *     scope: all — set 2026-07-30 15:14 from «закончить всё что в опен»
   *
   * Every field there is individually correct and the sentence is FALSE: his
   * words asked for `open_reopened`, the stored value was rejected, and `all`
   * is OUR fallback. So the line attributed a value he never chose to words he
   * did say — a manufactured quotation, in the one surface built to make him
   * trust what he is reading.
   *
   * The rule: PROVENANCE BELONGS TO THE REQUEST, NOT TO THE RESULT.
   */
  const rejected: DriveMode = {
    ...base,
    scope: "all", // the server already fell back
    configured: true,
    set_by: "S-almdudleer-operator-p533",
    set_at: "2026-07-30T15:14:34Z",
    source_text: HIS_WORDS,
    invalid: { scope: "opne" },
  };

  it("never presents the fallback as an attributed choice", () => {
    const copy = driveModeCopy(rejected);
    // THE REGRESSION ASSERTION: the exact fused sentence that was wrong.
    expect(copy.headline).not.toBe("scope: all");
    expect(`${copy.headline} ${copy.provenance}`).not.toMatch(
      /scope: all\b(?!.*requested)/,
    );
    // Provenance attaches to the REQUEST — the verb must not claim `all` was set.
    expect(copy.provenance).toContain("requested");
    expect(copy.provenance).not.toMatch(/\bset 2026/);
  });

  it("carries BOTH facts: what he asked for, raw, and what is in effect", () => {
    const copy = driveModeCopy(rejected);
    // His own typo, verbatim, so he can see it — not swallowed.
    expect(copy.headline).toContain('"opne"');
    expect(copy.headline).toContain("requested");
    expect(copy.headline).toContain("not recognised");
    // ...and the effective value, explicitly labelled as the one in effect.
    expect(copy.headline).toContain('"all" in effect');
    // His words are still shown — the provenance is real, only its attachment
    // was wrong. Dropping them would lose the evidence he asked for.
    expect(copy.provenance).toContain(`«${HIS_WORDS}»`);
  });

  it("raises a warning naming the field, the raw value and the default", () => {
    const copy = driveModeCopy(rejected);
    expect(copy.warnings).toHaveLength(1);
    expect(copy.warnings[0]).toContain("scope");
    expect(copy.warnings[0]).toContain('"opne"');
    expect(copy.warnings[0]).toContain('"all"');
  });

  it("handles every axis being rejected at once", () => {
    const copy = driveModeCopy({
      ...base,
      configured: true,
      set_at: "2026-07-30T15:14:34Z",
      source_text: HIS_WORDS,
      invalid: { scope: "OPEN", stop_when: "forever", on_stop: "ALERT!" },
    });
    expect(copy.warnings).toHaveLength(3);
    for (const raw of ['"OPEN"', '"forever"', '"ALERT!"']) {
      expect(copy.headline).toContain(raw);
    }
    expect(copy.provenance).toContain("requested");
  });

  it("keeps a VALID axis clean while another is rejected", () => {
    // Mixed state: the rejection must not contaminate the axis that is fine.
    const copy = driveModeCopy({
      ...base,
      scope: "in_progress",
      configured: true,
      set_at: "2026-07-30T15:14:34Z",
      invalid: { on_stop: "ALERT!" },
    });
    expect(copy.headline).toContain("scope: in_progress");
    expect(copy.headline).not.toMatch(/scope: in_progress[^·]*requested/);
    expect(copy.headline).toContain('"ALERT!" requested');
  });
});

describe("driveModeCopy — defensive shapes", () => {
  it("survives an absent `invalid` map from an older server", () => {
    // Older api, or a hand-rolled payload: `invalid` missing entirely.
    const partial = { ...base, configured: true, set_at: "2026-07-30T15:14:34Z" };
    delete (partial as Partial<DriveMode>).invalid;
    const copy = driveModeCopy(partial as DriveMode);
    expect(copy.warnings).toEqual([]);
    expect(copy.headline).toContain("scope: all");
  });

  it("passes an unparseable set_at through rather than dropping it", () => {
    // An unreadable stamp is still evidence of WHEN; hiding it would be the
    // same silent omission this panel exists to close.
    const copy = driveModeCopy({
      ...base,
      configured: true,
      set_at: "not-a-date",
    });
    expect(copy.provenance).toContain("not-a-date");
  });

  it("renders provenance from source_text alone when there is no stamp", () => {
    const copy = driveModeCopy({
      ...base,
      configured: true,
      source_text: HIS_WORDS,
    });
    expect(copy.provenance).toContain(`«${HIS_WORDS}»`);
  });
});
