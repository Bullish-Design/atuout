{
  description = "atuout — durable archiver for Atuin's native command-output captures";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs = { self, nixpkgs, ... }:
    let
      # Single-arch for now (matches nix-meta); widen this list if atuout ever
      # needs to build on another platform.
      systems = [ "x86_64-linux" ];
      forAllSystems = f: nixpkgs.lib.genAttrs systems (system: f {
        inherit system;
        pkgs = nixpkgs.legacyPackages.${system};
      });
    in
    {
      packages = forAllSystems ({ pkgs, ... }: rec {
        atuout = pkgs.python3Packages.buildPythonApplication {
          pname = "atuout";
          version = "0.2.0";
          pyproject = true;

          # Git-tracked source only — never drag generated devenv/ty state into
          # the store. cleanSource drops .git; the explicit filter drops the rest.
          src = nixpkgs.lib.cleanSourceWith {
            src = ./.;
            filter = path: _type:
              let base = baseNameOf path; in
              !(builtins.elem base [ ".devenv" ".coverage" "result" ]);
          };

          build-system = [ pkgs.python3Packages.hatchling ];

          dependencies = with pkgs.python3Packages; [ pydantic ];

          # Run the test suite in devenv with its development dependencies.
          doCheck = false;

          # Smoke-check the importable CLI package.
          pythonImportsCheck = [ "atuout" "atuout.cli" ];

          meta = with nixpkgs.lib; {
            description = "Agent command-output store and transcript importer for Atuin history";
            license = licenses.mit;
            mainProgram = "atuout";
            platforms = platforms.linux;
          };
        };

        default = atuout;
      });

      homeManagerModules = {
        atuout = import ./nix/hm-module.nix self;
        default = self.homeManagerModules.atuout;
      };
    };
}
