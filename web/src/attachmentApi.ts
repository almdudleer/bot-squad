/**
 * T-0061 — ATTACHMENT-scoped API surface (per-user-per-server).
 *
 * Lives separately from ``api.ts`` so the cross-server PER-ATTACHMENT
 * endpoints can grow without touching the GLOBAL api module (which is
 * itself in flight under Bundle E's T-0068 work — coordinated by SID
 * boundaries, not by import discipline).
 *
 * Endpoints:
 *   - GET    /api/me/attachments
 *   - GET    /api/me/attachment/<server_id>/tg-chat-id
 *   - PUT    /api/me/attachment/<server_id>/tg-chat-id
 *   - POST   /api/me/attachment/<server_id>/tg-chat-id/test
 *
 * The ``server_id == "self"`` sentinel resolves server-side to this
 * install's own server-id from the mothership registry; on a detach
 * build with no registry, the backend falls through to the legacy
 * UserMeta.tg_chat_id surface, so the same FE call works on both.
 */
type Json = Record<string, unknown> | unknown[];

async function call<T = Json>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (res.status === 401) {
    window.location.href = "/login";
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json() as Promise<T>;
}

export type AttachmentTgChatId = {
  server_id: string;
  tg_chat_id: string | null;
};

// T-0572 (Occam pass, D-0046): the /attachment/* pages were cut; the
// remaining consumers are the per-project PersonalNotificationPanel's
// save + test-ping calls. listMy/getTgChatId went with the pages.
export const attachmentApi = {
  putTgChatId: (serverId: string, tg_chat_id: string) =>
    call<AttachmentTgChatId>(
      `/api/me/attachment/${encodeURIComponent(serverId)}/tg-chat-id`,
      {
        method: "PUT",
        body: JSON.stringify({ tg_chat_id }),
      },
    ),

  testTgChatId: (serverId: string) =>
    call<{ ok: boolean; sent: boolean }>(
      `/api/me/attachment/${encodeURIComponent(serverId)}/tg-chat-id/test`,
      { method: "POST" },
    ),
};
