# bot-squad NixOS module (T-0057).
#
# NixOS configures system packages declaratively, so the bot-squad
# installer cannot apt-get/dnf/pacman docker on a NixOS host. This
# module is the substitute: it declares the OS-level dependencies
# bot-squad needs (docker, nodejs, python3 with venv, tmux,
# ca-certificates, ...) plus the bot-squad-worker systemd service.
#
# install.sh writes (copies) this file to
# <BOTSQUAD_INSTALL_DIR>/nixos/bot-squad.nix when /etc/os-release
# announces ID=nixos, then prints the imports/instructions block and
# exits — the rest of the imperative checkpoints (pkg_install, group
# create, repo clone, docker compose up) are skipped, because the
# NixOS admin owns those via configuration.nix + nixos-rebuild switch.
#
# Integration:
#   1. Drop this file next to /etc/nixos/configuration.nix (the
#      installer writes it to <install_dir>/nixos/bot-squad.nix by
#      default — copy or symlink as you prefer).
#   2. Edit /etc/nixos/configuration.nix:
#        imports = [
#          /etc/nixos/hardware-configuration.nix
#          ./bot-squad.nix
#        ];
#        services.bot-squad.enable = true;
#        services.bot-squad.user   = "<linux user owning the install>";
#   3. sudo nixos-rebuild switch
#   4. The docker socket + tmux + nodejs are now available. Clone the
#      bot-squad repo into services.bot-squad.installDir and run
#      `docker compose up -d --build` to bring the API + worker stack
#      online. Auto-clone + compose-up from install.sh on NixOS is
#      deferred (T-0057 ships the module + docs lane only).

{ config, pkgs, lib, ... }:

let
  cfg = config.services.bot-squad;
in {
  options.services.bot-squad = {
    enable = lib.mkEnableOption "bot-squad worker (NixOS module — T-0057)";

    installDir = lib.mkOption {
      type = lib.types.path;
      default = "/home/www/bot-squad";
      description = ''
        Filesystem root for the bot-squad install (repo clone + data
        dir). Matches BOTSQUAD_INSTALL_DIR in install.sh.
      '';
    };

    user = lib.mkOption {
      type = lib.types.str;
      default = "botsquad";
      description = ''
        Linux user that owns the install and runs the worker. On a
        fresh NixOS host let this default to "botsquad"; on an
        existing host set it to the user already running the install.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    # --- Docker engine + compose v2 plugin -----------------------------
    # The bot-squad API + UI stack is a docker-compose project; once
    # the manual `docker compose up` lands, the worker systemd unit
    # talks to the daemon via the same docker socket.
    virtualisation.docker.enable = true;

    # --- OS-level packages ---------------------------------------------
    # Mirrors the logical-package list pkg.sh declares for
    # debian/fedora/arch (curl/ca-certificates/git/jq/tmux/nodejs/
    # python3). python3 on NixOS ships venv built-in (same as
    # fedora/arch), no separate venv package.
    environment.systemPackages = with pkgs; [
      curl
      cacert
      git
      jq
      tmux
      nodejs_20
      python3
      docker-compose
    ];

    # --- bot-squad worker systemd unit ---------------------------------
    # Mirrors systemd/bot-squad-worker.service (the imperative-distro
    # unit) under the NixOS-native systemd.services attr-set. The unit
    # assumes:
    #   - the repo has been cloned to ${cfg.installDir}
    #   - the worker venv lives at ${cfg.installDir}/worker/.venv
    # On a fresh install neither exists yet; enable+start are deferred
    # until after the admin has done the manual clone + venv steps.
    # The unit is marked WantedBy=multi-user.target so once the install
    # is bootstrapped, the next `nixos-rebuild switch` (or reboot)
    # brings it up automatically.
    systemd.services.bot-squad-worker = {
      description = "bot-squad worker (APScheduler + Unix-socket action API)";
      after = [ "network.target" "docker.service" ];
      wants = [ "docker.service" ];
      wantedBy = [ "multi-user.target" ];
      serviceConfig = {
        Type = "simple";
        User = cfg.user;
        WorkingDirectory = cfg.installDir;
        Environment = [
          "PYTHONUNBUFFERED=1"
          "LOG_LEVEL=info"
          "BOT_SQUAD_MODE=coordinator"
        ];
        ExecStart = "${cfg.installDir}/worker/.venv/bin/python -m bot_squad_worker --config ${cfg.installDir}/config";
        Restart = "on-failure";
        RestartSec = "10s";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ReadWritePaths = [ "${cfg.installDir}/data" ];
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = "AF_UNIX AF_INET AF_INET6";
        LockPersonality = true;
      };
    };

    # --- Shared 'www' group --------------------------------------------
    # The imperative installer creates this group at the botsquad_group
    # checkpoint and chgrps $installDir into it. Declare it here so the
    # NixOS user/group reconciliation handles it without a manual
    # groupadd.
    users.groups.www = { };
  };
}
