/**
 * T-0764 — compare the shape each `call<T>(path)` site ASSERTS against the
 * shape the API actually declares.
 *
 * A raw `call<T>()` is a TYPE ASSERTION, not a validation: TypeScript erases
 * it, so nothing anywhere compared the claimed shape to the served one. When
 * T-0601 wrapped `GET /api/projects/{slug}/sessions` in the `{sessions,
 * errors}` envelope, the mothership fleet busy-indicator's
 * `call<SessionRow[]>` kept compiling, kept type-checking, and threw
 * "not iterable" into its own catch on every tick — clean console, no failing
 * test, a dead feature on the stakeholder's install for as long as the
 * envelope existed (fixed in 75dc01a).
 *
 * This test closes that loop. Its input is `api/response_shapes.json`, which
 * `api/tests/test_response_shape_contract.py` generates from the FastAPI app
 * itself and which cannot go stale: the API-side test goes RED the moment a
 * response shape moves, and the same commit that refreshes the pin is the
 * commit this test then judges the callers against.
 *
 * ── WHAT THIS SEES ────────────────────────────────────────────────────────
 * The TOP-LEVEL KIND only: array vs object vs scalar. That is enough to
 * catch the exact defect above, and it is the entire net.
 *
 * ── WHAT THIS DOES NOT SEE — read before trusting a green ─────────────────
 *  1. FIELD-LEVEL DRIFT. 120 of the 150 pinned endpoints are `object(loose)`
 *     (FastAPI's rendering of a `-> dict` annotation: "object, anything
 *     goes"). For those, a caller asserting the wrong FIELDS — or the right
 *     fields under old names — passes here. That includes
 *     `/api/projects/{slug}/sessions`, the endpoint this ticket came from.
 *     It was catchable only because the caller said ARRAY.
 *  2. RESPONSE BODIES THE SCHEMA CALLS `any` (the mothership proxy
 *     catch-all, the installer scripts): nothing to compare against.
 *  3. CALL SITES WHOSE PATH IS NOT A LITERAL, and sites whose path matches
 *     no pinned endpoint. Both are COUNTED and the counts are asserted, so
 *     the blind spot cannot grow silently — but they are not checked.
 *  4. Anything reached with `fetch()` directly rather than through one of
 *     the generic helpers below.
 *
 * ── METHOD AWARENESS IS LOAD-BEARING ──────────────────────────────────────
 * The pin is keyed by METHOD + path. A checker that assumed GET for every
 * site reports four bogus "server says array, caller asserts object" hits
 * (/backlog, /docs, /users, /m/servers) — all POST/PATCH creates compared
 * against the GET schema for the same path. Measured during T-0764's
 * scoping; the four are pinned as a test below.
 *
 * Sources and the pin are loaded through Vite (`import.meta.glob` + a JSON
 * import) rather than `node:fs`, so this needs no `@types/node` and behaves
 * identically under `npm test` and `npm run build`. A missing pin is a
 * module-resolution failure — the file fails to collect, loudly, instead of
 * quietly checking nothing.
 */
import { describe, expect, test } from "vitest";

import pinDoc from "../../api/response_shapes.json";

// ---------------------------------------------------------------------------
// Inputs
// ---------------------------------------------------------------------------

type Pin = Record<string, string>;

const PIN = (pinDoc as { shapes: Pin }).shapes;

/** Every non-test source under web/src, as raw text. */
const RAW_SOURCES = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

const SOURCES: [string, string][] = Object.entries(RAW_SOURCES)
  .filter(([path]) => !/\.test\.tsx?$/.test(path)) // tests assert against fakes
  .map(([path, text]) => [path.replace(/^\.\//, ""), text]);

/** Top-level kind of a pinned descriptor, e.g. "200 array(object(loose))". */
function pinnedKind(descriptor: string): "array" | "object" | "any" | "none" | "scalar" {
  const body = descriptor.replace(/^\d{3} /, "");
  if (body.startsWith("array(")) return "array";
  if (body.startsWith("object(")) return "object";
  if (body.startsWith("ref(")) return body.includes("=array(") ? "array" : "object";
  if (body === "any") return "any";
  if (body.startsWith("no-body") || body.startsWith("non-json") || body === "no-2xx-response") {
    return "none";
  }
  return "scalar";
}

// ---------------------------------------------------------------------------
// Finding the generic call helpers
// ---------------------------------------------------------------------------

/**
 * Names of the generic "fetch JSON at `path`, assert it is T" helpers.
 *
 * Discovered structurally rather than hard-coded (and the discovered set is
 * asserted below) — a new client module adding its own wrapper must not slip
 * past unscanned, which would be this ticket's own defect one level up.
 */
const HELPER_DECL =
  /(?:async function (\w+)\s*<|const (\w+)\s*=\s*async\s*<)[^(]*\(\s*path: string,\s*init\?: RequestInit/g;

function declaredHelpers(): Map<string, string> {
  const found = new Map<string, string>();
  for (const [file, src] of SOURCES) {
    for (const m of src.matchAll(HELPER_DECL)) found.set(m[1] ?? m[2], file);
  }
  return found;
}

/**
 * `proxyCall` is excluded on purpose: it is called ONLY by `fwd`, and only
 * with an already-proxied URL built by `proxyUrl()`. Its callers' upstream
 * paths are what `fwd` sees, and those ARE scanned — the mothership proxy is
 * shape-transparent, so an upstream path resolves against the same pin as
 * the local client's.
 */
const HELPERS_NOT_CALLED_WITH_AN_UPSTREAM_PATH = new Set(["proxyCall"]);

// ---------------------------------------------------------------------------
// Scanning call sites
// ---------------------------------------------------------------------------

type Site = {
  file: string;
  line: number;
  asserted: string;
  path: string | null;
  method: string;
};

/** Read a balanced `<...>` starting at `i` (which points at the `<`). */
function readGenerics(src: string, i: number): { text: string; end: number } | null {
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === "<") depth++;
    else if (c === ">") {
      depth--;
      if (depth === 0) return { text: src.slice(i + 1, j), end: j + 1 };
    }
  }
  return null;
}

/** Read the balanced argument list starting at `i` (which points at `(`). */
function readArgs(src: string, i: number): { text: string; end: number } | null {
  let depth = 0;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === "(") depth++;
    else if (c === ")") {
      depth--;
      if (depth === 0) return { text: src.slice(i + 1, j), end: j + 1 };
    }
  }
  return null;
}

/** The first argument's literal text, or null when it is not a literal. */
function firstLiteral(args: string): string | null {
  const s = args.trimStart();
  const quote = s[0];
  if (quote !== '"' && quote !== "'" && quote !== "`") return null;
  let depth = 0;
  for (let j = 1; j < s.length; j++) {
    const c = s[j];
    if (c === "\\") {
      j++;
      continue;
    }
    if (quote === "`") {
      if (c === "{" && s[j - 1] === "$") depth++;
      else if (c === "}" && depth > 0) depth--;
    }
    if (c === quote && depth === 0) return s.slice(1, j);
  }
  return null;
}

/**
 * Normalise a source path literal to the pinned key's vocabulary: every
 * `${...}` interpolation becomes `{}`, and any query string is dropped.
 */
function normalisePath(literal: string): string | null {
  let out = "";
  for (let i = 0; i < literal.length; i++) {
    if (literal[i] === "$" && literal[i + 1] === "{") {
      let depth = 1;
      let j = i + 2;
      for (; j < literal.length && depth > 0; j++) {
        if (literal[j] === "{") depth++;
        else if (literal[j] === "}") depth--;
      }
      out += "{}";
      i = j - 1;
      continue;
    }
    out += literal[i];
  }
  const q = out.indexOf("?");
  if (q >= 0) out = out.slice(0, q);
  if (!out.startsWith("/api/") && !out.startsWith("/i/")) return null;
  return out;
}

/** The HTTP verb from the init object literal, defaulting to GET. */
function methodOf(args: string): string {
  const m = args.match(/method:\s*"(\w+)"/);
  return (m ? m[1] : "GET").toUpperCase();
}

function scanSites(helpers: Set<string>): Site[] {
  const sites: Site[] = [];
  const namePattern = new RegExp(`\\b(${[...helpers].join("|")})\\s*<`, "g");
  for (const [file, src] of SOURCES) {
    for (const m of src.matchAll(namePattern)) {
      const ltIndex = m.index! + m[0].length - 1;
      const generics = readGenerics(src, ltIndex);
      if (!generics) continue;
      // A declaration (`async function call<T = Json>(path: string, …`) has
      // its parameter list right here too — tell it apart by its first arg.
      if (!src.slice(generics.end).startsWith("(")) continue;
      const args = readArgs(src, generics.end);
      if (!args) continue;
      if (/^\s*path\s*:/.test(args.text)) continue;
      const literal = firstLiteral(args.text);
      sites.push({
        file,
        line: src.slice(0, m.index!).split("\n").length,
        asserted: generics.text.trim(),
        path: literal === null ? null : normalisePath(literal),
        method: methodOf(args.text),
      });
    }
  }
  return sites;
}

// ---------------------------------------------------------------------------
// Classifying the asserted TypeScript type
// ---------------------------------------------------------------------------

/** Type names a caller uses to say "I am NOT asserting a shape". */
const OPT_OUT = new Set(["unknown", "any", "Json", "T"]);

type Kind = "array" | "object" | "scalar";

function assertedKindRaw(t: string, aliases: Map<string, Kind>): Kind {
  const s = t.trim();
  if (s.endsWith("[]") || /^Array\s*</.test(s) || s.startsWith("[")) return "array";
  if (s.startsWith("{")) return "object";
  if (/^(string|number|boolean|null)\b/.test(s)) return "scalar";
  const named = s.match(/^(\w+)$/);
  if (named && aliases.has(named[1])) return aliases.get(named[1])!;
  return "object";
}

/**
 * `type X = Y[]` must classify as an array. Without this, `call<Rows>` where
 * `type Rows = Row[]` would read as an object and manufacture a false
 * mismatch — the same class of error as ignoring the HTTP verb.
 */
function aliasKinds(): Map<string, Kind> {
  const kinds = new Map<string, Kind>();
  for (const [, src] of SOURCES) {
    for (const m of src.matchAll(/^\s*(?:export )?type (\w+)\s*=\s*([\s\S]{0,80}?)[;{]/gm)) {
      const [, name, rhs] = m;
      const head = rhs.trim();
      kinds.set(name, head ? assertedKindRaw(head, kinds) : "object");
    }
  }
  return kinds;
}

// ---------------------------------------------------------------------------
// Matching a caller path to a pinned endpoint
// ---------------------------------------------------------------------------

function segments(p: string): string[] {
  return p.split("/").filter((s) => s.length > 0);
}

/** A pinned `{param}` segment matches any caller segment; literals must agree. */
function pinMatches(pinPath: string, callerPath: string): "exact" | "param" | null {
  const a = segments(pinPath);
  const b = segments(callerPath);
  if (a.length !== b.length) return null;
  let exact = true;
  for (let i = 0; i < a.length; i++) {
    if (a[i] === b[i]) continue;
    const pinIsParam = a[i].startsWith("{") && a[i].endsWith("}");
    const callerIsParam = b[i] === "{}";
    if (pinIsParam && (callerIsParam || !b[i].includes("{"))) {
      exact = false;
      continue;
    }
    return null;
  }
  return exact ? "exact" : "param";
}

function resolveExact(
  pin: Pin,
  method: string,
  callerPath: string,
): { key: string; descriptor: string } | null {
  const candidates: { key: string; rank: number }[] = [];
  for (const key of Object.keys(pin)) {
    const [m, p] = key.split(" ");
    if (m !== method) continue;
    const kind = pinMatches(p, callerPath);
    if (kind) candidates.push({ key, rank: kind === "exact" ? 0 : 1 });
  }
  if (candidates.length === 0) return null;
  candidates.sort((x, y) => x.rank - y.rank);
  return { key: candidates[0].key, descriptor: pin[candidates[0].key] };
}

function resolve(pin: Pin, method: string, callerPath: string) {
  const direct = resolveExact(pin, method, callerPath);
  if (direct) return direct;
  // Two call sites append an interpolation that is a QUERY STRING, not a
  // path segment (`${category ? "?category=…" : ""}`), so normalisation
  // leaves a mixed `docs{}` segment. Query strings are not part of a pinned
  // key, so retry once with that tail dropped. Done here and named, rather
  // than folded into normalisePath, because it is a GUESS about what an
  // interpolation contains and a reader should see it as one.
  const trimmed = callerPath.replace(/\{\}$/, "");
  if (trimmed !== callerPath && !trimmed.endsWith("/")) {
    return resolveExact(pin, method, trimmed);
  }
  return null;
}

// ---------------------------------------------------------------------------
// The scan, run once
// ---------------------------------------------------------------------------

const HELPERS = declaredHelpers();
const SCANNED = new Set(
  [...HELPERS.keys()].filter((h) => !HELPERS_NOT_CALLED_WITH_AN_UPSTREAM_PATH.has(h)),
);
const ALIASES = aliasKinds();
const SITES = scanSites(SCANNED);

type Verdict =
  | {
      site: Site;
      kind: "checked";
      pinKey: string;
      assertedKind: Kind;
      servedKind: string;
      agree: boolean;
    }
  | { site: Site; kind: "opt-out" | "path-not-literal" | "no-such-endpoint" | "shape-not-declared" };

const VERDICTS: Verdict[] = SITES.map((site): Verdict => {
  if (site.path === null) return { site, kind: "path-not-literal" };
  const hit = resolve(PIN, site.method, site.path);
  if (!hit) return { site, kind: "no-such-endpoint" };
  const servedKind = pinnedKind(hit.descriptor);
  if (servedKind === "any" || servedKind === "none") return { site, kind: "shape-not-declared" };
  if (OPT_OUT.has(site.asserted)) return { site, kind: "opt-out" };
  const assertedKind = assertedKindRaw(site.asserted, ALIASES);
  return {
    site,
    kind: "checked",
    pinKey: hit.key,
    assertedKind,
    servedKind,
    agree: assertedKind === servedKind,
  };
});

const census = VERDICTS.reduce<Record<string, number>>(
  (acc, v) => ({ ...acc, [v.kind]: (acc[v.kind] ?? 0) + 1 }),
  {},
);

/**
 * The scan's census, asserted so the UNCHECKED share cannot grow quietly.
 *
 *  checked            — asserted kind compared against the pinned kind.
 *  opt-out            — the caller asserts `unknown` and normalises the
 *                       payload itself (all five today: the two T-0601
 *                       envelope readers on each client, plus autoupdate
 *                       status). Nothing to compare — the normaliser is what
 *                       makes them safe, not this test.
 *  path-not-literal   — the path argument is not a string/template literal.
 *  no-such-endpoint   — the path matches no pinned endpoint. Either a dead
 *                       call or a gap in this scanner; both need a human.
 *  shape-not-declared — the pin says `any` / no JSON body.
 *
 * Zero in three of the five buckets is the claim worth defending: every
 * call site in web/src today has a literal path that resolves to a pinned
 * endpoint. Update these numbers deliberately, and prefer shrinking the
 * unchecked ones.
 */
const EXPECTED_CENSUS: Record<string, number> = {
  checked: 117,
  "opt-out": 5,
};

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("api shape contract (T-0764)", () => {
  test("no call site asserts a top-level kind the API does not serve", () => {
    const bad = VERDICTS.filter((v) => v.kind === "checked" && !v.agree) as Extract<
      Verdict,
      { kind: "checked" }
    >[];
    const report = bad
      .map(
        (v) =>
          `  ${v.site.file}:${v.site.line}\n` +
          `      asserts ${v.assertedKind}  (call<${v.site.asserted}>)\n` +
          `      ${v.pinKey} serves ${v.servedKind}  (${PIN[v.pinKey]})`,
      )
      .join("\n");
    expect(
      bad.length,
      `call sites asserting a shape the API does not serve:\n${report}\n\n` +
        "TypeScript erases these assertions, so the caller compiles and fails only\n" +
        "at runtime, inside its nearest catch. Route through a client method that\n" +
        "normalises the payload, or assert the shape the pin names.",
    ).toBe(0);
  });

  test("every generic call helper in web/src is scanned", () => {
    // The scanner is regex-driven; a new `fetchJson`-style wrapper it does
    // not know about would make it silently check fewer sites. Pinning the
    // DISCOVERED set (rather than hard-coding one) is what makes that
    // visible — `onboarding/client.ts`'s `fetchJson` was found this way.
    expect([...HELPERS.keys()].sort()).toEqual(["call", "fetchJson", "fwd", "proxyCall"]);
    expect([...SCANNED].sort()).toEqual(["call", "fetchJson", "fwd"]);
  });

  test("the scan reaches the sites it is supposed to reach", () => {
    // A well-formed EMPTY scan reports zero mismatches too. This is the
    // positive control: if a refactor moves the client surface out from
    // under the scanner, this goes red rather than the suite going quietly
    // vacuous.
    expect(SITES.length).toBeGreaterThanOrEqual(110);
    expect(SOURCES.length).toBeGreaterThanOrEqual(50);
    expect(VERDICTS.filter((v) => v.kind === "checked").length).toBeGreaterThanOrEqual(100);
  });

  test("the blind spots are counted, not hidden", () => {
    expect(census).toEqual(EXPECTED_CENSUS);
  });

  test("method awareness is what keeps four false positives out", () => {
    // T-0764's scoping run assumed GET for every site and surfaced four
    // bogus "server says array, caller asserts object" hits — POST/PATCH
    // creates compared against the GET schema for the same path. Asserted
    // here so a refactor that drops the verb reintroduces them visibly.
    const pairs: [string, string][] = [
      ["/api/projects/{}/backlog", "POST"],
      ["/api/projects/{}/docs", "POST"],
      ["/api/users", "POST"],
      ["/api/m/servers", "POST"],
    ];
    for (const [path, method] of pairs) {
      const asGet = resolve(PIN, "GET", path);
      const asWritten = resolve(PIN, method, path);
      expect(asGet, `${path} has no GET pin`).not.toBeNull();
      expect(asWritten, `${path} has no ${method} pin`).not.toBeNull();
      expect(pinnedKind(asGet!.descriptor), `GET ${path}`).toBe("array");
      expect(pinnedKind(asWritten!.descriptor), `${method} ${path}`).toBe("object");
    }
  });

  test("the pre-75dc01a busy-indicator assertion is the shape this catches", () => {
    // The literal line that shipped the dead feature:
    //   apiFor(serverId).call<SessionRow[]>(`/api/projects/${slug}/sessions`)
    // Walked RED end-to-end against a copy of the tree with 75dc01a^'s
    // globalBusyMothership.ts restored — the suite named the file, the line,
    // and both kinds. This keeps the ingredients asserted in-repo.
    const hit = resolve(PIN, "GET", "/api/projects/{}/sessions");
    expect(hit).not.toBeNull();
    expect(pinnedKind(hit!.descriptor)).toBe("object");
    expect(assertedKindRaw("SessionRow[]", ALIASES)).toBe("array");
    expect(assertedKindRaw("SessionRow[]", ALIASES)).not.toBe(pinnedKind(hit!.descriptor));
    // ...and the assertion that replaced it opts out, so this check is not
    // simply hostile to that one endpoint.
    expect(OPT_OUT.has("unknown")).toBe(true);
  });

  test("a named type alias to an array is classified as an array", () => {
    const aliases = new Map<string, Kind>([
      ["Rows", "array"],
      ["Row", "object"],
    ]);
    expect(assertedKindRaw("Rows", aliases)).toBe("array");
    expect(assertedKindRaw("Row", aliases)).toBe("object");
    expect(assertedKindRaw("Row[]", aliases)).toBe("array");
    expect(assertedKindRaw("{ ok: boolean }", aliases)).toBe("object");
    expect(assertedKindRaw("string[]", aliases)).toBe("array");
    // The real alias table must have found the array aliases in api.ts.
    expect(ALIASES.size).toBeGreaterThan(10);
  });

  test("the pin is real input, not an optional extra", () => {
    // The pin is a static import: if `api/response_shapes.json` disappears,
    // this file fails to COLLECT (verified — vitest reports "no tests" and a
    // failed file). There is deliberately no skip-if-missing path, because
    // that would make the whole file report success while checking nothing.
    expect(Object.keys(PIN).length).toBeGreaterThan(100);
    expect(PIN["GET /api/projects/{slug}/sessions"]).toBe("200 object(loose)");
  });
});
