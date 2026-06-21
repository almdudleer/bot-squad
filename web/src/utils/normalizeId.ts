// T-0425 / T-0424 (shared normalize_id CONTRACT, authored by p67 on the Python
// side — this is the byte-for-byte TS mirror). Strip EXACTLY ONE trailing
// literal lowercase ".md" at id-comparison boundaries, so a bare stem and its
// ".md" filename form compare equal (e.g. "ui-polish" === "ui-polish.md").
//
// Rules (MUST match the Python normalize_id exactly):
//  - ends with ".md" → remove the last 3 chars; else unchanged.
//  - NOT greedy: "x.md.md" → "x.md".
//  - case-sensitive: only literal ".md" (".MD"/".Md" are NOT stripped).
//  - no trim, no other suffixes (callers pre-trim). Body preserved verbatim.
//  - apply at COMPARISON: normalizeId(a) === normalizeId(b).
// Edge cases: ""→""; ".md"→""; "README.MD"→"README.MD"; "x.md.md"→"x.md".
export function normalizeId(value: string | null | undefined): string {
  if (!value) return "";
  return value.endsWith(".md") ? value.slice(0, -3) : value;
}
