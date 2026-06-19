import { describe, expect, test } from "vitest";

import {
  SELF_SERVER_ID,
  SERVER_PICKER_STORAGE_KEY,
  isSuperAdminFromMe,
  readAttachmentServerId,
  resolveInitialPickedServer,
  type PickerCandidate,
} from "./sidebarHelpers";

describe("isSuperAdminFromMe (T-0062 with T-0066 fallback)", () => {
  test("explicit is_super_admin=true wins regardless of is_admin", () => {
    expect(isSuperAdminFromMe({ is_super_admin: true, is_admin: false })).toBe(true);
  });

  test("explicit is_super_admin=false wins regardless of is_admin", () => {
    // Once T-0066 ships, a server-local admin who isn't the mothership owner
    // returns false here — they should NOT see the MOTHERSHIP section.
    expect(isSuperAdminFromMe({ is_super_admin: false, is_admin: true })).toBe(false);
  });

  test("falls back to is_admin when is_super_admin is absent (pre-T-0066)", () => {
    expect(isSuperAdminFromMe({ is_admin: true })).toBe(true);
    expect(isSuperAdminFromMe({ is_admin: false })).toBe(false);
  });

  test("null / undefined me → false (anonymous can't be super-admin)", () => {
    expect(isSuperAdminFromMe(null)).toBe(false);
    expect(isSuperAdminFromMe(undefined)).toBe(false);
  });
});

describe("readAttachmentServerId (T-0061)", () => {
  function fakeStorage(value: string | null): Pick<Storage, "getItem"> {
    return { getItem: (k) => (k === SERVER_PICKER_STORAGE_KEY ? value : null) };
  }

  test("returns the SELF sentinel when no picker selection is stored", () => {
    expect(readAttachmentServerId(fakeStorage(null))).toBe(SELF_SERVER_ID);
  });

  test("returns the SELF sentinel for an empty stored value", () => {
    expect(readAttachmentServerId(fakeStorage(""))).toBe(SELF_SERVER_ID);
  });

  test("returns the stored picker selection when present", () => {
    expect(readAttachmentServerId(fakeStorage("srv_abc123"))).toBe("srv_abc123");
  });

  test("returns the SELF sentinel when no storage exists at all (node env)", () => {
    expect(readAttachmentServerId(null)).toBe(SELF_SERVER_ID);
  });
});

describe("resolveInitialPickedServer", () => {
  function attached(...ids: string[]): PickerCandidate[] {
    return ids.map((id, i) => ({ id, isSelf: i === 0 }));
  }

  test("respects a valid stored selection", () => {
    expect(resolveInitialPickedServer("b", null, attached("a", "b", "c"))).toBe("b");
  });

  test("drops a stale stored selection (server no longer attached)", () => {
    // If a server is detached after the user picked it, we don't want to
    // silently load against a missing id — fall through to current/self.
    expect(resolveInitialPickedServer("gone", null, attached("a", "b"))).toBe("a");
  });

  test("uses currentId when no stored selection", () => {
    expect(resolveInitialPickedServer(null, "c", attached("a", "b", "c"))).toBe("c");
  });

  test("ignores currentId that isn't in attached set", () => {
    expect(resolveInitialPickedServer(null, "unknown", attached("a", "b"))).toBe("a");
  });

  test("prefers the self server when no other signal", () => {
    expect(resolveInitialPickedServer(null, null, attached("self", "peer"))).toBe(
      "self",
    );
  });

  test("falls back to first attached when no self entry exists", () => {
    const ids: PickerCandidate[] = [{ id: "a" }, { id: "b" }];
    expect(resolveInitialPickedServer(null, null, ids)).toBe("a");
  });

  test("returns null when nothing is attached", () => {
    expect(resolveInitialPickedServer(null, null, [])).toBeNull();
    expect(resolveInitialPickedServer("anything", "anything", [])).toBeNull();
  });
});
