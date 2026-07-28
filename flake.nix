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
          version = "0.1.0";
          pyproject = true;

          # Git-tracked source only — never drag generated devenv/mypy state into
          # the store. cleanSource drops .git; the explicit filter drops the rest.
          src = nixpkgs.lib.cleanSourceWith {
            src = ./.;
            filter = path: _type:
              let base = baseNameOf path; in
              !(builtins.elem base [ ".devenv" ".mypy_cache" ".coverage" "result" ]);
          };

          build-system = [ pkgs.python3Packages.hatchling ];

          # Regenerate _pb2.py / _pb2_grpc.py from .proto sources at build time
          # so the gencode version always matches the protobuf runtime in nixpkgs.
          # Use pkgs.protobuf + pkgs.grpc (not grpcio-tools) so protoc and the
          # gRPC plugin match the protobuf/grpcio runtime versions exactly.
          nativeBuildInputs = [
            pkgs.protobuf  # provides protoc
            pkgs.grpc       # provides grpc_python_plugin
          ];

          preBuild = ''
            proto_dir="src/atuout/_proto"
            chmod -R u+w "$proto_dir"

            protoc \
              -I "$proto_dir" \
              --python_out="$proto_dir" \
              --grpc_python_out="$proto_dir" \
              --plugin=protoc-gen-grpc_python="${pkgs.grpc}/bin/grpc_python_plugin" \
              "$proto_dir/semantic.proto" \
              "$proto_dir/history.proto"

            # Rewrite absolute imports to package-relative so
            # `from atuout._proto import ...` works.
            for f in "$proto_dir"/*_pb2_grpc.py; do
              sed -i -E 's/^import (semantic_pb2|history_pb2) as /from atuout._proto import \1 as /' "$f"
            done
          '';

          # Runtime deps mirror pyproject [project.dependencies]. grpcio in
          # nixpkgs already links its native libs, so no LD_LIBRARY_PATH shim is
          # needed at runtime (unlike the devenv, which builds wheels via uv).
          dependencies = with pkgs.python3Packages; [
            pydantic
            grpcio
            protobuf
          ];

          # The test suite spawns a real atuin daemon (needs the ≥18.18.0-beta.2
          # capture service) — not available in the sandbox. Verify with devenv.
          doCheck = false;

          # Smoke-check that the entry point and the force-included zsh hook data
          # file both survive the wheel build.
          pythonImportsCheck = [ "atuout" "atuout.cli" ];

          meta = with nixpkgs.lib; {
            description = "Harvester for Atuin's native command-output captures";
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
