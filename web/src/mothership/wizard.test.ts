import { describe, expect, test } from "vitest";

import {
  buildClaudePromptOnTarget,
  buildClaudePromptViaSsh,
  buildCurlOneLiner,
  buildInviteClaudePrompt,
  initialAnswers,
  inviteWhatComesNext,
  parseInvitePaste,
  renderChatAgentPrompt,
  visibleSections,
} from "./wizard";

describe("visibleSections", () => {
  test("only §3.0 is visible at the start", () => {
    expect(visibleSections(initialAnswers())).toEqual(["q30"]);
  });

  test("§3.0 yes keeps subsequent sections hidden (invite-join branch)", () => {
    expect(
      visibleSections({ ...initialAnswers(), joining: "yes" }),
    ).toEqual(["q30"]);
  });

  test("§3.0 no reveals §3.1", () => {
    expect(visibleSections({ ...initialAnswers(), joining: "no" })).toEqual([
      "q30",
      "q31",
    ]);
  });

  test("§3.1 yes stops the reveal — outcome shown in-place", () => {
    expect(
      visibleSections({
        ...initialAnswers(),
        joining: "no",
        claudeOnTarget: "yes",
      }),
    ).toEqual(["q30", "q31"]);
  });

  test("§3.1 no reveals §3.2", () => {
    expect(
      visibleSections({
        ...initialAnswers(),
        joining: "no",
        claudeOnTarget: "no",
      }),
    ).toEqual(["q30", "q31", "q32"]);
  });

  test("§3.2 no reveals §3.3 — the fallback self-help branch", () => {
    expect(
      visibleSections({
        ...initialAnswers(),
        joining: "no",
        claudeOnTarget: "no",
        claudeViaSsh: "no",
      }),
    ).toEqual(["q30", "q31", "q32", "q33"]);
  });

  test("flipping §3.0 back to yes hides later sections but preserves their answers", () => {
    const answered = {
      joining: "no" as const,
      claudeOnTarget: "no" as const,
      claudeViaSsh: "no" as const,
      sshTarget: "alice@bastion",
      invitePaste: "",
    };
    expect(visibleSections(answered)).toEqual(["q30", "q31", "q32", "q33"]);

    const flipped = { ...answered, joining: "yes" as const };
    // §3.1+ disappear from the rendered chain...
    expect(visibleSections(flipped)).toEqual(["q30"]);
    // ...but the answer state itself is untouched, so flipping back
    // restores the full chain without re-asking.
    const restored = { ...flipped, joining: "no" as const };
    expect(visibleSections(restored)).toEqual(["q30", "q31", "q32", "q33"]);
    expect(restored.sshTarget).toBe("alice@bastion");
  });

  // T-0125: the invite-join branch is rendered INSIDE Q30Section (not as a
  // new section in visibleSections), so visibleSections itself stays the
  // same — but initialAnswers must now carry the invitePaste slot and the
  // joining="yes" branch must continue to suppress q31..q33.
  test("§3.0 yes still collapses everything below (invite-join branch lives inside Q30)", () => {
    expect(
      visibleSections({
        ...initialAnswers(),
        joining: "yes",
        invitePaste: "https://x/i/bsq_invite_TOK/install.sh",
      }),
    ).toEqual(["q30"]);
  });

  test("initialAnswers includes the invitePaste slot", () => {
    expect(initialAnswers().invitePaste).toBe("");
  });
});

describe("buildClaudePromptOnTarget", () => {
  test("embeds the instructions URL with token", () => {
    const out = buildClaudePromptOnTarget({
      mothershipBase: "https://staging.botsquad.dev",
      token: "bsq_install_TOKEN",
    });
    expect(out).toContain(
      "https://staging.botsquad.dev/i/bsq_install_TOKEN/instructions.md",
    );
    expect(out).toContain("single-use install token");
  });

  test("strips a trailing slash from mothershipBase before joining", () => {
    const out = buildClaudePromptOnTarget({
      mothershipBase: "https://staging.botsquad.dev/",
      token: "bsq_install_T",
    });
    expect(out).toContain(
      "https://staging.botsquad.dev/i/bsq_install_T/instructions.md",
    );
    // Sanity: no doubled slash.
    expect(out).not.toContain("//i/");
  });
});

describe("buildClaudePromptViaSsh", () => {
  test("embeds ssh target + instructions URL", () => {
    const out = buildClaudePromptViaSsh({
      mothershipBase: "https://staging.botsquad.dev",
      token: "bsq_install_T",
      sshTarget: "alice@host.example.com",
    });
    expect(out).toContain("ssh into alice@host.example.com");
    expect(out).toContain(
      "https://staging.botsquad.dev/i/bsq_install_T/instructions.md",
    );
  });

  test("empty ssh target renders a <user@host> placeholder so the copy-pasta still reads", () => {
    const out = buildClaudePromptViaSsh({
      mothershipBase: "https://staging.botsquad.dev",
      token: "bsq_install_T",
      sshTarget: "   ",
    });
    expect(out).toContain("ssh into <user@host>");
  });
});

describe("buildCurlOneLiner", () => {
  test("wraps the install URL in a fail-fast pipe-to-bash", () => {
    expect(buildCurlOneLiner("https://x.example.com/i/T/install.sh")).toBe(
      'curl -fsSL "https://x.example.com/i/T/install.sh" | bash',
    );
  });
});

describe("renderChatAgentPrompt", () => {
  test("substitutes __INSTALL_TOKEN__ and __MOTHERSHIP_URL__ (the only keys the chat-agent template uses)", () => {
    const template = [
      "A mothership at __MOTHERSHIP_URL__ issued me a token.",
      "Run: curl __MOTHERSHIP_URL__/i/__INSTALL_TOKEN__/install.sh",
      "Token: __INSTALL_TOKEN__",
    ].join("\n");
    const out = renderChatAgentPrompt({
      template,
      mothershipBase: "https://staging.botsquad.dev",
      token: "bsq_install_ABC",
    });
    expect(out).toBe(
      [
        "A mothership at https://staging.botsquad.dev issued me a token.",
        "Run: curl https://staging.botsquad.dev/i/bsq_install_ABC/install.sh",
        "Token: bsq_install_ABC",
      ].join("\n"),
    );
  });

  test("multiple occurrences are all replaced (global, not first-only)", () => {
    const out = renderChatAgentPrompt({
      template: "__INSTALL_TOKEN__ __INSTALL_TOKEN__ __INSTALL_TOKEN__",
      mothershipBase: "https://x",
      token: "TOK",
    });
    expect(out).toBe("TOK TOK TOK");
  });

  test("strips a trailing slash from mothershipBase before substituting", () => {
    const out = renderChatAgentPrompt({
      template: "url=__MOTHERSHIP_URL__/i/__INSTALL_TOKEN__/install.sh",
      mothershipBase: "https://x/",
      token: "T",
    });
    expect(out).toBe("url=https://x/i/T/install.sh");
  });
});

// ---------------------------------------------------------------------------
// T-0125: §3.0-yes invite-paste branch
// ---------------------------------------------------------------------------

describe("parseInvitePaste", () => {
  test("empty input parses to a null token with no error", () => {
    const out = parseInvitePaste("");
    expect(out).toEqual({
      token: null,
      kind: "unknown",
      mothershipBase: null,
      error: null,
    });
  });

  test("trims whitespace before parsing", () => {
    const out = parseInvitePaste("   \n\t  ");
    expect(out.token).toBe(null);
    expect(out.error).toBe(null);
  });

  test("parses a full invite URL and classifies it as 'invite'", () => {
    const out = parseInvitePaste(
      "https://staging.botsquad.dev/i/bsq_invite_ABC/install.sh",
    );
    expect(out).toEqual({
      token: "bsq_invite_ABC",
      kind: "invite",
      mothershipBase: "https://staging.botsquad.dev",
      error: null,
    });
  });

  test("parses an instructions.md variant of the URL just as well", () => {
    const out = parseInvitePaste(
      "https://staging.botsquad.dev/i/bsq_invite_ABC/instructions.md",
    );
    expect(out.token).toBe("bsq_invite_ABC");
    expect(out.kind).toBe("invite");
  });

  test("bare invite token (no URL) classifies as 'invite'", () => {
    const out = parseInvitePaste("bsq_invite_RAW");
    expect(out).toEqual({
      token: "bsq_invite_RAW",
      kind: "invite",
      mothershipBase: null,
      error: null,
    });
  });

  test("install token pasted on the join branch is classified as 'install' (wrong branch)", () => {
    const out = parseInvitePaste(
      "https://x.example.com/i/bsq_install_T/install.sh",
    );
    expect(out.kind).toBe("install");
    expect(out.token).toBe("bsq_install_T");
  });

  test("unknown prefix falls back to 'unknown'", () => {
    const out = parseInvitePaste("not_a_token");
    expect(out.kind).toBe("unknown");
    expect(out.token).toBe("not_a_token");
  });

  test("malformed URL (looks like one but isn't) surfaces an error", () => {
    const out = parseInvitePaste("https://[broken");
    expect(out.token).toBe(null);
    expect(out.error).not.toBe(null);
  });

  test("URL that's missing the /i/<token> path surfaces a guiding error", () => {
    const out = parseInvitePaste("https://example.com/some/other/path");
    expect(out.token).toBe(null);
    expect(out.error).toMatch(/\/i\/<token>/);
  });

  test("URL-encoded token segment is decoded back to the literal token", () => {
    // %2B = '+' — the BE base64url alphabet doesn't actually emit '+', but
    // the decoder must still pass anything URL-encoded through unchanged.
    const out = parseInvitePaste(
      "https://x.example.com/i/bsq_invite_AB%2BC/install.sh",
    );
    expect(out.token).toBe("bsq_invite_AB+C");
  });
});

describe("inviteWhatComesNext", () => {
  test("invite-flavored copy skips reverse-proxy / domain / TLS guidance", () => {
    const text = inviteWhatComesNext("invite");
    expect(text).toMatch(/joining an existing/i);
    expect(text).toMatch(/skips reverse-proxy/i);
    expect(text).toMatch(/TLS/);
    // Sanity: it explicitly does NOT instruct the user to configure
    // reverse-proxy / domain / TLS — those are settled by the inviter.
    expect(text).not.toMatch(/configure (a )?reverse-proxy/i);
    expect(text).not.toMatch(/set up (a )?domain/i);
  });

  test("install-token paste flags the wrong-branch case", () => {
    const text = inviteWhatComesNext("install");
    expect(text).toMatch(/fresh-install/i);
  });

  test("unknown paste hints at the expected URL shape", () => {
    const text = inviteWhatComesNext("unknown");
    expect(text).toMatch(/bsq_invite/);
  });
});

describe("buildInviteClaudePrompt", () => {
  test("embeds the install.sh URL (not instructions.md — the installer is what the joining user runs)", () => {
    const out = buildInviteClaudePrompt({
      mothershipBase: "https://staging.botsquad.dev",
      token: "bsq_invite_TOK",
    });
    expect(out).toContain(
      "https://staging.botsquad.dev/i/bsq_invite_TOK/install.sh",
    );
    expect(out).toMatch(/single-use invite token/i);
  });

  test("strips a trailing slash from mothershipBase before joining", () => {
    const out = buildInviteClaudePrompt({
      mothershipBase: "https://x/",
      token: "bsq_invite_T",
    });
    expect(out).toContain("https://x/i/bsq_invite_T/install.sh");
    expect(out).not.toContain("//i/");
  });

  test("phrasing does NOT instruct the joining user to set up reverse-proxy / domain / TLS", () => {
    const out = buildInviteClaudePrompt({
      mothershipBase: "https://x",
      token: "bsq_invite_T",
    });
    expect(out).not.toMatch(/reverse-proxy/i);
    expect(out).not.toMatch(/domain/i);
    expect(out).not.toMatch(/TLS/);
  });
});
