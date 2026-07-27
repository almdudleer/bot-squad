#!/usr/bin/env node
/**
 * T-0757 — runner for the content-pane responsive check.
 *
 *   node web/qa/run-pane-overflow.mjs --base http://localhost:5199 \
 *        --slug bot-squad --user test --password test
 *
 * Drives a real browser because the assertion is a LAYOUT one: jsdom has no
 * layout engine, so `clientWidth`/`scrollWidth` are always 0 there and a unit
 * test cannot see this bug. The pure judgement (`findViolations`) IS unit
 * tested — see paneOverflow.test.mjs; this file only supplies it real numbers.
 *
 * Exit 0 = every pane healthy at every width. Exit 1 = at least one violation.
 * Exit 2 = could not run (no playwright, login failed).
 *
 * `playwright` is not a dependency of web/ — this is an on-demand check, not
 * part of `npm test`. Install it where you run it (`npm i -D playwright`) or
 * run it from a checkout that already has it.
 */
import process from "node:process";

import {
  CHECK_WIDTHS,
  DEFAULT_BUDGET,
  findViolations,
  measurePanes,
  PANE_TARGETS,
} from "./paneOverflow.mjs";

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  return i >= 0 && process.argv[i + 1] ? process.argv[i + 1] : fallback;
}

const base = arg("base", "http://localhost:5173").replace(/\/$/, "");
const slug = arg("slug", "bot-squad");
const user = arg("user", "test");
const password = arg("password", "test");

let chromium;
for (const mod of ["playwright", "@playwright/test"]) {
  try {
    ({ chromium } = await import(mod));
    break;
  } catch {
    /* try the next one */
  }
}
if (!chromium) {
  console.error(
    "pane-overflow: playwright is not installed here.\n" +
      "  npm i -D playwright && npx playwright install chromium",
  );
  process.exit(2);
}

const browser = await chromium.launch();
const context = await browser.newContext();
const page = await context.newPage();

// Log in through the API so the run needs no UI knowledge of the login form.
await page.goto(`${base}/login`);
const loginStatus = await page.evaluate(
  async ([u, p]) => {
    const r = await fetch("/api/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: u, password: p }),
    });
    return r.status;
  },
  [user, password],
);
if (loginStatus !== 200) {
  console.error(`pane-overflow: login failed (${loginStatus})`);
  await browser.close();
  process.exit(2);
}

let failures = 0;
for (const width of CHECK_WIDTHS) {
  await page.setViewportSize({ width, height: 900 });
  for (const target of PANE_TARGETS) {
    await page.goto(`${base}${target.path(slug)}`);
    await page.waitForSelector(target.paneSelector, { timeout: 10_000 });
    // The body arrives from a fetch; wait for it rather than racing the render.
    await page
      .waitForSelector(`${target.paneSelector} .mc-markdown`, { timeout: 10_000 })
      .catch(() => undefined);
    const m = await page.evaluate(measurePanes, target.paneSelector);
    const violations = findViolations(m, DEFAULT_BUDGET);
    const label = `${target.name} @ ${width}px`;
    if (violations.length === 0) {
      console.log(
        `PASS  ${label}  pane ${m.paneClientWidth}px, ` +
          `pane overflow ${m.paneScrollWidth - m.paneClientWidth}, ` +
          `page overflow ${m.pageOverflow}`,
      );
      continue;
    }
    failures += violations.length;
    console.log(`FAIL  ${label}`);
    for (const v of violations) console.log(`        ${v.kind}: ${v.detail}`);
  }
}

await browser.close();
console.log(failures === 0 ? "\npane-overflow: OK" : `\npane-overflow: ${failures} violation(s)`);
process.exit(failures === 0 ? 0 : 1);
