{ self }:

{
  name = "needledrop";

  nodes.machine = {
    imports = [ self.nixosModules.default ];

    # The VM has no sound hardware. This device reads silence from /dev/zero; ALSA's `null` device leaves arecord's
    # buffer as it was, which needledrop then tries to recognise.
    environment.etc."asound.conf".text = ''
      pcm.silence {
        type file
        slave.pcm "null"
        file "/dev/null"
        infile "/dev/zero"
        format "raw"
      }
    '';

    environment.etc."needledrop-test-secret".text = "example.invalid";

    services.needledrop = {
      enable = true;
      secretFiles.TUNESHINE_HOST = "/etc/needledrop-test-secret";
      settings = {
        audio = {
          alsa_device = "silence";
          sample_duration_seconds = 1;
        };
        recognition.interval_seconds = 1;
        outputs = {
          log.enabled = true;
          tuneshine = {
            enabled = false;
            host = "$TUNESHINE_HOST";
          };
        };
      };
    };
  };

  testScript = ''
    machine.wait_for_unit("needledrop.service")
    machine.wait_until_succeeds("test -s /run/needledrop/sample.wav")

    machine.succeed("grep -q example.invalid /run/needledrop/config.yaml")
    machine.fail("grep -q TUNESHINE_HOST /run/needledrop/config.yaml")
    machine.fail("grep -qa TUNESHINE_HOST /proc/$(systemctl show -p MainPID --value needledrop.service)/environ")

    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Started with 1 output(s)'")
    machine.sleep(5)
    machine.fail("journalctl -u needledrop.service | grep -E 'Audio capture failed|Traceback'")
    machine.succeed("systemctl is-active needledrop.service")
  '';
}
