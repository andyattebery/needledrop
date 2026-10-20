{
  description = "needledrop: identifies the record playing on an ALSA input and reports it";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
    }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
      ];
    in
    {
      packages = forAllSystems (system: rec {
        needledrop = nixpkgs.legacyPackages.${system}.callPackage ./nix/package.nix {
          inherit pyproject-nix uv2nix pyproject-build-systems;
        };
        default = needledrop;
      });

      nixosModules.default =
        { pkgs, ... }:
        {
          imports = [ ./nix/module.nix ];
          services.needledrop.package =
            lib.mkDefault
              self.packages.${pkgs.stdenv.hostPlatform.system}.default;
        };

      checks = forAllSystems (
        system:
        {
          package = self.packages.${system}.default;
          inherit (self.packages.${system}.default.passthru.tests) pytest;
        }
        # The VM test needs KVM, which GitHub's arm64 runners don't have.
        // lib.optionalAttrs (system == "x86_64-linux") {
          module-vm-test = nixpkgs.legacyPackages.${system}.testers.runNixOSTest (
            import ./nix/test.nix { inherit self; }
          );
        }
      );
    };
}
