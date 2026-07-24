#!/usr/bin/env zsh
# atuout — zsh integration hook
#
# Usage: add to your .zshrc, AFTER atuin's pty-proxy init:
#   eval "$(atuin pty-proxy init zsh)"   # must come first — wraps the shell
#   eval "$(atuout init-zsh)"            # harvests captures via the daemon
#
# atuout harvests Atuin's native command-output captures (OSC 133, via
# `atuin pty-proxy`) into its own durable SQLite store, keyed by ATUIN_HISTORY_ID.

# ── hard requirement: must run inside `atuin pty-proxy` ──────────────
if [[ -z "${ATUIN_PTY_PROXY_ACTIVE:-}" ]]; then
    print -u2 "atuout: requires 'atuin pty-proxy'. Add BEFORE atuout's init in ~/.zshrc:"
    print -u2 '  eval "$(atuin pty-proxy init zsh)"'
    print -u2 '  eval "$(atuout init-zsh)"'
    return 1
fi

# ── configuration ────────────────────────────────────────────────────
: "${ATUOUT_ENABLED:=1}"

# ── state ────────────────────────────────────────────────────────────
typeset -g _atuout_command=""

# ── start the reconciler once (system-wide; no-op if already running) ─
atuout reconcile ensure &>/dev/null

# ── one-time soft capability check: warn (to stderr) if the reachable atuin daemon
#    is too old to capture output. Backgrounded so it never delays shell startup. ─
atuout check &!

# ── hooks ────────────────────────────────────────────────────────────

_atuout_preexec() {
    _atuout_command="$1"
}

_atuout_precmd() {
    local exit_code=$?
    [[ "$ATUOUT_ENABLED" != "1" ]] && return

    local id="${ATUIN_HISTORY_ID:-}"
    if [[ -z "$id" ]]; then
        _atuout_command=""
        return
    fi

    # Fast path: detached + disowned so it never blocks the prompt.
    atuout harvest "$id" --command "$_atuout_command" --exit-code "$exit_code" &>/dev/null &!

    _atuout_command=""
}

# ── register hooks ───────────────────────────────────────────────────
autoload -Uz add-zsh-hook
add-zsh-hook preexec _atuout_preexec
add-zsh-hook precmd  _atuout_precmd
