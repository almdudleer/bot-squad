# scripts/install/

Source artifacts for the bot-squad installer. The mothership serves
`install.sh` and `bootstrap-claude-instructions.md` at tokened URLs after
a small substitution pass; `chat-agent-prompt.txt` is rendered inline by
the FE wizard (T-0031) — not served from a tokened URL.

## Files

| File                                  | Served at                                                | Purpose                                              |
|---------------------------------------|----------------------------------------------------------|------------------------------------------------------|
| `install.sh`                          | `/i/<token>/install.sh`                                  | Idempotent installer. The only artifact that runs.   |
| `pkg.sh`                              | _(loaded by `install.sh` from alongside it)_             | Per-distro package-manager abstraction.              |
| `nixos/bot-squad.nix`                 | _(copied into `<install_dir>/nixos/` on NixOS hosts)_    | Declarative NixOS module (T-0057).                   |
| `bootstrap-claude-instructions.md`    | `/i/<token>/instructions.md`                             | Brief for a Claude Code session (tool-using).        |
| `chat-agent-prompt.txt`               | _(rendered inline by FE wizard T-0031, not a tokened URL)_ | Prompt for a generic chat AI (copy-paste-loop user). |

## Placeholders the mothership substitutes

Four substitution targets in `install.sh`, four in
`bootstrap-claude-instructions.md`, and two in `chat-agent-prompt.txt`.
Substitution uses literal `str.replace()` (no templating engine, no
escapes), so each placeholder is rewritten everywhere it appears:

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

## NixOS (T-0057, declarative)

NixOS hosts get a shorter chain: `detect_distro` + `emit_nixos_module`.
The installer cannot `apt-get`/`dnf`/`pacman`/`apk` docker on a NixOS
host without breaking `/etc/nixos/configuration.nix` as the
source-of-truth. Instead, when `/etc/os-release` announces `ID=nixos`
(or `BOTSQUAD_DISTRO_FAMILY=nixos` is forced), the chain copies
`scripts/install/nixos/bot-squad.nix` into
`<install_dir>/nixos/bot-squad.nix` and prints an instructions block
telling the admin to:

1. Review the emitted module.
2. Add `imports = [ ./bot-squad.nix ];` + `services.bot-squad.enable =
   true;` to `/etc/nixos/configuration.nix`.
3. Run `sudo nixos-rebuild switch`.
4. Manually clone the repo into the install dir and
   `docker compose up -d --build` — auto-clone + compose-up from
   `install.sh` on NixOS is deferred (the module declares docker +
   nodejs + python3 + tmux + ca-certificates + the
   `bot-squad-worker` systemd unit, which is the contract).

Override the module destination with `BOTSQUAD_NIXOS_MODULE_DEST=...`
or the source-of-truth path with `BOTSQUAD_NIXOS_MODULE_SRC=...`.
