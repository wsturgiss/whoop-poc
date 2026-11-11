# Toward an Omarchy plugin

On Omarchy, "plugin" means an **Omarchy shell plugin**: a folder with a `manifest.json` (+ QML entry
points), installed from git with `omarchy plugin add <git-url>` into `~/.config/omarchy/plugins/<id>/`,
and validated with `omarchy plugin validate <folder>`. Kinds include `bar-widget`, `overlay` and
`service`. A plugin is UI. It can't install Python tools or systemd units on its own, so whoop-poc ships
in two layers.

## Layer 1: the data plane (exists now)

`contrib/install.sh --address <MAC>` sets up, per user and without root:

| Piece | Installed at | Source |
|---|---|---|
| `whoop-poc` CLI/TUI | `~/.local/bin/whoop-poc` (`uv tool install --editable`) | this repo |
| `whoop-sync` wrapper | `~/.local/bin/whoop-sync` → `contrib/whoop-sync` | this repo |
| sync service + timer | `~/.config/systemd/user/whoop-sync.{service,timer}` | `contrib/systemd/` |
| strap config | `~/.config/whoop-poc/env` (`WHOOP_ADDRESS=…`, mode 600) | written once, never in git |
| summary / archive / sync status | `~/.local/state/whoop-poc/{coach.json,daily/,sync.json,last-sync.log}` | written by the service |

External prerequisites, not vendored: a NOOP checkout with `tools/linux-capture` (its venv),
`whoop-decode` built, and optionally `stager/` built for 4-stage sleep (see README). The strap is paired
in `bluetoothctl` with the phone's Bluetooth off.

## Layer 2: the Omarchy plugin (next, small)

A `bar-widget` plugin, id e.g. `wsturgiss.whoop`, in its own repo or a `omarchy-plugin/` subfolder
published as one. `plugin add` expects the manifest at the repo root.

```
manifest.json        kinds: ["bar-widget"], entryPoints.barWidget: "Widget.qml"
Widget.qml           reads ~/.local/state/whoop-poc/coach.json every N s
                     bar text:  ♥ 63 · 7h12m   (latest_hr.bpm · days[-1].sleep.asleep_min)
                     dims / shows ⚠ when freshness.data_age_min > 60 or last_sync.outcome != complete
                     click → launch `whoop-poc` (TUI) in a floating terminal
Preferences.qml      refresh interval, show sleep, staleness threshold
```

It reads **only `coach.json`** (the [COACH.md](COACH.md) contract), so the widget, Coach and anything else
are all readers of one documented file. No BLE, SQLite or Python happens inside the shell. If the
data plane isn't installed, the widget shows "whoop-poc not installed" and points to `contrib/install.sh`.

Order of work: (1) QML widget reading `coach.json`, cloned from a first-party bar widget via
`omarchy plugin clone`. (2) `omarchy plugin validate`. (3) `omarchy plugin add` from the git URL.
(4) Optionally an Omarchy menu entry / `omarchy hook` that runs `contrib/install.sh`.

## Deliberately not done

- Packaging NOOP itself (AUR or similar). It's upstream's tool. We depend on a checkout.
- A root/system install. Everything is per-user, which also keeps biometrics in `$HOME`.
- Live (realtime) HR in the bar. The strap is drained in batches, so "latest" is as old as the last sync.
  `whoop-sync realtime` exists, but holding a BLE link all day costs strap battery.
