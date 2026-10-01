self:
{ config, lib, pkgs, ... }:

let
  cfg = config.programs.atuout;
in
{
  options.programs.atuout = {
    enable = lib.mkEnableOption "atuout agent-output store";

    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.atuout;
      defaultText = lib.literalExpression "atuout.packages.\${system}.atuout";
      description = "The atuout package to use.";
    };

    agentIngest.enable = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Periodically import agent output from Atuin history and session transcripts.";
    };

    agentIngest.interval = lib.mkOption {
      type = lib.types.str;
      default = "5m";
      example = "15m";
      description = "How often the systemd user timer retries recent agent entries.";
    };

    agentIngest.lookbackHours = lib.mkOption {
      type = lib.types.ints.positive;
      default = 6;
      description = "How far back each timer run scans Atuin history for agent entries.";
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    systemd.user.services.atuout-agent-ingest = lib.mkIf cfg.agentIngest.enable {
      Unit.Description = "Import recent agent output into the atuout store";
      Service = {
        Type = "oneshot";
        ExecStart = "${lib.getExe cfg.package} ingest-agent --since-hours ${toString cfg.agentIngest.lookbackHours}";
      };
    };

    systemd.user.timers.atuout-agent-ingest = lib.mkIf cfg.agentIngest.enable {
      Unit.Description = "Periodically retry recent atuout agent-output imports";
      Timer = {
        OnBootSec = "2m";
        OnUnitActiveSec = cfg.agentIngest.interval;
        Persistent = true;
        Unit = "atuout-agent-ingest.service";
      };
      Install.WantedBy = [ "timers.target" ];
    };
  };
}
