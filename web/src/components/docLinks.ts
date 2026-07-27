import { DocSummary } from "../api";

// T-0756: resolving a doc body's RELATIVE links.
//
// Docs are authored as markdown files in a git tree, so a cross-doc link is
// written the way markdown links are written everywhere else — a relative
// filename, `[chapter 1](01-sessions-task-manager.md)`. That is the correct
// thing for an author to write: it is what makes the doc readable in an editor
// and on GitHub.
//
// The app, however, routes docs by QUERY PARAM (`/p/<slug>/docs?doc=<id>`). A
// relative href resolves against the PATH and drops the param, so
// `01-sessions-task-manager.md` became `/p/bot-squad/01-sessions-task-manager.md`
// — which the router lands on `/p/bot-squad`, the project BOARD. No 404, no
// error: the reader is just quietly somewhere else. Measured on the live
// install: 12 of the 13 links in roadmap/README, plus 16 more refs across
// 06-notifications, 07-multi-server-mothership, provenance, verbatim-contract
// and phase2-remote-recon-report.
//
// This module fixes the RENDERER, not the content. Rewriting those links to
// absolute app paths would fix today and break tomorrow — it commits every
// future doc author to remembering an app-specific URL shape, and costs the
// editor/GitHub reading.
//
// == Why a LOOKUP and not a derivation ==
//
// Mapping `D-0057-t-0637-ui-declutter....md` to the doc id `D-0057` is
// `artifact_nesting.id_from_stem` (T-0751) — a python function this bundle
// cannot call. Re-implementing it here would create exactly the second copy
// T-0751 spent a whole ticket deleting. Instead the docs LIST endpoint now
// ships each doc's filename `stem` next to the `id` it derived from it, and
// this module does an exact lookup keyed on that stem. There is still one
// stem->id rule and it still lives in python; the web side only reads its
// output.

/** category/stem -> doc id, plus a stem-only index for same-directory links. */
export type DocIndex = {
  byPath: Map<string, string>;
  /** stem -> id, or `null` when the same stem exists in two categories. */
  byStem: Map<string, string | null>;
};

export const EMPTY_DOC_INDEX: DocIndex = { byPath: new Map(), byStem: new Map() };

export function buildDocIndex(docs: readonly DocSummary[]): DocIndex {
  const byPath = new Map<string, string>();
  const byStem = new Map<string, string | null>();
  for (const d of docs) {
    // Older API builds (and any caller that hand-rolls a summary) have no
    // `stem`. For every doc whose stem is not an allocated `D-NNNN-<slug>` the
    // id IS the stem, so the id is a safe fallback that keeps the common case
    // working against a not-yet-deployed backend.
    const stem = d.stem ?? d.id;
    byPath.set(`${d.category}/${stem}`, d.id);
    // Ambiguity is recorded, not resolved: two `README.md` in two categories
    // must not make a bare `README.md` link silently pick one of them.
    byStem.set(stem, byStem.has(stem) ? null : d.id);
  }
  return { byPath, byStem };
}

export type DocHrefKind =
  /** Not a relative in-tree reference — leave the anchor exactly as it was. */
  | { kind: "external" }
  /** Pure `#fragment` — an in-page anchor, left to the browser. */
  | { kind: "fragment" }
  /** Relative reference that resolves to a doc the API serves. */
  | { kind: "doc"; id: string; fragment: string }
  /** Relative reference with no doc behind it — must FAIL VISIBLY. */
  | { kind: "missing"; target: string };

/**
 * Classify a raw markdown href.
 *
 * `baseCategory` is the category dir of the doc being rendered, so `./x.md`
 * and `../design/x.md` resolve the way they do on disk. Without it a bare
 * filename still resolves through the stem index — that is the shape every
 * live cross-doc link uses today.
 */
export function classifyDocHref(
  href: string,
  index: DocIndex,
  baseCategory?: string,
): DocHrefKind {
  const raw = href.trim();
  if (!raw) return { kind: "external" };
  // Absolute app paths (`/p/<slug>/t/T-0244`), full URLs, protocol-relative
  // URLs and `mailto:` are already unambiguous and already work.
  if (raw.startsWith("/") || raw.startsWith("//") || /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
    return { kind: "external" };
  }
  if (raw.startsWith("#")) return { kind: "fragment" };

  const hashAt = raw.indexOf("#");
  const fragment = hashAt >= 0 ? raw.slice(hashAt) : "";
  let target = hashAt >= 0 ? raw.slice(0, hashAt) : raw;
  // A `?query` on a relative file href has no meaning in the docs tree.
  const qAt = target.indexOf("?");
  if (qAt >= 0) target = target.slice(0, qAt);
  target = safeDecode(target);
  if (!target) return { kind: "fragment" };

  const id = lookup(target, index, baseCategory);
  return id === null ? { kind: "missing", target: raw } : { kind: "doc", id, fragment };
}

function safeDecode(s: string): string {
  try {
    return decodeURIComponent(s);
  } catch {
    return s; // a stray `%` is not worth throwing over
  }
}

function lookup(target: string, index: DocIndex, baseCategory?: string): string | null {
  // Only `.md` targets can be docs. A relative link to anything else (a
  // directory such as `_corpus-parts/`, an image, a script) has no doc behind
  // it — and the whole point of this ticket is that such a link must say so
  // rather than land the reader on the board.
  if (!/\.md$/i.test(target)) return null;

  // An explicitly-relative target (`./x.md`, `../design/x.md`) is anchored on
  // the doc's own directory and nothing else. A bare one (`x.md`,
  // `roadmap/x.md`) is ambiguous between "my sibling" and "relative to docs/",
  // so both readings are tried — sibling first, because that is what an author
  // editing the file means.
  const explicitlyRelative = target.startsWith("./") || target.startsWith("../");
  const bases: (string | undefined)[] = explicitlyRelative
    ? [baseCategory]
    : baseCategory
      ? [baseCategory, undefined]
      : [undefined];

  for (const base of bases) {
    const segments = normalizeSegments(target, base);
    if (segments === null) continue;
    const stem = segments[segments.length - 1].replace(/\.md$/i, "");
    if (!stem) continue;
    if (segments.length === 2) {
      const hit = index.byPath.get(`${segments[0]}/${stem}`);
      if (hit) return hit;
    } else if (segments.length === 1) {
      // No category known — fall back to the tree-wide stem index, which
      // records ambiguity as null so a stem present in two categories does
      // NOT silently pick one.
      const hit = index.byStem.get(stem);
      if (hit) return hit;
    }
    // Docs live exactly one category dir deep (`artifact_nesting.iter_docs`
    // walks one level). A deeper path — `roadmap/gap-matrix/README.md` — names
    // a file the API does not serve, so it stays missing.
  }
  return null;
}

/**
 * Resolve `target` against `baseCategory`, returning the path segments below
 * `docs/`, or null if it climbs out of the docs tree.
 */
function normalizeSegments(target: string, baseCategory?: string): string[] | null {
  const out: string[] = baseCategory ? [baseCategory] : [];
  for (const p of target.split("/")) {
    if (p === "" || p === ".") continue;
    if (p === "..") {
      if (out.length === 0) return null; // escapes docs/ — not a doc
      out.pop();
      continue;
    }
    out.push(p);
  }
  return out.length === 0 ? null : out;
}

/** The in-app route for a resolved doc link. */
export function docRoute(slug: string, id: string, fragment = ""): string {
  return `/p/${slug}/docs?doc=${encodeURIComponent(id)}${fragment}`;
}
