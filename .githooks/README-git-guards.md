# The git guards in this repository (T-0659)

⚠ **The names in the shared code are examples from signal-tracker, not claims
about this repository.** `lib/commit-policy.sh` and `lib/push-policy.sh` are
byte-identical across every clone that carries these guards — that is what
`check-guard-parity.sh` enforces — so their comments quote *that* repository's
incident commit (`b465362f`), its `.env.bak-ifx-2026-08-07`, and its per-clone
identity table. **None of those exist here.** This repository's own facts are
below. Looking for `b465362f` in bot-squad will find nothing, and that is not a
sign anything is wrong.

## What happened here, 2026-08-14

At 22:49:46Z an unidentified actor committed `init` as `t <t@t>` into TWO live
clones 13 seconds apart. This repository took **`4576a03`**: 425 files in one
commit — `Dockerfile` cut to a single `y`, `web/f.ts`, `.playwright-mcp/`,
`_t/`, `api/_t/` including **`api/_t/cfg/secrets.toml`**, and screenshots. It
was reset away at 22:57:32, so it survives only as an unreachable object.

The actor also overwrote `user.name` / `user.email` in `.git/config` of both
clones, so legitimate peer commits went on being signed `t <t@t>` after the
actor was gone.

`.githooks/pre-commit` existed here and RAN. It let all of it through: it looked
at neither the identity nor the content of the index, and by construction it did
not block — it printed a peer-activity report and asked for a re-run with
`BOT_SQUAD_ACK_PEERS=1`, which any automatic actor performs for free.

## The three refusing gates now in place

| gate | refuses | released ONLY by |
|---|---|---|
| identity | `user.email` outside this clone's allowlist, naming found *and* expected | `BOT_SQUAD_ALLOW_IDENTITY=<the exact address>` |
| secret-like path (commit) | `.env*`, `*.pem`, `*.key`, `id_rsa`, `*credentials*`, `*secret*`, … staged | `BOT_SQUAD_ALLOW_SECRET_PATHS=<the exact path(s)>` |
| secret-like path (push) | any commit in the pushed RANGE introducing such a path | `BOT_SQUAD_ALLOW_SECRET_PUSH=<the exact path(s)>` |
| no identity policy | the allowlist absent from `HEAD`, empty, comments-only, a directory, or with no line for this clone | `BOT_SQUAD_ALLOW_MISSING_IDENTITY_LIST=<the list's path>` |

None of them is released by `BOT_SQUAD_ACK_PEERS` (which `bsq commit --ack` sets
on ordinary commits all day) or by `BOT_SQUAD_SKIP_ATTR_SMOKE` (which belongs to
this repository's lint gates). One habitual variable must not disarm an
unrelated defence, and no key is a bare `=1`: the caller has to name the exact
address or path, so the release cannot become muscle memory.

`scripts/cli/safe-commit.sh` carries **no copy** of these gates and needs none:
it ends in a plain `git commit "$@"`, so the hook runs underneath it. Measured
2026-08-15 through the wrapper itself, by HEAD movement — an ordinary commit
lands, `t@t` is refused with the habitual keys set, the named key releases it,
a secret-like path is refused, its named key releases it. Two copies would
drift, and the one that drifts is the one nobody has watched fire.

## This repository's policy files

`allowed-identities` — **measured, not copied.** All 1020 commits reachable from
every ref, across all four clones (`dev`, `master`, `deploy`,
`/home/www/bot-squad`), are authored and committed by
`leshaserdyukov@gmail.com`. `almdudleer@dev.local` is the sibling repository's
`dev` identity; it was written into this clone's `.git/config` at 22:57 on
2026-08-14 while the incident was being cleaned up, and has never signed a
commit here. It is deliberately **not** in the list.

`allowed-lookalike-paths` — exactly four paths in the whole history match the
gate's predicate, and all four are now listed. `.env.example` was held OUT of
the list until 2026-08-15 because its `TG_BOT_TOKEN` line carried the live bot
token; T-0663 part 1 replaced that value with a placeholder, which is what makes
the exception legitimate. **The live value is still in the history behind HEAD**
— rotating it at BotFather is T-0663 part 2 and needs the stakeholder, because
the same token serves the prod bot.

`repo-facts` — the historical facts the guard suites cannot derive. An empty
value there is not a defect: the section that needs it SKIPS, loudly, with its
own counter.

## Running the suites

```bash
.githooks/test_commit_policy.sh     # the commit gates
.githooks/test_push_policy.sh       # the push gate
.githooks/check-guard-parity.sh <other clone>   # are we running the same gate?
.githooks/check-guard-parity.sh --selftest      # …and can that check say NO?
```

A SKIP is not a PASS. It has its own counter and its own line precisely because
a guard suite has two ways of being green — to check, and to be switched off.

## A set of negative arms needs one arm that expects a PASS (T-0659)

Every arm of a negative-control suite expects a refusal. That is what makes them
negative controls — and it is also why, on their own, they cannot tell a working
instrument from a dead one. **A check that answers BROKEN to everything satisfies
all of them.**

This is not hypothetical here. `check-guard-parity.sh` grew a property check for
the hook wrappers, and its first version read each file through `grep -n`, which
prefixes every line with `<n>:`. An anchor like `(^|[[:space:]])` therefore could
never match a command at column 1 — the character before it was the colon. Every
wrapper in every clone came back BROKEN. **The selftest printed 9 of 9.** All
four new negative arms — no call, call after `exit 0`, wrong file sourced, call
commented out — passed, because each of them expects exactly BROKEN.

What caught it was the one arm that expects the opposite: an honest wrapper pair
must read PARITY. Without that arm the instrument would have shipped screaming,
with a green selftest to vouch for it.

So: **whenever you add arms that all expect a refusal, add one that expects a
pass, and make it the control the others are read against.** It costs one case.
The alternative costs an instrument that everybody trusts and nobody can use —
and an instrument that lies towards alarm gets switched off exactly as fast as
one that lies towards calm.

## A declared exception needs a stated reason, or it is not a declaration (T-0659)

`repo-facts` can declare a secret-like path that is **deliberately** left out of
`allowed-lookalike-paths` — `SUITE_KNOWN_UNLISTED`. Without it, the push suite's
published-history check reds forever in any repository that has such a path, and
a permanently red suite gets switched off along with everything it was watching.

The declaration does **nothing** unless `SUITE_KNOWN_UNLISTED_WHY` is also set,
and the suite prints that reason next to the path every run. That is the whole
difference between an honest exception and a quiet silencing:

- an *undeclared* unlisted path still reds — nothing was waived by accident;
- a *declared* one reports as a loud INFO **with its reason on the same line**,
  so the next reader learns why rather than inheriting a mystery;
- a declaration with an empty reason is ignored and the path reds again, so the
  cheapest way to silence the check — add the path, skip the explanation — is
  the one thing it will not accept.

The reason lives in a tracked file, so adding one is visible in a diff and
reversible in one line. bot-squad declared exactly one — `.env.example`, while
its `TG_BOT_TOKEN` held a live token, so that reddening a push touching it read
as the intended outcome rather than a false positive. T-0663 part 1 replaced the
value, the path moved into the exception list, and the declaration was emptied
the same day: a declaration that outlives its reason is how a waiver becomes
permanent by accident.

## What these gates do NOT close

`--no-verify`, a `core.hooksPath` pointed elsewhere, `commit-tree` +
`update-ref`, and a clone where `core.hooksPath` was never set all still reach
history without meeting the commit gates. What covers that class regardless of
how the commit was made is the **push** gate, which is why it is here too — and
`install-hooks.sh`, which is what arms a clone in the first place. Of the
bot-squad clones on this box, only `dev` has `core.hooksPath` set.
