{ pkgs, lib, config, inputs, ... }:

{
  # https://devenv.sh/basics/
  env.GREET = "atuout";

  # https://devenv.sh/packages/
  packages = [
    pkgs.git
    pkgs.uv
    pkgs.jq
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
    uv run mypy
    uv run pytest
  '';

  # See full reference at https://devenv.sh/reference/options/
}
