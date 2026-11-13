{ self }:

{
  name = "needledrop";

  nodes.machine =
    { pkgs, ... }:
    {
      imports = [ self.nixosModules.default ];

      # The test script plays the MP3 stream with curl.
      environment.systemPackages = [ pkgs.curl ];

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
        openFirewall = true;
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
            sendspin.enabled = true;
            mp3_stream.enabled = true;
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

    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Started with 4 output(s)'")
    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Playback started'")

    # Recognition writes its window and, without network, fails with a warning rather than a traceback.
    machine.wait_until_succeeds("test -s /run/needledrop/sample.wav")
    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'Recognition failed'")

    # The OwnTone pipe gets the audio, though the hardened unit can't write anywhere under /srv.
    machine.succeed("test $(timeout 60 head -c 192000 /srv/media/Turntable | wc -c) -eq 192000")

    # The Sendspin output listens, advertises itself over mDNS, and keeps its key readable by its owner only.
    machine.wait_for_open_port(8928)
    machine.wait_until_succeeds("journalctl -u needledrop.service | grep -q 'mDNS advertising client'")
    machine.succeed("test $(stat -c %a /var/lib/needledrop/sendspin-identity.json) = 600")

    # The MP3 stream: ICY metadata only when asked for, a body that runs until the connection closes, and the station
    # name as the title while no track is known.
    machine.wait_for_open_port(8000)
    machine.succeed("curl -s -H 'Icy-MetaData: 1' -D /tmp/h --max-time 5 -o /tmp/s http://127.0.0.1:8000/turntable.mp3 || true")
    machine.succeed("grep -qi '^icy-metaint: 16000' /tmp/h")
    machine.fail("grep -qi '^transfer-encoding' /tmp/h")
    machine.succeed("test $(stat -c %s /tmp/s) -gt 50000")
    machine.succeed("grep -qa \"StreamTitle='Turntable'\" /tmp/s")
    machine.succeed("curl -s -D /tmp/h2 --max-time 2 -o /dev/null http://127.0.0.1:8000/turntable.mp3 || true")
    machine.fail("grep -qi '^icy-metaint' /tmp/h2")

    machine.sleep(5)
    machine.fail("journalctl -u needledrop.service | grep -E 'Audio capture failed|Traceback'")
    machine.succeed("systemctl is-active needledrop.service")
  '';
}
