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

## Layer 2: the Omarchy bar widget (`contrib/omarchy/wsturgiss.whoop/`)

A `bar-widget` plugin, id `wsturgiss.whoop`. It reads only the local API ([API.md](API.md)) on
127.0.0.1; no BLE, SQLite or Python runs inside the shell.

```bash
contrib/install.sh --api                  # the data plane + whoop-api unit, if not done yet
systemctl --user enable --now whoop-api
contrib/omarchy/install.sh                # install or update the widget and enable it in the bar
contrib/omarchy/install.sh --uninstall    # disable and remove it
node contrib/omarchy/test_model.mjs       # tests for the widget's pure logic (Model.js)
```

`install.sh` copies the folder to `~/.config/omarchy/plugins/wsturgiss.whoop/` (Omarchy rejects
symlinks inside or as a plugin), runs `omarchy-shell shell rescanPlugins`, and, unless it is already
enabled, `omarchy plugin enable wsturgiss.whoop --section right --before omarchy.audio`. Enabling and
disabling rewrite `~/.config/omarchy/shell.json`, so the script copies it to `shell.json.bak.<unix time>`
first. Re-running it only refreshes the files. `--uninstall` runs `omarchy plugin remove wsturgiss.whoop
--yes`, which disables the widget and moves the folder to `plugins/.wsturgiss.whoop.bak.<time>`. It refuses
to touch a target folder that isn't this plugin (another id, a git checkout, a symlink).

The shell hot-reloads a changed plugin, but `Model.js` is a `.pragma library` script, which the QML engine
keeps cached, and a reload can leave the old IPC target registered. After updating, run
`omarchy restart shell` if the change doesn't show.

### Files

```
manifest.json   kinds ["bar-widget"], entryPoints.barWidget "Panel.qml", barWidget.defaults + schema
                (settings: refreshIntervalSec 15, staleMinutes 60, showSleep, liveMinutes 30, apiPort 8787)
Panel.qml       extends qs.Ui Panel: BarIconButton in the bar + a KeyboardPanel popup (the same pattern as
                the first-party omarchy.power widget)
Model.js        pure functions: bar text/state, panel rows, poll interval, when to refetch the summary
```

### Behaviour

- **Bar:** `<latest bpm> · <last sleep asleep time>` and a state icon: heart (fresh), sync icon plus the
  drain's `%` (syncing), alert icon and dimmed (data older than `staleMinutes`), heart-pulse in the urgent
  colour with the live bpm (live session), heart-off, `--` and dimmed (API not answering). The latest bpm is
  the summary's `latest_hr`, i.e. as old as the last sync; the live bpm is `status.live.bpm`.
- **Polling:** `GET /v1/status` every `refreshIntervalSec` (5 s while a sync drains, 2 s while a live
  session runs). `GET /v1/summary` (the heavy one, ~4 s) only when `status.data.newest_unix` passes the
  summary's `cursor_unix`, when the panel opens, and at least every 15 min; never more than once a minute
  unless the panel forces it. Requests use QML `XMLHttpRequest` with a watchdog (3 s status, 120 s summary,
  10 s actions); responses over 2 MB are ignored, and all text is drawn as `Text.PlainText`.
- **Panel** (left click): sync line, live line, last sleep (asleep, window, efficiency, stages, RHR), last
  workout (time, length, avg/max, zone minutes), max HR and its source, zone bounds, strap battery, data age.
  Buttons: Sync (`POST /v1/sync`), Live N m / Stop live (`POST /v1/live/start {"minutes": N}`,
  `/v1/live/stop`), Open TUI. With the API down: Start API (`systemctl --user start whoop-api`) and Open TUI.
  POSTs send `Content-Type: application/json`; Qt sets `Host: 127.0.0.1:<port>` from the URL.
- **Middle click:** `omarchy-launch-or-focus-tui whoop-poc`. **Right click:** start a sync.
- **IPC:** `omarchy-shell wsturgiss.whoop <open|close|toggle|sync|refresh>`, e.g. for a keybinding.

### Gaps

- No `i18n/` catalogs; the strings are English only.
- Live HR in the bar is only as fresh as the status poll (2 s during a session).

### Verified on Omarchy (shell plugin API as installed)

Read from `/usr/share/omarchy/shell/` (`plugins/README.md`, `plugins/bar/README.md`, `Ui/Panel.qml`,
`Ui/KeyboardPanel.qml`, `Ui/BarIconButton.qml`) and the `omarchy-plugin-*` scripts:

- A plugin is a folder with `manifest.json` (`schemaVersion: 1`, `id`, `name`, `version`, `kinds`,
  `entryPoints`); `bar-widget` needs `entryPoints.barWidget`. `omarchy plugin validate <folder>` checks this
  and rejects symlinks and the `omarchy.*` namespace.
- `omarchy plugin add <git-url>` wants the manifest at the repo root, so it doesn't fit a subfolder of this
  repo; `contrib/omarchy/install.sh` copies the folder instead. Third-party plugins load from
  `~/.config/omarchy/plugins/<id>/`.
- `omarchy plugin enable|disable|remove` go through the running shell (`omarchy-shell shell
  enablePlugin/setPluginEnabled`), which rewrites `shell.json`'s `bar.layout`.
- Shell logs: `quickshell log /run/user/$UID/quickshell/by-pid/$(pgrep -x quickshell)/log.log`.

## Deliberately not done

- Packaging NOOP itself (AUR or similar). It's upstream's tool. We depend on a checkout.
- A root/system install. Everything is per-user, which also keeps biometrics in `$HOME`.
- An always-on live HR feed in the bar. The strap is drained in batches, so "latest" is as old as the last
  sync. The widget shows live HR only during a live session someone started (it ends after
  `liveMinutes`), because holding a BLE link all day costs strap battery.
