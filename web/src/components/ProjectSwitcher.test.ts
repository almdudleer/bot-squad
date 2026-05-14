import { describe, expect, test } from "vitest";

import { statusBadgeClass } from "./ProjectSwitcher";

describe("ProjectSwitcher statusBadgeClass", () => {
  test("maps the canonical enum to distinct mc-badge variants", () => {
    expect(statusBadgeClass("working")).toBe("mc-badge mc-badge-active");
    expect(statusBadgeClass("needs-input")).toBe("mc-badge mc-badge-warn");
    expect(statusBadgeClass("idle")).toBe("mc-badge mc-badge-dim");
  });

  test("unknown status falls back to dim", () => {
    expect(statusBadgeClass("future-state")).toBe("mc-badge mc-badge-dim");
  });
});
