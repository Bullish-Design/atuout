{ pkgs, config, ... }:
{
  # https://devenv.sh/basics/
  env.GREET = "atuout";

  # https://devenv.sh/packages/
  packages = [
    pkgs.git
    pkgs.uv
    pkgs.jq
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
