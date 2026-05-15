/**
 * T-0013: pure-helper tests for the post-install /welcome screen.
 *
 * The Welcome component is React; the *Next destination* and the
 * *operator-session normaliser* are pure exports from Welcome.tsx so
 * they can be unit-tested without jsdom (this repo runs vitest with no
 * DOM environment — see web/package.json + web/vite.config.ts).
 *
 * The component's network call is `api.welcomeOperator()`, a thin
 * wrapper around `fetch("/api/welcome/operator")`. The pickOperatorSession
 * normaliser tests cover everything the component would otherwise need to
 * mock fetch for.
 */
import { describe, expect, test } from "vitest";

import { WELCOME_NEXT_PATH, pickOperatorSession } from "./Welcome";

describe("Welcome page", () => {
  test("Next button routes to / (server-view Picker)", () => {
    // The DoD line 36 of T-0013 mandates the primary "Next" button
    // routes to `/`. This exported constant is the single source of
    // truth — if a future refactor moves the button elsewhere, this
    // test will be the first thing to fail.
    expect(WELCOME_NEXT_PATH).toBe("/");
  });
});

describe("pickOperatorSession", () => {
  test("returns the trimmed session name on a well-formed response", () => {
    expect(pickOperatorSession({ session: "bot-squad-operator" })).toBe(
      "bot-squad-operator",
    );
  });

  test("strips surrounding whitespace", () => {
    expect(pickOperatorSession({ session: "  ops-prime  " })).toBe("ops-prime");
  });

  test("returns null for empty / whitespace-only sessions", () => {
    expect(pickOperatorSession({ session: "" })).toBeNull();
    expect(pickOperatorSession({ session: "   " })).toBeNull();
  });

  test("returns null when session is missing or wrong type", () => {
    expect(pickOperatorSession(null)).toBeNull();
    expect(pickOperatorSession(undefined)).toBeNull();
    expect(pickOperatorSession({})).toBeNull();
    expect(pickOperatorSession({ session: 42 as unknown as string })).toBeNull();
  });
});
