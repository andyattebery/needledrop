{ self }:

{
  name = "needledrop";

  nodes.machine = {
    imports = [ self.nixosModules.default ];

    # The VM has no sound hardware. snd-dummy is a sound card paced by a kernel timer, and this device swaps what it
    # captures for /dev/urandom: noise loud enough to start playback, so recognition runs (and fails, with no network
    # here) and the OwnTone pipe gets audio. ALSA's `null` device can't stand in: it isn't paced, and a continuous
    # capture from it reads as fast as the CPU allows.
    boot.kernelModules = [ "snd-dummy" ];
    environment.etc."asound.conf".text = ''
      pcm.noise {
        type file
        slave.pcm "hw:Dummy"
        file "/dev/null"
        infile "/dev/urandom"
        format "raw"
      }
    '';

    # OwnTone's pipe. The test script reads it in OwnTone's place.
    systemd.tmpfiles.rules = [
      "d /srv/media 0755 root root -"
      "p /srv/media/Turntable 0666 root root -"
    ];

    environment.etc."needledrop-test-secret".text = "example.invalid";

    services.needledrop = {
      enable = true;
      secretFiles.TUNESHINE_HOST = "/etc/needledrop-test-secret";
      settings = {
        audio = {
          alsa_device = "noise";
          sample_duration_seconds = 1;
        };
        recognition.interval_seconds = 1;
        outputs = {
          log.enabled = true;
          owntone_audio = {
            enabled = true;
            audio_fifo = "/srv/media/Turntable";
          };
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

    machine.succeed("grep -q example.invalid /run/needledrop/config.yaml")
    machine.fail("grep -q TUNESHINE_HOST /run/needledrop/config.yaml")
    machine.fail("grep -qa TUNESHINE_HOST /proc/$(systemctl show -p MainPID --value needledrop.service)/environ")

    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Started with 2 output(s)'")
    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Playback started'")

    # Recognition writes its window and, without network, fails with a warning rather than a traceback.
    machine.wait_until_succeeds("test -s /run/needledrop/sample.wav")
    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Recognition failed'")

    # The OwnTone pipe gets the audio, though the hardened unit can't write anywhere under /srv.
    machine.succeed("test $(timeout 60 head -c 192000 /srv/media/Turntable | wc -c) -eq 192000")

    machine.sleep(5)
    machine.fail("journalctl -u needledrop.service | grep -E 'Audio capture failed|Traceback'")
    machine.succeed("systemctl is-active needledrop.service")
  '';
}
