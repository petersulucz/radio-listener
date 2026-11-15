{
  description = "Continuous GMRS receiver with local transcription and Logship upload";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forSystems = function: nixpkgs.lib.genAttrs systems (system:
        function (import nixpkgs { inherit system; }));
    in {
      packages = forSystems (pkgs: {
        default = pkgs.python3Packages.buildPythonApplication {
          pname = "radio-listener";
          version = "0.1.0";
          src = ./.;
          pyproject = true;
          build-system = [ pkgs.python3Packages.setuptools ];
          dependencies = [
            pkgs.python3Packages.numpy
            pkgs.python3Packages.scipy
            (pkgs.python3Packages.buildPythonPackage {
              pname = "pymodes";
              version = "3.7.0";
              format = "wheel";
              src = pkgs.fetchurl {
                url = "https://files.pythonhosted.org/packages/c3/f8/ec272f1137e37d6e00bc3ae54644c05ea80beea7ed22f613a6114accd861/pymodes-3.7.0-py3-none-any.whl";
                hash = "sha256-eOZOnJrIHG6phrGndynBxRe11iAMF0GMpK4Ns8RHjD0=";
              };
              doCheck = false;
            })
          ];
          pythonImportsCheck = [ "radio_listener" ];
          doCheck = true;
          checkPhase = ''
            runHook preCheck
            ${pkgs.python3.withPackages (ps: [ ps.numpy ps.scipy ])}/bin/python -m unittest discover -s tests -v
            runHook postCheck
          '';
        };
      });

      apps = forSystems (pkgs: {
        listen = {
          type = "app";
          program = "${pkgs.writeShellApplication {
            name = "radio-listener-manual";
            runtimeInputs = [
              self.packages.${pkgs.stdenv.hostPlatform.system}.default
              pkgs.rtl-sdr-blog
              pkgs.dump1090-fa
              pkgs.ffmpeg
              pkgs.whisper-cpp
              pkgs.rtlamr
            ];
            text = ''
              config_dir="''${XDG_CONFIG_HOME:-$HOME/.config}/radio-listener"
              config_file="''${RADIO_LISTENER_CONFIG:-$config_dir/config.toml}"
              token_file="''${RADIO_LISTENER_TOKEN_FILE:-$config_dir/token.env}"
              set -a
              # shellcheck source=/dev/null
              . "$token_file"
              set +a
              exec radio-listener --config "$config_file" "$@"
            '';
          }}/bin/radio-listener-manual";
        };
      });

      nixosModules.default = import ./nix/module.nix { inherit self; };
    };
}
