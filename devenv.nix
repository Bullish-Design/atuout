{ pkgs, lib, config, inputs, ... }:

let
  # atuin main requires rustc >= 1.97; build it with a current stable toolchain from
  # rust-overlay rather than the pinned nixpkgs' older rustc.
  rustPkgs = import inputs.nixpkgs {
    inherit (pkgs) system;
    overlays = [ (import inputs.rust-overlay) ];
  };
  rustToolchain = rustPkgs.rust-bin.stable.latest.default;
  rustPlatform = rustPkgs.makeRustPlatform {
    cargo = rustToolchain;
    rustc = rustToolchain;
  };
  # Build atuin from the pinned source (atuin.nix), so we get PR #3510's Semantic
  # capture service that released atuin lacks. Cached after the first build.
  atuinLatest = pkgs.callPackage "${inputs.atuin-src}/atuin.nix" { inherit rustPlatform; };
in
{
  # https://devenv.sh/basics/
  env.GREET = "atuout";

  # https://devenv.sh/packages/
  packages = [
    pkgs.git
    pkgs.uv
    pkgs.jq
    atuinLatest  # atuin built from source w/ PR #3510 — integration test needs the Semantic service
    ];

  # grpcio ships a C extension that dlopen's libstdc++ at import time; expose it
  # (and zlib, needed by grpcio-tools' protoc) so imports work in the venv.
  env.LD_LIBRARY_PATH = lib.makeLibraryPath [
    pkgs.stdenv.cc.cc.lib
    pkgs.zlib
  ];

  # https://devenv.sh/languages/
  languages = {
      python = {
          enable = true;
          version = "3.13";
          venv.enable = true;
          uv.enable = true;
        };
    };

  # https://devenv.sh/scripts/
  scripts.hello.exec = ''
    echo hello from $GREET
  '';

  # devman — the automation plane (CONCEPT.md §5). `base` alone: this repository
  # ships no scheduled work and writes none of its own files.
  devman = {
    enable = true;
    project = "atuout";
    groups = [ "base" ];
  };

  # https://devenv.sh/tasks/
  #
  # The two task names the `base` group calls (groups/base/README.md). devenv
  # owns each implementation; Dagu owns the composition (§6).
  tasks = {
    "atuout:lint".exec = "uv run ruff check src tests";
    "atuout:test".exec = "uv run pytest";

    "base:check".after = [ "atuout:lint" ];
    "base:test".after = [ "atuout:test" ];
  };

  enterShell = ''
    hello
    git --version
  '';

  # https://devenv.sh/tests/
  enterTest = ''
    echo "Running tests"
    git --version | grep --color=auto "${pkgs.git.version}"
    uv sync --extra dev
    uv run ruff check src tests
    uv run ty check
    uv run pytest
  '';

  # See full reference at https://devenv.sh/reference/options/
}
