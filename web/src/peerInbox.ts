/**
 * T-0127: pure helpers for the in-UI peer-reply inbox panel.
 *
 * The peer message bus is wired through the API (api.peerSend / peerInboxRead /
 * peerInboxWait). `peerInboxRead` DRAINS — each call returns only lines appended
 * since the last read — so the UI must persist what it has already seen. These
 * helpers own the parse + dedupe + persist logic so the React panel
 * (components/PeerInbox.tsx) stays thin and this stays unit-testable.
 *
 * Wire format of one inbox line (TAB-separated, three fields):
 *   "<ISO-ts>\t[from <SID>]\t<body>"
 * The body may itself contain tabs, so we split into AT MOST 3 fields.
 */

export type StoredMsg = {
  id: string;
  ts: string;
  from: string;
  body: string;
  read: boolean;
};

/** Single source of truth for the logged-in user's stable UI SID. */
export function uiSidFor(username: string): string {
  return `S-${username}-ui-p0`;
}

/**
 * Parse one raw inbox line into {ts, from, body}, or null for a blank/garbage
 * line. Tolerance rule: a line is acceptable as long as it has a non-blank
 * first field (the timestamp); the `from` tag and body are optional. The body
 * is allowed to contain tabs — we only split off the first two fields and join
 * the remainder back together.
 */
export function parseInboxLine(
  line: string,
): { ts: string; from: string; body: string } | null {
  if (line == null || line.trim() === "") return null;
  const parts = line.split("\t");
  const ts = parts[0] ?? "";
  if (ts.trim() === "") return null;
  const fromTag = parts[1] ?? "";
  const body = parts.slice(2).join("\t");
  // fromTag is "[from S-...]"; strip the wrapper to get the bare SID. If it
  // doesn't match that shape, fall back to the raw tag.
  let from = fromTag;
  const m = /^\[from (.+)\]$/.exec(fromTag);
  if (m) from = m[1];
  return { ts, from, body };
}

/** Stable dedupe key for a parsed message. */
export function messageId(m: { ts: string; from: string; body: string }): string {
  return `${m.ts}|${m.from}|${m.body}`;
}

/**
 * Merge freshly-drained inbox lines into the existing stored list.
 *
 * - Parses each incoming line, dropping nulls.
 * - Dedupes by id against the existing list AND within the incoming batch.
 * - Appends genuinely-new messages (chronological, oldest→newest) as read:false.
 * - Preserves the `read` flag of any message that already exists.
 * - Caps the result to the newest `cap` messages (default 500).
 */
export function mergeMessages(
  existing: StoredMsg[],
  incomingLines: string[],
  cap = 500,
): StoredMsg[] {
  const seen = new Set(existing.map((m) => m.id));
  const out = existing.slice();
  for (const line of incomingLines) {
    const parsed = parseInboxLine(line);
    if (!parsed) continue;
    const id = messageId(parsed);
    if (seen.has(id)) continue; // already stored OR earlier in this batch
    seen.add(id);
    out.push({ id, ts: parsed.ts, from: parsed.from, body: parsed.body, read: false });
  }
  if (out.length > cap) return out.slice(out.length - cap);
  return out;
}

function storageKey(slug: string, sid: string): string {
  return `peerInbox:v1:${slug}:${sid}`;
}

/** Load persisted messages for (slug, sid); returns [] on any failure. */
export function loadStored(slug: string, sid: string): StoredMsg[] {
  try {
    const raw = localStorage.getItem(storageKey(slug, sid));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed as StoredMsg[];
  } catch {
    return [];
  }
}

/** Persist messages for (slug, sid); swallows any failure. */
export function saveStored(slug: string, sid: string, msgs: StoredMsg[]): void {
  try {
    localStorage.setItem(storageKey(slug, sid), JSON.stringify(msgs));
  } catch {
    // localStorage may be absent (tests) or quota-full — best effort only.
  }
}
