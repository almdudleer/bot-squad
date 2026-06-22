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
});
