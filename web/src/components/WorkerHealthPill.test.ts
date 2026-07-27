import { describe, test, expect } from "vitest";
import { workerHealthView } from "./WorkerHealthPill";

describe("workerHealthView (T-0456 failure-only)", () => {
  test("healthy → null (no pill, no green noise)", () => {
    expect(workerHealthView(null)).toBeNull();
    expect(workerHealthView({})).toBeNull();
    expect(workerHealthView({ worker: { alive: true } })).toBeNull();
    expect(workerHealthView({ worker: { health: [] } })).toBeNull();
  });

  test("dead_heartbeat → 'worker down'", () => {
    const v = workerHealthView({ worker: { health: ["dead_heartbeat"] } });
    expect(v?.label).toBe("worker down");
    expect(v?.title).toContain("dead_heartbeat");
  });

  test("sha_drift only → 'worker stale'", () => {
    const v = workerHealthView({ worker: { health: ["sha_drift"] } });
    expect(v?.label).toBe("worker stale");
    expect(v?.title).toContain("sha_drift");
  });

  test("both flags → 'worker down' leads, title names both", () => {
    const v = workerHealthView({ worker: { health: ["sha_drift", "dead_heartbeat"] } });
    expect(v?.label).toBe("worker down");
    expect(v?.title).toContain("sha_drift");
    expect(v?.title).toContain("dead_heartbeat");
  });

  test("unknown flag → still surfaces (forward-compat)", () => {
    const v = workerHealthView({ worker: { health: ["some_future_flag"] } });
    expect(v).not.toBeNull();
    expect(v?.title).toContain("some_future_flag");
  });

  test("dead_heartbeat / sha_drift stay RED", () => {
    expect(workerHealthView({ worker: { health: ["sha_drift"] } })?.danger).toBe(true);
    expect(workerHealthView({ worker: { health: ["dead_heartbeat"] } })?.danger).toBe(true);
  });
});

describe("workerHealthView — restart_pending (T-0739)", () => {
  const now = Date.now() / 1000;

  test("restart_pending alone → MUTED 'worker restarting', not the red alarm", () => {
    // The point of the ticket: after T-0717 this drift is the expected tail of
    // a restart that is already coming. Rendering it in the same red chip as a
    // genuinely stalled worker is how an alarm gets trained away.
    const v = workerHealthView({
      worker: {
        health: ["restart_pending"],
        restart: { state: "in_flight", since: now, expected_by: now + 170, overdue: false },
      },
    });
    expect(v?.danger).toBe(false);
    expect(v?.label).toBe("worker restarting");
    expect(v?.title).toContain("converges within ~");
  });

  test("the tooltip names WHICH self-healing shape and when it converges", () => {
    const v = workerHealthView({
      worker: {
        health: ["restart_pending"],
        restart: { state: "deferred", since: now, expected_by: now + 460, overdue: false },
      },
    });
    expect(v?.title).toContain("restart deferred");
  });

  test("restart_pending + another flag → RED wins", () => {
    // An unrecognised or severe problem must never be softened just because a
    // restart happens to be pending.
    for (const other of ["dead_heartbeat", "sha_drift", "some_future_flag"]) {
      const v = workerHealthView({
        worker: { health: ["restart_pending", other], restart: { state: "deferred" } },
      });
      expect(v?.danger).toBe(true);
      expect(v?.label).not.toBe("worker restarting");
    }
  });

  test("an OVERDUE restart is red AND says the restart never landed", () => {
    // Surfaced by the manual walkthrough: the first cut claimed "NO restart is
    // pending" here, which is both false and throws away the single most useful
    // line for whoever R-0005 pages.
    const v = workerHealthView({
      worker: {
        health: ["sha_drift"],
        restart: { state: "deferred", since: now - 900, expected_by: now - 1, overdue: true },
      },
    });
    expect(v?.danger).toBe(true);
    expect(v?.title).toContain("OVERDUE");
    expect(v?.title).toContain("did not land on its own");
  });

  test("a pre-T-0739 API (no restart key) is unchanged", () => {
    const v = workerHealthView({ worker: { health: ["sha_drift"] } });
    expect(v?.danger).toBe(true);
    expect(v?.label).toBe("worker stale");
    expect(v?.title).not.toContain("OVERDUE");
  });

  test("healthy still renders nothing, marker or not", () => {
    expect(workerHealthView({ worker: { restart: { state: "in_flight" } } })).toBeNull();
  });
});
