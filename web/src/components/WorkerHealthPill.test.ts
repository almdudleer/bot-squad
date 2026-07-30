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

describe("workerHealthView — deploy_pending (T-0754)", () => {
  const now = Date.now() / 1000;
  const deploy = {
    state: "in_flight" as const,
    slug: "bot-squad",
    queue_id: "q1",
    target_sha: "75dc01ac24554b4aa3a4e49910e669eaf75bc76d",
    converged: "worker" as const,
    since: now - 65,
    expected_by: now + 240,
    overdue: false,
  };

  test("deploy_pending alone → MUTED 'deploy landing', not the red alarm", () => {
    // The whole harm this ticket records is a HUMAN reading an alarming chip on
    // a deploy where nothing is wrong — measured at 62.3s on the 75dc01a
    // deploy. The pill is where that harm lands, so the fix has to reach it.
    const v = workerHealthView({ worker: { health: ["deploy_pending"], deploy } });
    expect(v?.danger).toBe(false);
    expect(v?.label).toBe("deploy landing");
  });

  test("the tooltip names the commit and WHICH side is already on it", () => {
    const v = workerHealthView({ worker: { health: ["deploy_pending"], deploy } });
    expect(v?.title).toContain("75dc01a");
    expect(v?.title).toContain("the worker is already on it");
  });

  test("the converged side can be the API — p343's correction to the model", () => {
    const v = workerHealthView({
      worker: { health: ["deploy_pending"], deploy: { ...deploy, converged: "api" } },
    });
    expect(v?.title).toContain("the API is already on it");
  });

  test("deploy_pending + another flag → RED wins", () => {
    for (const other of ["dead_heartbeat", "sha_drift", "some_future_flag"]) {
      const v = workerHealthView({ worker: { health: ["deploy_pending", other], deploy } });
      expect(v?.danger).toBe(true);
      expect(v?.label).not.toBe("deploy landing");
    }
  });

  test("an OVERDUE deploy is red AND says it never converged", () => {
    // The bound. Past its deadline the job no longer excuses the drift — but it
    // is still the headline for whoever gets paged.
    const v = workerHealthView({
      worker: {
        health: ["sha_drift"],
        deploy: { ...deploy, since: now - 1200, expected_by: now - 1, overdue: true },
      },
    });
    expect(v?.danger).toBe(true);
    expect(v?.title).toContain("OVERDUE");
    expect(v?.title).toContain("has not converged");
  });

  test("both explanations can ride one sha_drift without colliding", () => {
    const v = workerHealthView({
      worker: {
        health: ["sha_drift"],
        restart: { state: "deferred", since: now - 900, expected_by: now - 1, overdue: true },
        deploy: { ...deploy, overdue: true, expected_by: now - 1 },
      },
    });
    expect(v?.title).toContain("did not land on its own");
    expect(v?.title).toContain("has not converged");
  });

  test("a pre-T-0754 API (no deploy key) is unchanged", () => {
    const v = workerHealthView({ worker: { health: ["sha_drift"] } });
    expect(v?.danger).toBe(true);
    expect(v?.label).toBe("worker stale");
    expect(v?.title).not.toContain("deploy of");
  });

  test("healthy still renders nothing mid-deploy", () => {
    expect(workerHealthView({ worker: { deploy } })).toBeNull();
  });
});

describe("T-0824 — the install tree term", () => {
  const now = Date.now() / 1000;

  test("worker_stale is RED and names what is actually wrong", () => {
    // Row 1, measured live 12:56Z: the old surface rendered NOTHING here.
    const v = workerHealthView({
      install: { git_sha: "e".repeat(40) },
      worker: { health: ["worker_stale"], git_sha: "3".repeat(40) },
    });
    expect(v?.danger).toBe(true);
    expect(v?.label).toBe("worker stale");
    expect(v?.title).toContain("NOT running the deployed code");
  });

  test("install_sha_unknown says CANNOT TELL, not 'stale' and not nothing", () => {
    // The flag's whole content is that the question is unanswerable. Rendering
    // it as staleness asserts what the payload just said it cannot establish;
    // rendering it as healthy recreates the indistinguishable pair.
    const v = workerHealthView({
      install: { git_sha: null, reason: "no_marker" },
      worker: { health: ["install_sha_unknown"] },
    });
    expect(v).not.toBeNull();
    expect(v?.danger).toBe(true);
    expect(v?.label).toBe("deployed sha unknown");
    expect(v?.title).toContain("unanswerable");
  });

  test("a pending restart mutes worker staleness, and says how long", () => {
    const v = workerHealthView({
      install: { git_sha: "e".repeat(40) },
      worker: {
        health: ["restart_pending"],
        restart: { state: "in_flight", since: now, expected_by: now + 30, overdue: false },
      },
    });
    expect(v?.danger).toBe(false);
    expect(v?.label).toBe("worker restarting");
  });

  test("an OVERDUE restart rides along on worker_stale — the headline", () => {
    // The T-0717 night's missing line, now attached to the flag that means the
    // worker is genuinely on stale code.
    const v = workerHealthView({
      install: { git_sha: "e".repeat(40) },
      worker: {
        health: ["worker_stale"],
        restart: { state: "deferred", since: now - 900, expected_by: now - 600, overdue: true },
      },
    });
    expect(v?.danger).toBe(true);
    expect(v?.title).toContain("OVERDUE");
    expect(v?.title).toContain("did not land on its own");
  });

  test("worker_stale and sha_drift are shown as the two facts they are", () => {
    const v = workerHealthView({
      install: { git_sha: "e".repeat(40) },
      worker: { health: ["worker_stale", "sha_drift"] },
    });
    expect(v?.danger).toBe(true);
    expect(v?.title).toContain("NOT running the deployed code");
    expect(v?.title).toContain("API container behind");
  });

  test("row 3 — all three converged — still renders nothing", () => {
    // The green control on this surface too: the fix must not turn a correct
    // install into a permanently lit chip.
    expect(
      workerHealthView({
        install: { git_sha: "e".repeat(40) },
        worker: { alive: true, git_sha: "e".repeat(40) },
      }),
    ).toBeNull();
  });
});
