self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.atuout;
in
{
  options.programs.atuout = {
    enable = lib.mkEnableOption "atuout — Atuin command-output harvester";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.atuout;
      defaultText = lib.literalExpression "atuout.packages.\${system}.atuout";
      description = "The atuout package to use.";
    };

    reconciler.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Run the long-lived reconciler as a single systemd user service
        (`atuout reconcile --daemonize`). This is the one background daemon;
        the shell hook's `reconcile ensure` flock-detects it and no-ops.
      '';
    };

    enableZshIntegration = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = ''
        Emit `eval "$(atuout init-zsh)"` in the zsh init. This MUST be evaluated
        AFTER `atuin pty-proxy init zsh` (the hook hard-requires
        `ATUIN_PTY_PROXY_ACTIVE`); atuout places its eval late (mkOrder 1500) but
        the consumer owns starting pty-proxy first.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    # One background reconciler for the whole user session.
    systemd.user.services.atuout-reconciler = lib.mkIf cfg.reconciler.enable {
      Unit = {
        Description = "atuout reconciler (backfills missed Atuin output captures)";
        # The reconciler holds a TailHistory stream open against the atuin
        # daemon; start it once the daemon is up. HM's atuin daemon service is
        # named "atuin-daemon".
        After = [ "atuin-daemon.service" ];
        Wants = [ "atuin-daemon.service" ];
      };
      Service = {
        ExecStart = "${lib.getExe cfg.package} reconcile --daemonize";
        Restart = "on-failure";
        RestartSec = 5;
        # Give the reconciler atuout on PATH in case it shells out to itself.
        Environment = [ "PATH=${cfg.package}/bin:/run/current-system/sw/bin" ];
      };
      Install.WantedBy = [ "default.target" ];
    };

    programs.zsh.initContent = lib.mkIf cfg.enableZshIntegration (
      # Late order so this lands after atuin's pty-proxy init (which the consumer
      # emits earlier). The hook itself guards on ATUIN_PTY_PROXY_ACTIVE and
      # returns cleanly if pty-proxy isn't active.
      lib.mkOrder 1500 ''
        if command -v atuout >/dev/null 2>&1; then
          eval "$(atuout init-zsh)"
        fi
      ''
    );
  };
}
