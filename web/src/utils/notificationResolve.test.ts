import { describe, expect, test } from "vitest";

import { effectiveSummary, levelRowViews } from "./notificationResolve";
import type { NotificationsResolved } from "../api";

// T-0218 — these mirror the manual walkthrough (scenario
// T-0218-...-global-server-proj.md): the FE never recomputes precedence, it
// only flattens the server's resolved payload into per-row view state.

function resolved(
  global: string | null,
  server: string | null,
  project: string | null,
): NotificationsResolved {
  let tg_chat_id: string | null = null;
  let source: NotificationsResolved["effective"]["source"] = "none";
  if (project) (tg_chat_id = project), (source = "project");
  else if (server) (tg_chat_id = server), (source = "server");
  else if (global) (tg_chat_id = global), (source = "global");
  return {
    levels: {
      global: { tg_chat_id: global, set: !!global },
      server: { server_id: "self", tg_chat_id: server, set: !!server },
      project: { slug: "bot-squad", tg_chat_id: project, set: !!project },
    },
    effective: { tg_chat_id, source },
  };
}

describe("levelRowViews", () => {
  test("stable global→server→project order with raw + set + winner flags", () => {
    const rows = levelRowViews(resolved("111", null, null));
    expect(rows.map((r) => r.key)).toEqual(["global", "server", "project"]);
    expect(rows[0]).toMatchObject({ key: "global", raw: "111", set: true, isWinner: true });
    expect(rows[1]).toMatchObject({ key: "server", raw: "", set: false, isWinner: false });
    expect(rows[2]).toMatchObject({ key: "project", raw: "", set: false, isWinner: false });
  });

  test("most-specific override is the winner; lower levels keep their value+set", () => {
    const rows = levelRowViews(resolved("111", "222", null));
    expect(rows[0]).toMatchObject({ key: "global", raw: "111", set: true, isWinner: false });
    expect(rows[1]).toMatchObject({ key: "server", raw: "222", set: true, isWinner: true });
  });

  test("project override wins over server and global", () => {
    const rows = levelRowViews(resolved("111", "222", "333"));
    expect(rows.find((r) => r.isWinner)?.key).toBe("project");
  });

  test("null at every level → no winner", () => {
    const rows = levelRowViews(resolved(null, null, null));
    expect(rows.every((r) => !r.isWinner && r.raw === "" && !r.set)).toBe(true);
  });
});

describe("effectiveSummary", () => {
  test("names the winning value + source", () => {
    expect(effectiveSummary(resolved("111", "222", null))).toBe("Effective: 222 (from server)");
    expect(effectiveSummary(resolved("111", null, null))).toBe("Effective: 111 (from global)");
    expect(effectiveSummary(resolved("111", "222", "333"))).toBe("Effective: 333 (from project)");
  });

  test("none → explains no personal target is set", () => {
    expect(effectiveSummary(resolved(null, null, null))).toBe(
      "Effective: none — no personal target set at any level",
    );
  });

  test("blank effective value renders as none even if source disagrees", () => {
    // Defensive: a "" effective must never render as a real target, regardless
    // of what `source` claims (guards the `!tg_chat_id` branch).
    const r = resolved(null, null, null);
    expect(
      effectiveSummary({ ...r, effective: { tg_chat_id: "", source: "global" } }),
    ).toBe("Effective: none — no personal target set at any level");
  });
});
