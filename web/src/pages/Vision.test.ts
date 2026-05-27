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
import { computeTlBindings } from "./Vision";

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
