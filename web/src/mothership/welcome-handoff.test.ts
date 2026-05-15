/**
 * T-0013: pure-helper tests for the mothership → target-/welcome handoff.
 *
 * The ServerProgress component is React; the *trigger* (print_attach=done)
 * and the *URL composer* (base_url + /welcome) are pure functions exported
 * from routes.tsx so they can be exercised without a DOM. Component-level
 * coverage would require jsdom which the package doesn't pull in (see
 * package.json — vitest only, no testing-library).
 */
import { describe, expect, test } from "vitest";

import { isInstallComplete, welcomeUrlFor } from "./routes";
import type { Checkpoint } from "./api";

function ev(checkpoint: string, status: Checkpoint["status"]): Checkpoint {
  return {
    checkpoint,
    status,
    hostname: "test-host",
    ts: null,
    received_at: "2026-05-15T00:00:00Z",
  };
}

describe("isInstallComplete", () => {
  test("false when no events", () => {
    expect(isInstallComplete([])).toBe(false);
  });

  test("false when print_attach hasn't landed yet", () => {
    expect(
      isInstallComplete([
        ev("python_venv", "done"),
        ev("docker_compose_up", "done"),
        ev("spawn_operator", "done"),
      ]),
    ).toBe(false);
  });

  test("false when print_attach is in-progress", () => {
    expect(
      isInstallComplete([
        ev("spawn_operator", "done"),
        ev("print_attach", "begin"),
      ]),
    ).toBe(false);
  });

  test("true when print_attach=done lands", () => {
    expect(
      isInstallComplete([
        ev("spawn_operator", "done"),
        ev("print_attach", "begin"),
        ev("print_attach", "done"),
      ]),
    ).toBe(true);
  });

  test("false when print_attach later transitions to failed (last-write-wins)", () => {
    // Defensive — print_attach is the terminal step so failed→done
    // sequencing shouldn't happen in practice, but the helper must be
    // monotonic-safe so a flaky retry can't strand the user mid-handoff.
    expect(
      isInstallComplete([
        ev("print_attach", "done"),
        ev("print_attach", "failed"),
      ]),
    ).toBe(false);
  });

  test("ignores other checkpoints' status", () => {
    expect(
      isInstallComplete([
        ev("apt_update", "failed"),
        ev("print_attach", "done"),
      ]),
    ).toBe(true);
  });
});

describe("welcomeUrlFor", () => {
  test("appends /welcome to a bare base_url", () => {
    expect(welcomeUrlFor("https://srv.example.com")).toBe(
      "https://srv.example.com/welcome",
    );
  });

  test("strips a single trailing slash before appending", () => {
    expect(welcomeUrlFor("https://srv.example.com/")).toBe(
      "https://srv.example.com/welcome",
    );
  });

  test("preserves path prefixes", () => {
    // The mothership doesn't currently allow paths in base_url, but the
    // helper shouldn't lose them if T-0024's schema relaxes later.
    expect(welcomeUrlFor("https://srv.example.com/sub")).toBe(
      "https://srv.example.com/sub/welcome",
    );
  });

  test("preserves path prefix with trailing slash", () => {
    expect(welcomeUrlFor("https://srv.example.com/sub/")).toBe(
      "https://srv.example.com/sub/welcome",
    );
  });
});
