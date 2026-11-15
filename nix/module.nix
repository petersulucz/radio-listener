{ self }:
{ config, lib, pkgs, ... }:
let
  cfg = config.services.radio-listener;
  toml = pkgs.formats.toml { };
  whisperModel = pkgs.fetchurl {
    url = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.en.bin";
    hash = "sha256-oDd5yG3zMjB19eeWyyzlAp8A7Ihp7uP9+4l6/jbG0AI=";
  };
  generatedConfig = toml.generate "radio-listener.toml" {
    spool_dir = "/var/lib/radio-listener/spool";
    model_path = if cfg.mode == "gmrs" then cfg.modelPath else "/dev/null";
    radio = {
      device_index = cfg.deviceIndex;
      gain_db = cfg.gainDb;
      ppm = cfg.ppm;
      squelch_db = cfg.squelchDb;
    };
    recording.max_spool_bytes = cfg.maxSpoolBytes;
    logship = {
      url = cfg.logshipUrl;
      account_id = cfg.accountId;
      schema = cfg.schema;
      adsb_schema = cfg.adsbSchema;
      meter_schema = cfg.meterSchema;
    };
    meter = {
      enabled = cfg.mode == "gmrs" && cfg.meter.enable;
      interval_seconds = cfg.meter.intervalSeconds;
      window_seconds = cfg.meter.windowSeconds;
      id = cfg.meter.id;
    };
  };
in {
  options.services.radio-listener = {
    enable = lib.mkEnableOption "continuous 462 MHz GMRS listening";
    package = lib.mkOption {
      type = lib.types.package;
      default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "Radio listener application package.";
    };
    sdrPackage = lib.mkOption {
      type = lib.types.package;
      default = pkgs.rtl-sdr;
      description = "RTL-SDR driver package. Use pkgs.rtl-sdr-blog for a Blog V4 device.";
    };
    logshipUrl = lib.mkOption { type = lib.types.str; };
    accountId = lib.mkOption { type = lib.types.str; };
    schema = lib.mkOption { type = lib.types.str; default = "gmrs.transmissions"; };
    adsbSchema = lib.mkOption { type = lib.types.str; default = "adsb.messages"; };
    meterSchema = lib.mkOption { type = lib.types.str; default = "meters.readings"; };
    meter = {
      enable = lib.mkEnableOption "periodic SCM/SCM+ meter scans between GMRS windows";
      id = lib.mkOption {
        type = lib.types.str;
        default = "";
        description = "Decimal meter endpoint ID. Empty scans for candidate IDs but uploads no readings.";
      };
      intervalSeconds = lib.mkOption { type = lib.types.float; default = 300.0; };
      windowSeconds = lib.mkOption { type = lib.types.float; default = 30.0; };
    };
    mode = lib.mkOption {
      type = lib.types.enum [ "gmrs" "adsb" ];
      default = "gmrs";
      description = "Radio receiver mode. One RTL-SDR can run only one mode at a time.";
    };
    tokenFile = lib.mkOption {
      type = lib.types.str;
      description = "File containing LOGSHIP_TOKEN=...; keep it outside the Nix store.";
    };
    modelPath = lib.mkOption {
      type = lib.types.str;
      default = toString whisperModel;
      description = "Whisper model for GMRS transcription. Defaults to a checksum-verified download in the Nix store.";
    };
    deviceIndex = lib.mkOption { type = lib.types.int; default = 0; };
    gainDb = lib.mkOption { type = lib.types.float; default = 30.0; };
    ppm = lib.mkOption { type = lib.types.int; default = 0; };
    squelchDb = lib.mkOption { type = lib.types.float; default = 12.0; };
    maxSpoolBytes = lib.mkOption { type = lib.types.int; default = 5000000000; };
  };

  config = lib.mkIf cfg.enable {
    users.groups.plugdev = { };
    users.groups.radio-listener = { };
    users.users.radio-listener = {
      isSystemUser = true;
      group = "radio-listener";
      extraGroups = [ "plugdev" ];
    };
    services.udev.packages = [ cfg.sdrPackage ];
    boot.blacklistedKernelModules = [ "dvb_usb_rtl28xxu" ];

    systemd.services.radio-listener = {
      description = "GMRS, meter, or Mode S radio listener";
      wantedBy = [ "multi-user.target" ];
      after = [ "network-online.target" ];
      wants = [ "network-online.target" ];
      path = [ cfg.sdrPackage pkgs.dump1090-fa ]
        ++ lib.optionals (cfg.mode == "gmrs") [ pkgs.ffmpeg pkgs.whisper-cpp ]
        ++ lib.optionals (cfg.mode == "gmrs" && cfg.meter.enable) [ pkgs.rtlamr ];
      serviceConfig = {
        Type = "simple";
        User = "radio-listener";
        Group = "radio-listener";
        StateDirectory = "radio-listener";
        WorkingDirectory = "/var/lib/radio-listener";
        EnvironmentFile = cfg.tokenFile;
        ExecStart = "${lib.getExe' cfg.package "radio-listener"} --config ${generatedConfig} --mode ${cfg.mode}";
        Restart = "always";
        RestartSec = 5;
        ProtectSystem = "strict";
        ProtectHome = true;
        NoNewPrivileges = true;
      };
    };
  };
}
