{
  lib,
  stdenv,
  callPackage,
  callPackages,
  runCommand,
  makeWrapper,
  python313,
  alsa-utils,
  ffmpeg-headless,
  pyproject-nix,
  uv2nix,
  pyproject-build-systems,
}:

let
  workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ../.; };

  # Not python3: shazamio-core 1.1.2's newest wheels are cp313. Keep in step with .python-version and the Dockerfile.
  python = python313;

  pythonSet = (callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
    lib.composeManyExtensions [
      pyproject-build-systems.overlays.wheel
      (workspace.mkPyprojectOverlay { sourcePreference = "wheel"; })
      (_final: prev: {
        # Only what the build reads, so unrelated edits don't rebuild it.
        needledrop = prev.needledrop.overrideAttrs {
          src = lib.fileset.toSource {
            root = ../.;
            fileset = lib.fileset.unions [
              ../pyproject.toml
              ../LICENSE
              ../needledrop
            ];
          };
        };
      })
    ]
  );

  inherit (callPackages pyproject-nix.build.util { }) mkApplication;

  # nixpkgs' alsa-utils loads every ALSA plugin it packages (pipewire, gstreamer, ffmpeg, …): an 812 MiB closure against
  # 90 MiB without them. Capture from hw, dsnoop and the other built-in device types needs none of them.
  alsa-utils-no-plugins = alsa-utils.override {
    withPipewireLib = false;
    alsa-plugins = runCommand "no-alsa-plugins" { } "mkdir -p $out/lib/alsa-lib";
  };

  # Without needledrop/ in the source, the tests can only import the installed package.
  pytest = stdenv.mkDerivation {
    name = "${pythonSet.needledrop.name}-pytest";
    src = lib.fileset.toSource {
      root = ../.;
      fileset = lib.fileset.unions [
        ../pyproject.toml
        ../tests
      ];
    };
    nativeBuildInputs = [ (pythonSet.mkVirtualEnv "needledrop-test-env" { needledrop = [ "dev" ]; }) ];
    dontConfigure = true;
    buildPhase = ''
      runHook preBuild
      pytest tests
      runHook postBuild
    '';
    installPhase = ''
      runHook preInstall
      touch $out
      runHook postInstall
    '';
  };

  venv = pythonSet.mkVirtualEnv "needledrop-env" workspace.deps.default;
in
(mkApplication {
  inherit venv;
  package = pythonSet.needledrop;
}).overrideAttrs
  (old: {
    nativeBuildInputs = old.nativeBuildInputs ++ [ makeWrapper ];
    # As in the Docker image: arecord for capture, ffmpeg for pydub's fallback decoder.
    # Wrap the venv's script itself: argparse names the program after the file Python runs, and wrapProgram renames it.
    postInstall = (old.postInstall or "") + ''
      rm $out/bin/needledrop
      makeWrapper ${venv}/bin/needledrop $out/bin/needledrop \
        --prefix PATH : ${
          lib.makeBinPath [
            alsa-utils-no-plugins
            ffmpeg-headless
          ]
        }
    '';
    passthru = old.passthru // {
      tests = (old.passthru.tests or { }) // {
        inherit pytest;
      };
    };
    meta = old.meta // {
      description = "Identifies the record playing on an ALSA input and reports it to Last.fm, OwnTone and Tuneshine";
      homepage = "https://github.com/andyattebery/needledrop";
      license = lib.licenses.mit;
      mainProgram = "needledrop";
      platforms = lib.platforms.linux;
    };
  })
