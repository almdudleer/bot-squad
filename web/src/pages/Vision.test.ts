/**
 * T-0100: roadmap-side TL ownership resolver.
 *
 * `computeTlBindings` is the pure helper Vision.tsx uses to decide which
 * TL sessions are bound to which initiatives. It MUST match the worker's
 * `_find_owner` (worker/bot_squad_worker/sessions.py): scan every session
 * (excluding archived), pick out TLs (no primary task_id), bind them to
 * any initiative listed in `initiative` or `extra_initiatives`. Status
 * is NOT a filter.
 *
 * The bug this guards against: an earlier version filtered by
 * status==="active", so a paused/suspended TL owning an initiative made
 * the roadmap render "no TL" while the worker still routed peer_send
 * traffic to it and blocked re-binding.
 */
import { describe, expect, test } from "vitest";

import { SessionRow } from "../api";
import {
  computeTlBindings,
  initiativeDisplayName,
  initiativeKindBadge,
  isPersistentInitiative,
  memberCountLabel,
} from "./Vision";

function tl(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test-tl-p0",
    status: "active",
    window: "tl",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  } as SessionRow;
}

describe("computeTlBindings", () => {
  test("binds an active TL via primary `initiative`", () => {
    const sessions = [
      tl({ sid: "S-a", status: "active", initiative: "alpha.md" }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")?.map((s) => s.sid)).toEqual(["S-a"]);
  });

  test("binds a TL via `extra_initiatives` (Phase 9 multi-binding)", () => {
    const sessions = [
      tl({
        sid: "S-multi",
        status: "active",
        initiative: "alpha.md",
        extra_initiatives: ["beta.md", "gamma.md"],
      }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")?.map((s) => s.sid)).toEqual(["S-multi"]);
    expect(tlsByInitiative.get("beta.md")?.map((s) => s.sid)).toEqual(["S-multi"]);
    expect(tlsByInitiative.get("gamma.md")?.map((s) => s.sid)).toEqual(["S-multi"]);
  });

  test("shows a paused TL as still bound — matches worker `_find_owner`", () => {
    // The original bug: status filter hid paused/suspended TLs from the
    // roadmap even though the worker still considers them the owner.
    const sessions = [
      tl({ sid: "S-paused", status: "paused", initiative: "alpha.md" }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")?.map((s) => s.sid)).toEqual(["S-paused"]);
  });

  test("shows a suspended TL as still bound", () => {
    const sessions = [
      tl({ sid: "S-susp", status: "suspended", initiative: "alpha.md" }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")?.map((s) => s.sid)).toEqual(["S-susp"]);
  });

  test("excludes archived sessions", () => {
    // Archived = "gone for real"; surfacing the chip would mislead.
    const sessions = [
      tl({ sid: "S-arch", status: "suspended", initiative: "alpha.md", archived: true }),
    ];
    const { tlsByInitiative, candidateTls } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")).toBeUndefined();
    expect(candidateTls).toEqual([]);
  });

  test("excludes dev sessions (non-empty primary task_id)", () => {
    const sessions = [
      tl({ sid: "S-dev", task_id: "T-0042", initiative: "alpha.md" }),
    ];
    const { tlsByInitiative, candidateTls } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")).toBeUndefined();
    expect(candidateTls).toEqual([]);
  });

  test("treats task_id == '~' as a TL (worker convention)", () => {
    const sessions = [
      tl({ sid: "S-tilde", task_id: "~", initiative: "alpha.md" }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.get("alpha.md")?.map((s) => s.sid)).toEqual(["S-tilde"]);
  });

  test("ignores '~' / empty entries in extra_initiatives", () => {
    const sessions = [
      tl({ sid: "S-noise", extra_initiatives: ["~", "", "beta.md"] }),
    ];
    const { tlsByInitiative } = computeTlBindings(sessions);
    expect(tlsByInitiative.size).toBe(1);
    expect(tlsByInitiative.get("beta.md")?.map((s) => s.sid)).toEqual(["S-noise"]);
  });

  test("returns all non-archived TLs in candidateTls (for the bind dropdown)", () => {
    const sessions = [
      tl({ sid: "S-active" }),
      tl({ sid: "S-paused", status: "paused" }),
      tl({ sid: "S-susp", status: "suspended" }),
      tl({ sid: "S-arch", archived: true }),
      tl({ sid: "S-dev", task_id: "T-0001" }),
    ];
    const { candidateTls } = computeTlBindings(sessions);
    expect(candidateTls.map((s) => s.sid)).toEqual([
      "S-active",
      "S-paused",
      "S-susp",
    ]);
  });
});

// T-0354: an initiative is flagged PERSISTENT via initiative_kind on the
// backing kind:initiative task (D-0059) — replaces the T-0411 constant_team
// body-regex, which was dead post-T-0480 (no kind:initiative task ever
// carried a constant_team line in its body).
describe("isPersistentInitiative", () => {
  test("true when initiative_kind is persistent", () => {
    expect(isPersistentInitiative({ initiative_kind: "persistent" })).toBe(true);
  });
  test("false for one-shot / absent / undefined", () => {
    expect(isPersistentInitiative({ initiative_kind: "one-shot" })).toBe(false);
    expect(isPersistentInitiative({})).toBe(false);
    expect(isPersistentInitiative(undefined)).toBe(false);
  });
});

// T-0428 (dogfood): initiative rows render a clean display name.
describe("initiativeDisplayName", () => {
  test("strips initiatives/ + .md and titlecases (preserving caps/digit tokens)", () => {
    expect(initiativeDisplayName("initiatives/multi-server-installation-process.md")).toBe("Multi Server Installation Process");
    expect(initiativeDisplayName("INI-01-persistent-initiatives.md")).toBe("INI 01 Persistent Initiatives");
    expect(initiativeDisplayName("operator-ux-and-session-mgmt.md")).toBe("Operator Ux And Session Mgmt");
  });
});

// T-0295 (a): EVERY initiative row gets a kind badge. Before this, only
// persistent rows were badged, so "no badge" was ambiguous between "this
// initiative ends" and "the kind never reached the FE" — which, post-T-0480,
// is what it usually meant.
describe("initiativeKindBadge", () => {
  test("persistent → PERSISTENT", () => {
    expect(initiativeKindBadge({ initiative_kind: "persistent" }).label).toBe("PERSISTENT");
  });
  test("one-shot → ENDING", () => {
    expect(initiativeKindBadge({ initiative_kind: "one-shot" }).label).toBe("ENDING");
  });
  test("absent kind → ENDING, never blank", () => {
    expect(initiativeKindBadge({}).label).toBe("ENDING");
    expect(initiativeKindBadge(undefined).label).toBe("ENDING");
    expect(initiativeKindBadge(null).label).toBe("ENDING");
  });
  test("the two kinds are visually distinct", () => {
    expect(initiativeKindBadge({ initiative_kind: "persistent" }).cls).not.toBe(
      initiativeKindBadge({ initiative_kind: "one-shot" }).cls,
    );
  });
});

// T-0295 (b): the member count reads against the team_size cap.
describe("memberCountLabel", () => {
  test("renders live vs cap", () => {
    expect(memberCountLabel(1, 2)).toBe("1/2 members");
  });
  test("says so when live exceeds the cap instead of reading as normal", () => {
    expect(memberCountLabel(3, 1)).toBe("3/1 members (over cap)");
  });
  test("no cap known (orphan tick state) → plain count, no fake denominator", () => {
    expect(memberCountLabel(2, null)).toBe("2 member(s)");
  });
});
