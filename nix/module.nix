{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.services.needledrop;

  settingsFormat = pkgs.formats.yaml { };

  # needledrop writes its samples to /run/needledrop, which RuntimeDirectory provides.
  runtimeDir = "/run/needledrop";
in
{
  options.services.needledrop = {
    enable = lib.mkEnableOption "needledrop, which identifies the record playing on an ALSA input";

    package = lib.mkOption {
      type = lib.types.package;
      defaultText = lib.literalMD "`packages.default` from the needledrop flake";
      description = "The needledrop package to use.";
    };

    settings = lib.mkOption {
      type = lib.types.submodule {
        freeformType = settingsFormat.type;

        options = {
          audio = lib.mkOption {
            type = lib.types.submodule {
              freeformType = settingsFormat.type;

              options = {
                alsa_device = lib.mkOption {
                  type = lib.types.str;
                  example = "dsnoopusb";
                  description = "ALSA PCM to capture from, as passed to `arecord -D`.";
                };

                sample_rate = lib.mkOption {
                  type = lib.types.ints.positive;
                  default = 48000;
                  description = "Capture sample rate in Hz.";
                };

                sample_duration_seconds = lib.mkOption {
                  type = lib.types.ints.positive;
                  default = 12;
                  description = "Seconds of audio captured per cycle.";
                };
              };
            };
            default = { };
            description = "The `audio` section of the configuration.";
          };

          recognition = lib.mkOption {
            type = lib.types.submodule {
              freeformType = settingsFormat.type;

              options = {
                interval_seconds = lib.mkOption {
                  type = lib.types.ints.positive;
                  default = 20;
                  description = "Seconds between recognition cycles.";
                };

                min_track_change_seconds = lib.mkOption {
                  type = lib.types.ints.unsigned;
                  default = 30;
                  description = "Ignore a repeat detection of the same track within this many seconds.";
                };
              };
            };
            default = { };
            description = "The `recognition` section of the configuration.";
          };
        };
      };
      default = { };
      example = lib.literalExpression ''
        {
          audio.alsa_device = "dsnoopusb";
          outputs = {
            log.enabled = true;
            lastfm = {
              enabled = true;
              api_key = "$LASTFM_API_KEY";
              api_secret = "$LASTFM_API_SECRET";
              username = "me";
              password_hash = "$LASTFM_PASSWORD_HASH";
            };
          };
        }
      '';
      description = ''
        needledrop's `config.yaml`, with its own keys; `docs/user-guide.md` lists them all. The rendered file is
        world-readable in the Nix store, so keep secrets in {option}`services.needledrop.secretFiles` and refer to them
        here as `$NAME`.
      '';
    };

    secretFiles = lib.mkOption {
      type = lib.types.attrsOf lib.types.externalPath;
      default = { };
      example = {
        LASTFM_API_SECRET = "/run/secrets/lastfm-api-secret";
      };
      description = ''
        Files that each hold one secret, by name. At each start every `$NAME` in
        {option}`services.needledrop.settings` is replaced with the contents of the file named `NAME`. The files reach
        the service as systemd credentials, never as environment variables of the program. A `$NAME` without a file
        fails the start instead of becoming an empty string. Values are inserted into the YAML verbatim.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = lib.mapAttrsToList (name: _: {
      assertion = builtins.match "[A-Za-z_][A-Za-z0-9_]*" name != null;
      message = "services.needledrop.secretFiles: \"${name}\" is not a valid variable name.";
    }) cfg.secretFiles;

    users.users.needledrop = {
      isSystemUser = true;
      group = "needledrop";
    };
    users.groups.needledrop = { };

    systemd.services.needledrop = {
      description = "needledrop vinyl record identification";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [
        "network-online.target"
        "sound.target"
      ];

      # The secrets enter this script's environment only, not the program's. The rendered file lives on tmpfs and,
      # with the UMask below, is readable by the service user alone.
      preStart = ''
        ${lib.concatMapStrings (name: ''
          export ${name}="$(< "$CREDENTIALS_DIRECTORY/${name}")"
        '') (lib.attrNames cfg.secretFiles)}
        ${lib.getExe pkgs.envsubst} -no-unset \
          -i ${settingsFormat.generate "needledrop.yaml" cfg.settings} \
          -o ${runtimeDir}/config.yaml
      '';

      serviceConfig = {
        ExecStart = "${lib.getExe cfg.package} --config ${runtimeDir}/config.yaml";
        LoadCredential = lib.mapAttrsToList (name: path: "${name}:${path}") cfg.secretFiles;

        # Not DynamicUser: it implies RemoveIPC=, which on stop deletes a dsnoop device's shared memory that this
        # service created while another reader may still be using it.
        User = "needledrop";
        Group = "needledrop";
        SupplementaryGroups = [ "audio" ];
        RuntimeDirectory = "needledrop";
        UMask = "0077";
        Restart = "on-failure";
        RestartSec = 10;

        # Not set: PrivateDevices hides /dev/snd (DeviceAllow admits only ALSA devices instead), PrivateIPC would cut the
        # service off from a dsnoop device shared with other readers, and PrivateUsers drops the audio group.
        DevicePolicy = "closed";
        DeviceAllow = [ "char-alsa rw" ];
        CapabilityBoundingSet = "";
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectKernelLogs = true;
        ProtectControlGroups = true;
        ProtectClock = true;
        ProtectHostname = true;
        ProtectProc = "invisible";
        RestrictNamespaces = true;
        RestrictRealtime = true;
        RestrictSUIDSGID = true;
        LockPersonality = true;
        RestrictAddressFamilies = [
          "AF_UNIX"
          "AF_INET"
          "AF_INET6"
        ];
        SystemCallArchitectures = "native";
        SystemCallFilter = [
          "@system-service"
          "~@privileged"
        ];
      };
    };
  };
}
