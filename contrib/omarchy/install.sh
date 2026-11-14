#!/usr/bin/env bash
# Install or remove the wsturgiss.whoop Omarchy bar widget.
#
#   contrib/omarchy/install.sh              install (or update) and enable it
#   contrib/omarchy/install.sh --uninstall  disable and remove it
#
# Install copies contrib/omarchy/wsturgiss.whoop/ to
# ~/.config/omarchy/plugins/wsturgiss.whoop/ (Omarchy refuses symlinked
# plugins), rescans the shell's plugins and, if the widget isn't already
# enabled, enables it in the bar's right section before omarchy.audio.
# Running it again only refreshes the copied files.
#
# Uninstall runs `omarchy plugin remove`, which disables the widget and moves
# the installed folder to ~/.config/omarchy/plugins/.wsturgiss.whoop.bak.<time>.
#
# Enabling and disabling rewrite ~/.config/omarchy/shell.json, so it is copied
# to ~/.config/omarchy/shell.json.bak.<unix time> first.
#
# The widget only reads the local API; the data plane is contrib/install.sh --api.
set -euo pipefail

ID="wsturgiss.whoop"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$ID"
PLUGINS_DIR="$HOME/.config/omarchy/plugins"
TARGET="$PLUGINS_DIR/$ID"
SHELL_JSON="$HOME/.config/omarchy/shell.json"

fail() { echo "install.sh: $*" >&2; exit 1; }

command -v omarchy >/dev/null || fail "omarchy not found; this installs an Omarchy shell plugin"
command -v jq >/dev/null || fail "jq not found"

backup_shell_json() {
  [[ -f $SHELL_JSON ]] || return 0
  local dest="$SHELL_JSON.bak.$(date +%s)"
  cp -p "$SHELL_JSON" "$dest"
  echo "Backed up shell.json to $dest"
}

plugin_enabled() {
  omarchy plugin list --json | jq -e --arg id "$ID" 'any(.[]; .id == $id and .enabled)' >/dev/null
}

# A folder at the target that this script didn't put there (another plugin,
# a git checkout) is left alone.
check_target_is_ours() {
  [[ -e $TARGET || -L $TARGET ]] || return 0
  [[ ! -L $TARGET ]] || fail "$TARGET is a symlink; remove it first"
  [[ ! -d $TARGET/.git ]] || fail "$TARGET is a git checkout; use omarchy plugin remove $ID first"
  [[ $(jq -r '.id // ""' "$TARGET/manifest.json" 2>/dev/null) == "$ID" ]] ||
    fail "$TARGET exists but isn't this plugin; not touching it"
}

install_plugin() {
  omarchy plugin validate "$SRC" || fail "plugin folder failed validation"
  check_target_is_ours
  mkdir -p "$PLUGINS_DIR"

  # Stage, then swap, so the shell's file watcher never sees a half-copied folder.
  local stage="$PLUGINS_DIR/.$ID.new"
  rm -rf "$stage"
  cp -r "$SRC" "$stage"
  rm -rf "$TARGET"
  mv "$stage" "$TARGET"
  echo "Copied $ID to $TARGET"

  omarchy-shell shell rescanPlugins >/dev/null

  if plugin_enabled; then
    echo "$ID is already enabled; files updated (the shell reloads them on change)"
  else
    backup_shell_json
    if jq -e '.bar.layout.right | any(.id == "omarchy.audio")' "$SHELL_JSON" >/dev/null 2>&1; then
      omarchy plugin enable "$ID" --section right --before omarchy.audio
    else
      omarchy plugin enable "$ID" --section right
    fi
  fi

  curl -s -m 2 -o /dev/null "http://127.0.0.1:${WHOOP_API_PORT:-8787}/healthz" ||
    echo "Note: the whoop-poc API isn't answering; the widget shows offline until it does (contrib/install.sh --api, then systemctl --user start whoop-api)."
}

uninstall_plugin() {
  if [[ ! -e $TARGET && ! -L $TARGET ]]; then
    echo "$ID is not installed"
    return 0
  fi
  check_target_is_ours
  backup_shell_json
  omarchy plugin remove "$ID" --yes
}

case "${1:-}" in
  "" | --install) install_plugin ;;
  --uninstall | --remove) uninstall_plugin ;;
  -h | --help) sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//' ;;
  *) fail "unknown option: $1 (use --uninstall to remove)" ;;
esac
