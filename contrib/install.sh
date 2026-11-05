#!/usr/bin/env bash
# Install whoop-poc for the current user (no root):
#   ~/.local/bin/whoop-poc           CLI/TUI (uv tool, editable → follows this checkout)
#   ~/.local/bin/whoop-sync          sync wrapper (symlink into contrib/)
#   ~/.config/systemd/user/whoop-sync.{service,timer}
#   ~/.config/whoop-poc/env          created from --address on first install; never overwritten
# Usage: contrib/install.sh [--address AA:BB:CC:DD:EE:FF] [--no-timer]
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
repo=$(dirname "$here")
addr="" timer=1
while [ $# -gt 0 ]; do
  case "$1" in
    --address) addr=$2; shift 2 ;;
    --no-timer) timer=0; shift ;;
    *) echo "unknown arg: $1" >&2; exit 2 ;;
  esac
done
bin=$HOME/.local/bin
units=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
conf=${XDG_CONFIG_HOME:-$HOME/.config}/whoop-poc
mkdir -p "$bin" "$units" "$conf"

uv tool install --force --editable "$repo"
ln -sfn "$here/whoop-sync" "$bin/whoop-sync"
install -m 644 "$here/systemd/whoop-sync.service" "$here/systemd/whoop-sync.timer" "$units/"

if [ ! -f "$conf/env" ]; then
  [ -n "$addr" ] || { echo "first install: pass --address <strap MAC> (bluetoothctl devices)" >&2; exit 2; }
  umask 077
  cat > "$conf/env" <<EOF
WHOOP_ADDRESS=$addr
# WHOOP_CAPTURE_DIR=\$HOME/src/noop/tools/linux-capture
# WHOOP_DB=captures/whoop.db
# WHOOP_SYNC_MAX=1200
EOF
fi

systemctl --user daemon-reload
if [ "$timer" = 1 ]; then systemctl --user enable --now whoop-sync.timer; fi
echo "installed. status: systemctl --user list-timers whoop-sync.timer; snapshot: ~/.local/state/whoop-poc/coach.json"
