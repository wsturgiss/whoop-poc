#!/usr/bin/env python3
"""Live heart rate from a WHOOP 4.0 strap, for a limited time, into a small JSON file.

    whoop-sync live [--minutes 10]      (the wrapper holds the strap lock, so no sync runs meanwhile)

Holding a BLE link open with HR streaming costs strap battery, so this always stops on its own after
--minutes. SIGTERM/SIGINT stop it early, and it turns the stream off on the way out.

How: subscribe to the standard Heart Rate Measurement characteristic (0x2A37) and arm only WHOOP's
TOGGLE_REALTIME_HR. NOOP observed that a one-shot enable on a 4C streams a burst and goes quiet, so it is
re-armed every few seconds (which also keeps the link warm). If no HR arrives, it falls back to NOOP's
full realtime arm (adds SEND_R10_R11_REALTIME: R-R + raw IMU, more radio traffic).

Output: $XDG_STATE_HOME/whoop-poc/live.json, rewritten atomically on each beat/state change:
  {"schema":1, "state":"connecting|waiting|live|reconnecting|ended", "bpm":N|null, "unix":T|null,
   "started_unix":T, "until_unix":T, "pid":P, "mode":"hr|full"|null, "note":str|null}
Run with NOOP linux-capture's venv python from its directory (the wrapper does this).
"""
import argparse
import asyncio
import json
import os
import signal
import sys
import time

sys.path.insert(0, os.getcwd())          # NOOP linux-capture dir (cwd)
import whoop_frame as wf                 # noqa: E402
from whoop_sync import (CMD_SEND_R10_R11_REALTIME, CMD_TOGGLE_REALTIME_HR, HR_MEASUREMENT,  # noqa: E402
                        Family, _acquire)

REARM_S = 3.0
QUIET_FALLBACK_S = 8.0     # no HR this long in "hr" mode → arm the full realtime stream


class Out:
    def __init__(self, path, minutes):
        self.path = os.path.expanduser(path)
        now = int(time.time())
        self.doc = {"schema": 1, "state": "connecting", "bpm": None, "unix": None, "started_unix": now,
                    "until_unix": now + int(minutes * 60), "pid": os.getpid(), "mode": None, "note": None}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.write()

    def write(self, **kw):
        self.doc.update(kw)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.doc, f)
        os.replace(tmp, self.path)


async def session(client, fam, out, stop, deadline):
    last_hr = {"t": 0.0}

    def on_hr(_c, data):
        bpm = wf.parse_standard_hr(bytes(data))
        if bpm:
            last_hr["t"] = time.time()
            out.write(state="live", bpm=bpm, unix=int(last_hr["t"]))

    await client.start_notify(HR_MEASUREMENT, on_hr)
    for u in fam.notify:                 # WHOOP channels must be subscribed for commands to be served
        try:
            await client.start_notify(u, lambda *_: None)
        except Exception:
            pass

    async def arm(seq, full):
        if full:
            await client.write_gatt_char(fam.cmd_write, fam.cmd(CMD_SEND_R10_R11_REALTIME, seq=seq, payload=b"\x01"))
        await client.write_gatt_char(fam.cmd_write, fam.cmd(CMD_TOGGLE_REALTIME_HR, seq=seq + 1, payload=b"\x01"))

    full, seq, armed_at = False, 1, time.time()
    out.write(mode="hr", state="waiting" if out.doc["state"] != "live" else "live")
    try:
        while not stop.is_set() and time.time() < deadline and client.is_connected:
            await arm(seq, full)
            seq = (seq + 2) & 0xFF
            if not full and time.time() - max(last_hr["t"], armed_at) > QUIET_FALLBACK_S:
                full = True
                out.write(mode="full", note="HR-only arm was quiet; using full realtime stream")
            try:
                await asyncio.wait_for(stop.wait(), REARM_S)
            except asyncio.TimeoutError:
                pass
    finally:
        if client.is_connected:          # turn the streams back off before letting go
            for cmd in (CMD_TOGGLE_REALTIME_HR, CMD_SEND_R10_R11_REALTIME):
                try:
                    await client.write_gatt_char(fam.cmd_write, fam.cmd(cmd, seq=seq, payload=b"\x00"))
                except Exception:
                    pass
                seq = (seq + 1) & 0xFF


async def main_async(a):
    from bleak import BleakClient
    from bleak.exc import BleakError
    fam = Family("whoop4")
    out = Out(a.out, a.minutes)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    deadline = out.doc["until_unix"]
    note = None
    while not stop.is_set() and time.time() < deadline:
        dev = await _acquire(a.address, tries=3)
        if dev is None:
            note = "strap not advertising"
            break
        try:
            async with BleakClient(dev) as client:
                await session(client, fam, out, stop, deadline)
        except (BleakError, asyncio.TimeoutError, EOFError, OSError) as e:
            note = f"link error: {e}"
        if not stop.is_set() and time.time() < deadline:
            out.write(state="reconnecting")
            await asyncio.sleep(1.0)
    if note is None:
        note = "stopped" if stop.is_set() else "time limit reached"
    out.write(state="ended", note=note)
    print(f"live: ended ({note})", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=10.0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
