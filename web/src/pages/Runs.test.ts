import { describe, it, expect } from "vitest";
import { humanizeRequester } from "./Runs";

// T-0362: the Deployment Queue "Requested by" column showed the raw requester
// SID. humanizeRequester turns it into "<window> (<user>)" while leaving the
// full SID for the tooltip, and passes non-SID values through untouched.
describe("humanizeRequester (T-0362)", () => {
  it("humanizes a requester SID to '<window> (<user>)'", () => {
    expect(humanizeRequester("S-almdudleer-multi_server-TL-p67")).toBe("multi_server-TL (almdudleer)");
    expect(humanizeRequester("S-almdudleer-fe-iterate-p208")).toBe("fe-iterate (almdudleer)");
  });
  it("passes non-SID / already-human values through", () => {
    expect(humanizeRequester("S-stakeholder")).toBe("S-stakeholder"); // no -p<N> suffix
    expect(humanizeRequester("alice")).toBe("alice");
  });
  it("renders an em dash for an empty requester", () => {
    expect(humanizeRequester("")).toBe("—");
  });
});
