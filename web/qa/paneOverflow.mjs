/**
 * T-0757 — the CONTENT-PANE responsive check.
 *
 * == Why this exists (read this before changing it) ==
 *
 * T-0757 was a docs layout whose `d-flex gap-4` row never stacked: at 390px the
 * navigation rail kept 264 of 366 available px and the CONTENT pane — the point
 * of the page — got 102. The h1 wrapped one word per line and a 352px table
 * overhung the pane by 250px.
 *
 * A page-overflow check had been running against that page all night and PASSED
 * CLEANLY, because it was true: `document.body.scrollWidth` was exactly 390 and
 * page overflow was 0. The damage was entirely INSIDE the flex row — a child
 * overflowing its parent while the page stays honest. Any responsive check that
 * only asks "does the body scroll sideways" is blind to that whole class, and
 * the class is large: every two-column layout in the app can produce it.
 *
 * So this check asks two questions the page-level one cannot, and BOTH are
 * required:
 *
 *   1. STARVED — is the content pane wide enough to be read at all?
 *      This is the one that catches T-0757. A starved pane can be perfectly
 *      well-behaved about overflow and still be useless.
 *
 *   2. ESCAPING — does anything inside the pane stick out past it?
 *      A wide table/code block/diagram must scroll INSIDE its own container.
 *
 * Question 2 alone is not enough, and the project board proves it: its row DOES
 * have `overflow-x: auto`, so it passes the containment test — but its columns
 * get 36-54px for content needing 96-198px, so scrolling only reveals more
 * equally-starved columns. A scroll container on a starved pane is the board's
 * bug, not a fix. Question 1 alone is not enough either: a roomy pane can still
 * let a 900px table overhang it.
 *
 * Descendants of a scroll container are NOT escapes. A 839px `<pre>` inside a
 * 366px pane is CORRECT when the `<pre>` scrolls — its `<code>` child extends
 * to 851px by design. Charging that as a violation would push the next fix
 * toward truncating content instead of containing it.
 *
 * This module is deliberately dependency-free and browser-agnostic:
 * `measurePanes` is serialized into a page by the playwright runner
 * (`run-pane-overflow.mjs`), and `findViolations` is a pure function the vitest
 * suite exercises against the real T-0757 numbers.
 */

/** Default budget. A phone-readable pane at 390px viewport is ~366px; 320 is
 *  the floor below which prose stops being prose (T-0757 measured 102). */
export const DEFAULT_BUDGET = {
  minPaneWidthPx: 320,
  /** Sub-pixel layout rounding; anything above this is a real overhang. */
  tolerancePx: 1,
};

/**
 * Browser-side probe. Serialized into the page — must stay self-contained
 * (no imports, no closure over module scope).
 *
 * @param {string} paneSelector CSS selector for the pane that holds the CONTENT
 * @returns {object} a measurement `findViolations` can judge
 */
export function measurePanes(paneSelector) {
  const pane = document.querySelector(paneSelector);
  if (!pane) {
    return { paneSelector, found: false, viewportWidth: window.innerWidth };
  }
  const paneBox = pane.getBoundingClientRect();
  // The pane's own content edge — padding excluded, because a child overhanging
  // into the padding box is already outside where content may go.
  const style = window.getComputedStyle(pane);
  const padRight = parseFloat(style.paddingRight) || 0;
  const contentRight = paneBox.right - padRight;

  const scrolls = (el) => {
    const s = window.getComputedStyle(el);
    return s.overflowX === "auto" || s.overflowX === "scroll" || s.overflowX === "hidden";
  };

  const escapes = [];
  for (const el of pane.querySelectorAll("*")) {
    const right = el.getBoundingClientRect().right;
    if (right <= contentRight + 1) continue;
    // Inside a scroll container? Then this is scrolled content, not an escape.
    let contained = false;
    for (let p = el.parentElement; p && p !== pane; p = p.parentElement) {
      if (scrolls(p)) {
        contained = true;
        break;
      }
    }
    if (contained) continue;
    escapes.push({
      tag: el.tagName,
      className: String(el.className || "").slice(0, 40),
      right: Math.round(right),
      text: (el.textContent || "").trim().slice(0, 40),
    });
  }

  return {
    paneSelector,
    found: true,
    viewportWidth: window.innerWidth,
    // The page-level signal the OLD check looked at — captured so a report can
    // show it was clean while the pane was not.
    pageOverflow: document.documentElement.scrollWidth - window.innerWidth,
    bodyScrollWidth: document.body.scrollWidth,
    paneClientWidth: pane.clientWidth,
    paneScrollWidth: pane.scrollWidth,
    paneContentRight: Math.round(contentRight),
    escapes: escapes.slice(0, 10),
    escapeCount: escapes.length,
  };
}

/**
 * Judge one measurement. Pure — this is what the unit tests drive.
 *
 * @param {object} m a `measurePanes` result
 * @param {object} [budget]
 * @returns {{kind: string, detail: string}[]} empty when the pane is healthy
 */
export function findViolations(m, budget = DEFAULT_BUDGET) {
  const out = [];
  if (!m || m.found === false) {
    return [{ kind: "pane-missing", detail: `no element matches ${m?.paneSelector}` }];
  }
  if (m.paneClientWidth < budget.minPaneWidthPx) {
    out.push({
      kind: "pane-starved",
      detail:
        `content pane is ${m.paneClientWidth}px at a ${m.viewportWidth}px viewport ` +
        `(floor ${budget.minPaneWidthPx}px) — the navigation chrome is taking the page`,
    });
  }
  const overflow = m.paneScrollWidth - m.paneClientWidth;
  if (overflow > budget.tolerancePx) {
    out.push({
      kind: "pane-overflow",
      detail:
        `content pane scrollWidth ${m.paneScrollWidth} vs clientWidth ${m.paneClientWidth} ` +
        `(+${overflow}px) while page overflow is ${m.pageOverflow}`,
    });
  }
  if (m.escapeCount > 0) {
    const first = m.escapes[0];
    out.push({
      kind: "child-escapes-pane",
      detail:
        `${m.escapeCount} element(s) extend past the pane's content edge ` +
        `(${m.paneContentRight}px); first: <${first.tag}> to ${first.right}px ` +
        `"${first.text}" — it needs its own scroll container`,
    });
  }
  return out;
}

/**
 * The surfaces this check covers, and the pane on each that holds the CONTENT
 * (not the navigation). Add a row when you add a two-column layout — that is
 * the whole maintenance cost of not shipping T-0757 again.
 */
export const PANE_TARGETS = [
  {
    name: "docs-detail",
    // 07-multi-server-mothership is the length worst case on the live install
    // (30k chars); roadmap/README is the TABLE worst case. Both are real docs,
    // not fixtures — T-0757 asked for exactly that.
    path: (slug) => `/p/${slug}/docs?doc=07-multi-server-mothership`,
    paneSelector: ".mc-docs-detail",
  },
  {
    name: "docs-detail-tables",
    path: (slug) => `/p/${slug}/docs?doc=README`,
    paneSelector: ".mc-docs-detail",
  },
];

/** Phone-first. 390 is the iPhone 14/15 logical width the ticket was filed at. */
export const CHECK_WIDTHS = [390, 414, 768];
