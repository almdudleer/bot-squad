# scripts/install/

Source artifacts for the bot-squad installer. The mothership serves
these files at tokened URLs after a small substitution pass.

## Files

| File                                  | Served at                          | Purpose                                              |
|---------------------------------------|------------------------------------|------------------------------------------------------|
| `install.sh`                          | `/i/<token>/install.sh`            | Idempotent installer. The only artifact that runs.   |
| `bootstrap-claude-instructions.md`    | `/i/<token>/instructions.md`       | Brief for a Claude Code session (tool-using).        |
| `chat-agent-prompt.txt`               | `/i/<token>/prompt.txt`            | Prompt for a generic chat AI (copy-paste-loop user). |

## Placeholders the mothership substitutes

The four substitution targets in `install.sh` and the two in the two md/txt
artifacts. The mothership rewrites these as literal text at serve time —
no templating engine, no escapes; they appear once each per file:

- `__INSTALL_TOKEN__` — opaque `bsq_install_<32B b64url>`, single-burn at `/api/m/installer/connect` (24h TTL)
- `__MOTHERSHIP_URL__` — the mothership root URL the install talks back to
- `__CLONE_URL__` — git clone URL the installer uses (may itself be tokened)
- `__REPO_REF__` — git ref to check out (branch or tag)

When `install.sh` is read raw from the repo (no substitution), the env-var
defaults kick in. Set `BOTSQUAD_SKIP_MOTHERSHIP=1` and override
`BOTSQUAD_CLONE_URL` to smoke locally.

## User-copied surface (3 variants, Chapter I §3)

- **§3.1 / §3.2 — has claude-code (here or on a peer with SSH):**
  copyable prompt is approximately
  > Please fetch the instructions from `__MOTHERSHIP_URL__/i/__INSTALL_TOKEN__/instructions.md` and install bot-squad here.

- **§3.3 "I'll do it myself":**
  copyable command is
  > `curl -fsSL __MOTHERSHIP_URL__/i/__INSTALL_TOKEN__/install.sh | bash`

- **§3.3 "I have a chat-based AI agent to help me":**
  copyable prompt is the rendered `chat-agent-prompt.txt`, pasted directly
  into ChatGPT / Gemini / etc.

## State

The installer keeps two files under `$HOME/.bot-squad/` on the target host:

- `install.state` — append-only newline list of completed checkpoint
  names. Drives idempotency.
- `server.token` — long-lived `bsq_server_<32B b64url>` bearer minted by
  the mothership at `/api/m/installer/connect`. Mode 0600.

## Smoke (local)

From a Linux host with docker + apt + a user in the `sudo` group:

```bash
cd /path/to/bot-squad/dev
BOTSQUAD_SKIP_MOTHERSHIP=1 \
  BOTSQUAD_CLONE_URL="$PWD" \
  BOTSQUAD_REPO_REF=bot_squad/dev \
  BOTSQUAD_INSTALL_DIR=/tmp/bot-squad-smoke \
  BOTSQUAD_STATE_DIR=/tmp/bot-squad-smoke-state \
  bash scripts/install/install.sh
```

Idempotency check: run twice. Second run should print only `skip` lines
for each checkpoint until the final `print_attach`.
