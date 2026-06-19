/**
 * T-0218 — pure view-model for the 3-level personal notification panel.
 *
 * The backend (`GET /me/notifications/resolved`) owns the precedence
 * (project -> server -> global -> none); the FE NEVER recomputes it. This
 * helper only flattens the resolved payload into per-row display state
 * (raw value, explicit-override flag, winner flag) and a one-line effective
 * summary, so the panel component and its tests share one source of truth.
 */
import type { NotificationsResolved, NotificationLevelSource } from "../api";

export type NotificationLevelKey = "global" | "server" | "project";

export type LevelRowView = {
  key: NotificationLevelKey;
  /** Raw value at THIS level ("" when not set / inherited). */
  raw: string;
  /** This level carries an explicit override. */
  set: boolean;
  /** This level supplies the effective (winning) target. */
  isWinner: boolean;
};

export const LEVEL_ORDER: NotificationLevelKey[] = ["global", "server", "project"];

/** Per-row display state, in stable global→server→project order. */
export function levelRowViews(r: NotificationsResolved): LevelRowView[] {
  const winner = r.effective.source;
  return LEVEL_ORDER.map((key) => {
    const lvl = r.levels[key];
    return {
      key,
      raw: lvl.tg_chat_id ?? "",
      set: lvl.set,
      isWinner: winner === key,
    };
  });
}

const SOURCE_LABEL: Record<NotificationLevelSource, string> = {
  project: "project",
  server: "server",
  global: "global",
  none: "none",
};

/** One-line summary of which target actually pings, e.g.
 *  "Effective: 404580642 (from global)" or "Effective: none — no personal
 *  target set at any level". */
export function effectiveSummary(r: NotificationsResolved): string {
  const { tg_chat_id, source } = r.effective;
  if (source === "none" || !tg_chat_id) {
    return "Effective: none — no personal target set at any level";
  }
  return `Effective: ${tg_chat_id} (from ${SOURCE_LABEL[source]})`;
}
