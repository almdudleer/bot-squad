/**
 * T-0068: bearer-missing error UX. The cross-server route's wrapper
 * classifies proxy errors into two flavours (not_connected vs.
 * access_denied) and renders distinct copy for each. The classification
 * itself is covered in `api.test.ts` (ProxyError instance + kind); here we
 * pin the *copy* surface so future tweaks don't accidentally let the two
 * states collapse into one.
 */
import { describe, expect, test } from "vitest";

import { peerAccessCopy } from "./MothershipProject";

describe("peerAccessCopy (T-0068 bearer-missing error UX)", () => {
  test("not_connected maps to the 'Connect this server first' panel", () => {
    const copy = peerAccessCopy("not_connected");
    expect(copy.title).toBe("Connect this server first");
    expect(copy.body("srv_xyz")).toMatch(/srv_xyz/);
    expect(copy.body("srv_xyz")).toMatch(/install handshake/);
  });

  test("access_denied maps to the 'Access denied' panel", () => {
    const copy = peerAccessCopy("access_denied");
    expect(copy.title).toBe("Access denied");
    expect(copy.body("srv_xyz")).toMatch(/srv_xyz/);
    expect(copy.body("srv_xyz")).toMatch(/rejected/);
  });

  test("the two kinds produce distinct titles", () => {
    expect(peerAccessCopy("not_connected").title).not.toBe(
      peerAccessCopy("access_denied").title,
    );
  });
});
