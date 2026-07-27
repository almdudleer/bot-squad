/**
 * T-0727 — the Processes role badge must not paint a user-conversation
 * session as a dev worker. Renders RoleBadge directly (renderToStaticMarkup,
 * the ObservabilityPanel/SystemSettings test pattern — no DOM needed).
 *
 * Manual walkthrough against the live `*-user-conversation-*` row on staging
 * passed first per T-0158; this locks the label + colour class.
 */
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, test } from "vitest";

import type { SessionRow } from "../api";
import { RoleBadge } from "./Sessions";

function row(overrides: Partial<SessionRow>): SessionRow {
  return {
    sid: "S-test",
    status: "active",
    window: "w",
    cwd: "/tmp",
    task_id: null,
    initiative: null,
    extra_initiatives: [],
    ...overrides,
  };
}

describe("RoleBadge", () => {
  test("a user-conversation session reads 'User chat' in its own colour, not dev-blue", () => {
    const html = renderToStaticMarkup(
      <RoleBadge
        row={row({
          sid: "S-almdudleer-gu_dc8262b6cea9098d98e04d7e-user-conversation-p5",
          role: "user-conversation",
        })}
      />,
    );
    expect(html).toContain("User chat");
    expect(html).toContain("mc-badge-user");
    expect(html).not.toContain("mc-badge-info"); // the dev-blue class
    expect(html).not.toContain("Dev");
  });

  test("a dev session is unchanged (dev-blue)", () => {
    const html = renderToStaticMarkup(<RoleBadge row={row({ role: "dev", task_id: "T-1" })} />);
    expect(html).toContain("Dev");
    expect(html).toContain("mc-badge-info");
  });

  test("T-0175 fallback intact: an unknown/missing role is still dev, never teamlead", () => {
    const unknown = renderToStaticMarkup(<RoleBadge row={row({ role: "bogus" })} />);
    expect(unknown).toContain("Dev");
    expect(unknown).not.toContain("Teamlead");
    const missing = renderToStaticMarkup(<RoleBadge row={row({})} />);
    expect(missing).toContain("Dev");
    expect(missing).not.toContain("Teamlead");
  });
});
