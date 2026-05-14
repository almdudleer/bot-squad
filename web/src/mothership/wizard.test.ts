import { describe, expect, test } from "vitest";

import {
  buildClaudePromptOnTarget,
  buildClaudePromptViaSsh,
  buildCurlOneLiner,
  initialAnswers,
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
});

describe("buildClaudePromptOnTarget", () => {
  test("embeds the instructions URL with token", () => {
    const out = buildClaudePromptOnTarget({
      mothershipBase: "https://staging.bot-squad.org",
      token: "bsq_install_TOKEN",
    });
    expect(out).toContain(
      "https://staging.bot-squad.org/i/bsq_install_TOKEN/instructions.md",
    );
    expect(out).toContain("single-use install token");
  });

  test("strips a trailing slash from mothershipBase before joining", () => {
    const out = buildClaudePromptOnTarget({
      mothershipBase: "https://staging.bot-squad.org/",
      token: "bsq_install_T",
    });
    expect(out).toContain(
      "https://staging.bot-squad.org/i/bsq_install_T/instructions.md",
    );
    // Sanity: no doubled slash.
    expect(out).not.toContain("//i/");
  });
});

describe("buildClaudePromptViaSsh", () => {
  test("embeds ssh target + instructions URL", () => {
    const out = buildClaudePromptViaSsh({
      mothershipBase: "https://staging.bot-squad.org",
      token: "bsq_install_T",
      sshTarget: "alice@host.example.com",
    });
    expect(out).toContain("ssh into alice@host.example.com");
    expect(out).toContain(
      "https://staging.bot-squad.org/i/bsq_install_T/instructions.md",
    );
  });

  test("empty ssh target renders a <user@host> placeholder so the copy-pasta still reads", () => {
    const out = buildClaudePromptViaSsh({
      mothershipBase: "https://staging.bot-squad.org",
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
      mothershipBase: "https://staging.bot-squad.org",
      token: "bsq_install_ABC",
    });
    expect(out).toBe(
      [
        "A mothership at https://staging.bot-squad.org issued me a token.",
        "Run: curl https://staging.bot-squad.org/i/bsq_install_ABC/install.sh",
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
