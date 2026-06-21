/**
 * T-0349: the server-view landing (Picker) onboarding sequencing.
 *
 * Bug it fixes: the post-T-0336 landing stacked THREE onboarding popovers at
 * once (help, "other people's projects", "create your first project"), they
 * overlapped the project cards, and the create-prompt nagged an operator who is
 * actively running projects. `pickServerOnboardingBeat` encodes the two
 * invariants the single-brain landing must hold: at most one beat at a time,
 * and onboarding ONLY on a genuinely-empty install.
 */
import { describe, expect, test } from "vitest";

import { pickServerOnboardingBeat } from "./Picker";

describe("pickServerOnboardingBeat", () => {
  test("active operator WITH projects sees NO onboarding beat (foreground his cards)", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: false,
        isAdmin: true,
        helpVisible: true,
        createVisible: true,
      }),
    ).toBe(null);
  });

  test("non-admin with projects also sees nothing", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: false,
        isAdmin: false,
        helpVisible: true,
        createVisible: false,
      }),
    ).toBe(null);
  });

  test("empty install shows the help beat FIRST (one at a time)", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: true,
        isAdmin: true,
        helpVisible: true,
        createVisible: true,
      }),
    ).toBe("help");
  });

  test("empty install, help dismissed → create beat (admin only)", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: true,
        isAdmin: true,
        helpVisible: false,
        createVisible: true,
      }),
    ).toBe("create");
  });

  test("empty install, help dismissed, NON-admin → no create prompt", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: true,
        isAdmin: false,
        helpVisible: false,
        createVisible: true,
      }),
    ).toBe(null);
  });

  test("empty install, both beats already seen → nothing left", () => {
    expect(
      pickServerOnboardingBeat({
        isEmptyInstall: true,
        isAdmin: true,
        helpVisible: false,
        createVisible: false,
      }),
    ).toBe(null);
  });

  test("never returns two beats — help takes precedence over create", () => {
    const beat = pickServerOnboardingBeat({
      isEmptyInstall: true,
      isAdmin: true,
      helpVisible: true,
      createVisible: true,
    });
    // exactly one (or none) — the union type can't express two, but assert the
    // precedence explicitly so a future reorder doesn't silently surface both.
    expect(beat).toBe("help");
  });
});
