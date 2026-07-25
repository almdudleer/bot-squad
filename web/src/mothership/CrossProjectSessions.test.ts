/**
 * Tests for the pure helpers behind the cross-project Processes/Sessions
 * view (T-0661). Mirrors AllProjects.test.ts's approach — the fan-out +
 * network side stays untested here (covered by fanOut's own tests in
 * api.test.ts); these tests pin the pure section->targets->rows pipeline.
 */
import { describe, expect, test } from "vitest";

import {
  buildCrossProjectRows,
  buildFanoutTargets,
  sortCrossProjectRows,
  type CrossProjectRow,
  type SessionFanoutTarget,
} from "./CrossProjectSessions";
import type { ServerSection } from "./AllProjects";
import type { AttachedServer, FanOutResult, ServerProject } from "./api";
import type { SessionRow } from "../api";

function srv(id: string, extra: Partial<AttachedServer> = {}): AttachedServer {
  return {
    id,
    display_name: id,
    base_url: `https://${id}.example.com`,
    owner_user: "u",
    created_at: "2026-05-14T00:00:00Z",
    install_state: "ready",
    install_token_expires_at: null,
    last_seen_at: null,
    projects_cache: [],
    ...extra,
  };
}

function section(
  server: AttachedServer,
  kind: ServerSection["kind"],
  result: ServerSection["result"],
): ServerSection {
  return { server, kind, result };
}

function ok(serverId: string, data: ServerProject[]): FanOutResult<ServerProject[]> {
  return { serverId, ok: true, data };
}

function row(sid: string, extra: Partial<SessionRow> = {}): SessionRow {
  return {
    sid,
    status: "active",
    window: "0",
    cwd: "/x",
    ...extra,
  };
}

function target(server: AttachedServer, slug: string, projectLabel = slug): SessionFanoutTarget {
  return { server, slug, projectLabel };
}

describe("buildFanoutTargets", () => {
  test("collects one target per project of every LIVE, ok section", () => {
    const a = srv("a");
    const b = srv("b");
    const targets = buildFanoutTargets([
      section(a, "live", ok("a", [{ slug: "alpha", display_name: "Alpha", status: "idle" }])),
      section(b, "live", ok("b", [{ slug: "beta", display_name: "Beta", status: "idle" }])),
    ]);
    expect(targets).toEqual([
      { server: a, slug: "alpha", projectLabel: "Alpha" },
      { server: b, slug: "beta", projectLabel: "Beta" },
    ]);
  });

  test("skips installing sections (nothing to poll for sessions yet)", () => {
    const p = srv("p", { install_state: "pending" });
    const targets = buildFanoutTargets([section(p, "installing", null)]);
    expect(targets).toEqual([]);
  });

  test("skips unreachable (fan-out-failed) sections", () => {
    const a = srv("a");
    const targets = buildFanoutTargets([
      section(a, "live", { serverId: "a", ok: false, error: "504" }),
    ]);
    expect(targets).toEqual([]);
  });

  test("falls back to slug when display_name is empty", () => {
    const a = srv("a");
    const targets = buildFanoutTargets([
      section(a, "live", ok("a", [{ slug: "alpha", display_name: "", status: "idle" }])),
    ]);
    expect(targets[0].projectLabel).toBe("alpha");
  });

  test("a server with multiple projects yields one target per project, in order", () => {
    const a = srv("a");
    const targets = buildFanoutTargets([
      section(
        a,
        "live",
        ok("a", [
          { slug: "one", display_name: "One", status: "idle" },
          { slug: "two", display_name: "Two", status: "idle" },
        ]),
      ),
    ]);
    expect(targets.map((t) => t.slug)).toEqual(["one", "two"]);
  });
});

describe("sortCrossProjectRows", () => {
  const a = srv("a");

  function crossRow(sid: string, extra: Partial<SessionRow>): CrossProjectRow {
    return { server: a, slug: "alpha", projectLabel: "Alpha", row: row(sid, extra) };
  }

  test("awaiting-input rows always come first, regardless of activity", () => {
    const rows = [
      crossRow("idle-one", { activity: "idle" }),
      crossRow("waiting", { activity: "running", awaiting_input: true }),
      crossRow("running-one", { activity: "running" }),
    ];
    expect(sortCrossProjectRows(rows).map((r) => r.row.sid)).toEqual([
      "waiting",
      "running-one",
      "idle-one",
    ]);
  });

  test("running before idle before paused among non-waiting rows", () => {
    const rows = [
      crossRow("p", { activity: "paused" }),
      crossRow("i", { activity: "idle" }),
      crossRow("r", { activity: "running" }),
    ];
    expect(sortCrossProjectRows(rows).map((r) => r.row.sid)).toEqual(["r", "i", "p"]);
  });

  test("ties break on server/project name, not on original array order", () => {
    const b = srv("b");
    const rows: CrossProjectRow[] = [
      { server: b, slug: "zeta", projectLabel: "Zeta", row: row("z", { activity: "running" }) },
      { server: a, slug: "alpha", projectLabel: "Alpha", row: row("al", { activity: "running" }) },
    ];
    expect(sortCrossProjectRows(rows).map((r) => r.row.sid)).toEqual(["al", "z"]);
  });

  test("does not mutate the input array", () => {
    const rows = [crossRow("x", { activity: "idle" }), crossRow("y", { activity: "running" })];
    const copy = [...rows];
    sortCrossProjectRows(rows);
    expect(rows).toEqual(copy);
  });
});

describe("buildCrossProjectRows", () => {
  const a = srv("a");
  const b = srv("b");

  test("flattens ok results across targets into one row list", () => {
    const { rows, unreachableCount } = buildCrossProjectRows([
      {
        target: target(a, "alpha"),
        result: { serverId: "a alpha", ok: true, data: [row("s1", { activity: "running" })] },
      },
      {
        target: target(b, "beta"),
        result: { serverId: "b beta", ok: true, data: [row("s2", { activity: "idle" })] },
      },
    ]);
    expect(rows.map((r) => r.row.sid)).toEqual(["s1", "s2"]);
    expect(unreachableCount).toBe(0);
  });

  test("drops non-live rows (suspended/archived) — this is a 'what's running' glance, not history", () => {
    const { rows } = buildCrossProjectRows([
      {
        target: target(a, "alpha"),
        result: {
          serverId: "a alpha",
          ok: true,
          data: [
            row("live-one", { activity: "running", live: true }),
            row("dead-one", { activity: "suspended", live: false }),
            row("archived-one", { activity: "idle", live: true, archived: true }),
          ],
        },
      },
    ]);
    expect(rows.map((r) => r.row.sid)).toEqual(["live-one"]);
  });

  test("a failed target is counted as unreachable and doesn't poison sibling rows", () => {
    const { rows, unreachableCount } = buildCrossProjectRows([
      { target: target(a, "alpha"), result: { serverId: "a alpha", ok: false, error: "timeout" } },
      {
        target: target(b, "beta"),
        result: { serverId: "b beta", ok: true, data: [row("s2", { activity: "running" })] },
      },
    ]);
    expect(unreachableCount).toBe(1);
    expect(rows.map((r) => r.row.sid)).toEqual(["s2"]);
  });

  test("empty target list yields an empty, unreachable-free result", () => {
    expect(buildCrossProjectRows([])).toEqual({ rows: [], unreachableCount: 0 });
  });

  test("output is sorted (awaiting-input first) even though inputs arrive server-by-server", () => {
    const { rows } = buildCrossProjectRows([
      {
        target: target(a, "alpha"),
        result: { serverId: "a alpha", ok: true, data: [row("running", { activity: "running" })] },
      },
      {
        target: target(b, "beta"),
        result: {
          serverId: "b beta",
          ok: true,
          data: [row("waiting", { activity: "idle", awaiting_input: true })],
        },
      },
    ]);
    expect(rows.map((r) => r.row.sid)).toEqual(["waiting", "running"]);
  });
});
