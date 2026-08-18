"""T-0903: the deploy/restart permission round-trip is gone from the contracts.

WHAT HE ASKED (2026-08-18T09:16:52Z, verbatim on T-0903)::

    так, деплой, пожалуйста, хватит ждать моих разрешений на рестарт в этом
    проекте. bot-squad должен рестартить и как можно скорее до меня докатывать
    все изменения что я прошу, я единственный пользователь пока что

and, asked directly whether that covered only reviewed low-risk restarts or
literally any change including DB schema and prod data, «про любые!»
(09:24:14Z).

WHY IT IS A TEST AND NOT JUST AN EDIT. The pattern he killed was never written
down as an instruction — no role contract contained the words "ask him before
restarting". A watchrobot operator invented it, paired it with a self-authored
"молчание не согласие", and T-0895 sat finished, reviewed and pushed for a day.
A habit nothing forbids grows back, and the only surface that reaches a live
session automatically is the role contract: ``bsq``'s ``_assemble_prompt``
inlines it verbatim into the spawn brief. So the enforceable form is the one
below — the rule is PRESENT in every contract whose session can restart or
deploy, the three copies have not drifted, and it survives assembly into a real
brief. A pointer to a doc, or prose in a ticket, would reach neither.

Scope of REQUIRED_ROLES: set on the call sites. operator, teamlead and
prod-teamlead are the roles whose contracts have them restarting workers or
queueing deploys. ``dev`` is deliberately NOT in the list — a dev never
deploys — but its own contract stated the gate as an ownership fact ("prod
deploys are stakeholder-owned"), so it gets its own assertion further down.
"""
from __future__ import annotations

import importlib.util
import os
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
ROLES_DIR = _REPO / "api" / "app" / "resources" / "roles"
_BSQ_PATH = _REPO / "scripts" / "cli" / "bsq"
SKILL = _REPO / "framework-skills" / "autonomous-when-grounded" / "SKILL.md"

HEADING = "## Ship it — his standing authorization, no permission round-trip"

REQUIRED_ROLES = ("operator", "teamlead", "prod-teamlead")

# `--role` spellings `_assemble_prompt` accepts for those same three contracts.
BRIEF_ROLES = {"operator": "operator", "teamlead": "tl", "prod-teamlead": "prod-teamlead"}

# The load-bearing claims. Each is here because dropping it silently would
# leave the section looking intact while the thing it fixes walked out.
REQUIRED_CLAIMS = (
    # His words — the authorization itself, not a paraphrase of it.
    "хватит ждать моих разрешений на рестарт",
    "я единственный пользователь пока что",
    # The answer to the narrowing question, so no session re-narrows it.
    "«про любые!»",
    # The inversion that actually cost the day: a ping with no reply read as
    # a refusal. Without this line the rule is silent on the failing case.
    "silence is\nnot a hold",
    # The limits. A session must not read "no permission round-trip" as
    # "no bar" or as licence to drop safety practices that are not about him.
    "review, green tests, the ticket's DoD",
    "destroy-guard on unpushed commits",
    "A captured wish is still not a build\n  directive (drive-mode, T-0656)",
    # Report after — otherwise removing the ask also removes the telling.
    "**Tell him after, not before.**",
)


def read_role(stem: str) -> str:
    return (ROLES_DIR / f"{stem}.md").read_text(encoding="utf-8")


def block_of(text: str) -> str:
    """The rule's section: its heading through the next top-level heading."""
    start = text.index(HEADING)
    rest = text.find("\n## ", start + 1)
    return text[start:rest if rest != -1 else len(text)]


@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_deploying_role_carries_the_standing_authorization(role):
    assert HEADING in read_role(role), (
        f"{role}.md has no T-0903 section. Its session restarts workers or "
        "queues deploys, and a rule it is not served is a rule it invents "
        "around — which is how T-0895 sat finished for a day."
    )


@pytest.mark.parametrize("role", REQUIRED_ROLES)
@pytest.mark.parametrize("claim", REQUIRED_CLAIMS)
def test_every_load_bearing_claim_is_present(role, claim):
    block = block_of(read_role(role))
    assert claim in block, f"{role}.md dropped from the T-0903 section: {claim!r}"


def test_the_three_copies_are_byte_identical():
    """Three copies, one rule. Prose register may differ per role elsewhere;
    this section may not — it is an authorization, and a divergent copy is a
    second authorization nobody granted."""
    blocks = {r: block_of(read_role(r)) for r in REQUIRED_ROLES}
    distinct = set(blocks.values())
    assert len(distinct) == 1, (
        "the T-0903 section has diverged between role docs — "
        f"{len(distinct)} variants across {sorted(blocks)}. Fix the copies to "
        "match rather than teaching this test about a second variant."
    )


def test_dev_contract_no_longer_routes_prod_deploys_through_him():
    """`dev.md` never told a dev to ask permission — it stated the gate as an
    ownership fact, which reads to a dev as "this waits on him"."""
    dev = read_role("dev")
    assert "prod deploys are stakeholder-owned" not in dev
    assert "**Neither of them waits for the\n  stakeholder's permission to restart or deploy**" in dev


def test_the_ask_rule_skill_carves_restarts_out_of_the_irreversible_bucket():
    """`autonomous-when-grounded` is the text that produced the habit: its
    cost-of-mistake gate lists "prod-breaking-without-rollback" as the genuine
    ask, and a restart reads as exactly that. The carve-out belongs beside it,
    not only in the contracts."""
    t = SKILL.read_text(encoding="utf-8")
    gate = "**Irreversible** (data loss, destructive migration, credential change"
    assert gate in t, "the cost-of-mistake gate moved — re-anchor this check"
    carve = "A restart or a deploy is NOT in that bucket on his projects"
    # Exactly once: `.claude/skills/<name>/SKILL.md` is a HARDLINK to this file
    # (same inode), so an editor that "updates both copies" writes the same
    # inode twice and silently duplicates the paragraph. Measured while writing
    # this ticket.
    assert t.count(carve) == 1, f"carve-out appears {t.count(carve)}x — see hardlink note"
    assert "«про любые!»" in t
    assert "silence is not a hold" in t
    # The gate itself must survive: he removed one waiting habit, not the rule
    # that a genuinely irreversible call is worth a page.
    assert "that's the genuine ask. Page deliberately." in t


# ---------------------------------------------------------------------------
# Reach: the contract text a SPAWNED session actually receives.
# ---------------------------------------------------------------------------

def _load_bsq(bot_squad_root: Path):
    """Load the extensionless CLI with BOT_SQUAD pointed at a scratch root.

    BOT_SQUAD is read at module scope, so it must be set BEFORE exec_module
    (T-0806). A fresh module object per call keeps the roots independent.
    """
    os.environ["BOT_SQUAD"] = str(bot_squad_root)
    loader = SourceFileLoader("bsq_mod_t0903", str(_BSQ_PATH))
    spec = importlib.util.spec_from_loader("bsq_mod_t0903", loader)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _scratch_root(tmp_path: Path) -> tuple[Path, Path]:
    """The layout `_assemble_prompt` reads: roles SSOT + a ticket to assign."""
    (tmp_path / "api" / "app" / "resources").mkdir(parents=True)
    (tmp_path / "api" / "app" / "resources" / "roles").symlink_to(ROLES_DIR)
    backlog = tmp_path / "data" / "test-project" / "backlog"
    backlog.mkdir(parents=True)
    ticket = backlog / "T-0001-ship.md"
    ticket.write_text(
        "---\nid: T-0001\ntitle: 'restart the worker'\nstatus: open\n---\n\n"
        "## Stakeholder notes\n\nship it\n",
        encoding="utf-8",
    )
    return tmp_path, ticket


@pytest.mark.skipif(not _BSQ_PATH.exists(), reason="bsq CLI not in this tree")
@pytest.mark.parametrize("role", REQUIRED_ROLES)
def test_the_rule_survives_into_a_real_spawn_brief(tmp_path, role):
    """Editing the .md and assuming it is wired in is the failure this guards.
    Drive the REAL assembler and read the brief a spawned session is handed."""
    root, ticket = _scratch_root(tmp_path)
    bsq = _load_bsq(root)
    brief = bsq._assemble_prompt(
        "test-project", ["T-0001"], {"T-0001": ticket},
        {"T-0001": bsq.read_frontmatter(ticket)}, BRIEF_ROLES[role], "S-fake-tl",
    )
    # Positive control for the instrument: the brief really is this role's
    # contract. Without it, an empty/mis-routed assembly would pass the
    # absence half of any later check and fail this one loudly instead.
    assert f"== YOUR ROLE CONTRACT ({role}.md) ==" in brief
    assert HEADING in brief, (
        f"the T-0903 section is in {role}.md but does NOT reach the assembled "
        "brief — the session is served text that still has the gate."
    )
    assert "«про любые!»" in brief


# ---------------------------------------------------------------------------
# Positive control (T-0740): a guard that passes with the defect present pins
# nothing. Drive the same extractors over deliberately drifted copies.
# ---------------------------------------------------------------------------

def test_the_checks_above_can_actually_fail():
    good = read_role("operator")

    # 1. The whole section removed — the presence check must bite.
    without = good.replace(block_of(good), "", 1)
    assert without != good, "fixture did not mutate — the control proves nothing"
    assert HEADING not in without

    # 2. A plausible re-narrowing: the scope answer quietly softened. This is
    #    the drift the ticket names explicitly ("do not re-narrow this").
    narrowed = good.replace("«про любые!»", "reviewed low-risk restarts", 1)
    assert narrowed != good, "scope fixture did not mutate"
    assert "«про любые!»" not in block_of(narrowed)

    # 3. The inversion line dropped — the section still reads correct, and the
    #    exact failure that cost a day (ping, silence, wait) walks back in.
    no_silence = good.replace("silence is\nnot a hold", "he will answer", 1)
    assert no_silence != good, "silence fixture did not mutate"
    assert "silence is\nnot a hold" not in block_of(no_silence)

    # 4. The limits dropped — "no round-trip" turns into "no bar".
    no_bar = good.replace("review, green tests, the ticket's DoD", "whatever", 1)
    assert no_bar != good, "bar fixture did not mutate"
    assert "review, green tests, the ticket's DoD" not in block_of(no_bar)

    # 5. Divergence between copies — the identity check must bite.
    blocks = {r: block_of(read_role(r)) for r in REQUIRED_ROLES}
    blocks["operator"] = blocks["operator"] + "\n\nAlso: ask him first.\n"
    assert len(set(blocks.values())) == 2


@pytest.mark.skipif(not _BSQ_PATH.exists(), reason="bsq CLI not in this tree")
def test_the_brief_reach_check_can_actually_fail(tmp_path):
    """The reach check is the one that would be silently green if the rule were
    edited into a file the assembler does not read. Prove it bites: assemble
    from a roles dir whose operator.md has the section cut out, and require the
    same assertion to notice."""
    root, ticket = _scratch_root(tmp_path)
    # Replace the symlinked SSOT with a real dir holding a drifted operator.md.
    (root / "api" / "app" / "resources" / "roles").unlink()
    drifted_roles = root / "api" / "app" / "resources" / "roles"
    drifted_roles.mkdir()
    good = read_role("operator")
    (drifted_roles / "operator.md").write_text(good.replace(block_of(good), "", 1),
                                               encoding="utf-8")

    bsq = _load_bsq(root)
    brief = bsq._assemble_prompt(
        "test-project", ["T-0001"], {"T-0001": ticket},
        {"T-0001": bsq.read_frontmatter(ticket)}, "operator", "S-fake-tl",
    )
    # GREEN CONTROL: the assembler really did read this drifted dir, so the
    # absence below is the cut section — not a missing/empty contract.
    assert "== YOUR ROLE CONTRACT (operator.md) ==" in brief
    assert "You are the **operator** for this project" in brief

    assert HEADING not in brief, "reach check cannot fail — it pins nothing"
