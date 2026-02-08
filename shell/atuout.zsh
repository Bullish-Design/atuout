#!/usr/bin/env zsh
# atuout — zsh integration hook
#
# Usage: add to your .zshrc:
#   eval "$(atuout init-zsh)"
#
# This hooks into zsh's preexec/precmd cycle to record every command
# via asciinema and (optionally) link it to the Atuin history entry.

# ── configuration ────────────────────────────────────────────────────
: "${ATUOUT_DATA_DIR:=${HOME}/.local/share/atuout/recordings}"
: "${ATUOUT_ENABLED:=1}"

# ── state ────────────────────────────────────────────────────────────
typeset -g _atuout_command=""
typeset -g _atuout_atuin_id=""
typeset -g _atuout_cast_file=""
typeset -g _atuout_recording_pid=""

# ── helpers ──────────────────────────────────────────────────────────

_atuout_get_atuin_id() {
    # Atuin sets ATUIN_HISTORY_ID for the current command
    if [[ -n "${ATUIN_HISTORY_ID:-}" ]]; then
        echo "${ATUIN_HISTORY_ID}"
        return
    fi
    echo ""
}

_atuout_cast_path() {
    local ts atuin_id stem
    ts=$(date +%s%3N 2>/dev/null || python3 -c 'import time; print(int(time.time()*1000))')
    atuin_id="$1"
    if [[ -n "$atuin_id" ]]; then
        stem="${ts}_${atuin_id}"
    else
        stem="${ts}"
    fi
    echo "${ATUOUT_DATA_DIR}/${stem}.cast"
}

# ── hooks ────────────────────────────────────────────────────────────

_atuout_preexec() {
    [[ "$ATUOUT_ENABLED" != "1" ]] && return

    _atuout_command="$1"
    _atuout_atuin_id="$(_atuout_get_atuin_id)"

    mkdir -p "$ATUOUT_DATA_DIR"
    _atuout_cast_file="$(_atuout_cast_path "$_atuout_atuin_id")"

    # Start asciinema recording in the background.
    # We use script-level recording: asciinema records the command
    # in a subshell so we don't block the user's shell.
    asciinema rec \
        --overwrite \
        -c "$_atuout_command" \
        "$_atuout_cast_file" &>/dev/null &
    _atuout_recording_pid=$!
}

_atuout_precmd() {
    local exit_code=$?

    [[ "$ATUOUT_ENABLED" != "1" ]] && return
    [[ -z "$_atuout_command" ]] && return

    # Wait for the recording to finish (it should already be done
    # since the command itself has returned).
    if [[ -n "$_atuout_recording_pid" ]]; then
        wait "$_atuout_recording_pid" 2>/dev/null
    fi

    # Stamp exit code into a sidecar so we can retrieve it later
    # (asciinema doesn't always write exit_code into the header).
    if [[ -n "$_atuout_cast_file" && -f "$_atuout_cast_file" ]]; then
        local meta_file="${_atuout_cast_file%.cast}.meta"
        printf '{"exit_code":%d,"command":%s,"atuin_id":%s}\n' \
            "$exit_code" \
            "$(printf '%s' "$_atuout_command" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')" \
            "$(printf '%s' "$_atuout_atuin_id" | python3 -c 'import json,sys; v=sys.stdin.read(); print(json.dumps(v) if v else "null")')" \
            > "$meta_file"
    fi

    # Reset state
    _atuout_command=""
    _atuout_atuin_id=""
    _atuout_cast_file=""
    _atuout_recording_pid=""
}

# ── register hooks ───────────────────────────────────────────────────
autoload -Uz add-zsh-hook
add-zsh-hook preexec _atuout_preexec
add-zsh-hook precmd  _atuout_precmd
