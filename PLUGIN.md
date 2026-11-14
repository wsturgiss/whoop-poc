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
| local JSON API (opt-in, `--api`) | `~/.config/systemd/user/whoop-api.service` → `whoop-poc serve` on 127.0.0.1:8787 | `contrib/systemd/`, [API.md](API.md) |
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
Widget.qml           polls GET http://127.0.0.1:8787/v1/status every few s (XMLHttpRequest)
                     and /v1/report?hours=24&bins=<px> less often (API.md)
                     bar text:  ♥ 63 · 7h12m   (hr.latest.bpm · sleep.sessions[-1].asleep_min)
                     dims / shows ⚠ when status.data.age_min > 60 or status.sync.last.outcome != complete
                     ⟳ while status.sync.running; click → launch `whoop-poc` (TUI) in a floating terminal
Overlay.qml          (later) live HR: POST /v1/live/start, poll /v1/live?session=…&since=…
Preferences.qml      refresh interval, show sleep, staleness threshold
```

It reads the **local API** ([API.md](API.md)): `whoop-poc serve` (the `whoop-api` user unit) on
127.0.0.1. QML can't talk to a unix socket, so the API is plain HTTP JSON, and the widget can also start
a sync or a live session with the same POSTs the TUI uses. `coach.json` stays the [COACH.md](COACH.md)
contract for Coach. The API's `/v1/summary` is the same document, so the widget can fall back to reading
the file when the API isn't running. No BLE, SQLite or Python happens inside the shell. If the data plane
isn't installed, the widget shows "whoop-poc not installed" and points to `contrib/install.sh --api`.

Order of work: (1) QML widget reading the API (falling back to `coach.json`), cloned from a first-party bar
widget via `omarchy plugin clone`. (2) `omarchy plugin validate`. (3) `omarchy plugin add` from the git URL.
(4) Optionally an Omarchy menu entry / `omarchy hook` that runs `contrib/install.sh`.

## Deliberately not done

- Packaging NOOP itself (AUR or similar). It's upstream's tool. We depend on a checkout.
- A root/system install. Everything is per-user, which also keeps biometrics in `$HOME`.
- Live (realtime) HR in the bar. The strap is drained in batches, so "latest" is as old as the last sync.
  `whoop-sync realtime` exists, but holding a BLE link all day costs strap battery.
