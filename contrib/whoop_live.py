#!/usr/bin/env python3
"""Live heart rate from a WHOOP 4.0 strap, for a limited time, into a small JSON file.

    whoop-sync live [--minutes 10]      (the wrapper holds the strap lock, so no sync runs meanwhile)

Holding a BLE link open with HR streaming costs strap battery, so this always stops on its own after
--minutes (default 30). SIGUSR1 extends it by 10 min (capped at 3 h total). SIGTERM/SIGINT stop it early,
and it turns the stream off on the way out.

How: arm only WHOOP's TOGGLE_REALTIME_HR and read heart rate from the type-40 REALTIME_DATA frames the
strap sends on its WHOOP notify channels (bpm u8 @12, per NOOP's whoop_protocol.json). The standard
Heart Rate Measurement characteristic (0x2A37) is also subscribed, but it stays silent unless the strap's
HR-broadcast profile is enabled (BLE_HR_PROFILE_ENABLED), which it isn't on this strap. NOOP observed that a one-shot enable on a 4C streams a burst and goes quiet, so it is
re-armed every few seconds (which also keeps the link warm). If no HR arrives, it falls back to NOOP's
full realtime arm (adds SEND_R10_R11_REALTIME: R-R + raw IMU, more radio traffic).

Output: $XDG_STATE_HOME/whoop-poc/live.json, rewritten atomically on each beat/state change:
  {"schema":1, "state":"connecting|waiting|live|reconnecting|ended", "bpm":N|null, "unix":T|null,
   "started_unix":T, "until_unix":T, "pid":P, "mode":"hr|full"|null, "note":str|null}
Each reading is also appended to live-samples.csv ("unix,bpm", one per second) for the session graph.
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
from whoop_battery import frames        # noqa: E402  (4.0 frame reassembly, same dir)
from whoop_sync import (CMD_SEND_R10_R11_REALTIME, CMD_TOGGLE_REALTIME_HR, HR_MEASUREMENT,  # noqa: E402
                        Family, _acquire)

REARM_S = 3.0
QUIET_FALLBACK_S = 8.0     # no HR this long in "hr" mode → arm the full realtime stream
EXTEND_S = 600             # SIGUSR1 adds this much
MAX_TOTAL_S = 3 * 3600
TYPE_REALTIME_DATA = 40
HR_OFF = 12


def realtime_hr(f: bytes):
    """bpm from a CRC-valid WHOOP 4.0 REALTIME_DATA frame (0 = no reading → None)."""
    if len(f) > HR_OFF + 4 and f[4] == TYPE_REALTIME_DATA and wf.verify_whoop4_frame(f):
        return f[HR_OFF] or None
    return None


class Out:
    def __init__(self, path, minutes):
        self.path = os.path.expanduser(path)
        now = int(time.time())
        self.doc = {"schema": 1, "state": "connecting", "bpm": None, "unix": None, "started_unix": now,
                    "until_unix": now + int(minutes * 60), "pid": os.getpid(), "mode": None, "note": None}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        # Session series for the TUI graph: "unix,bpm" per second, truncated at each session start.
        sp = os.path.join(os.path.dirname(self.path), "live-samples.csv")
        self._samples = os.fdopen(os.open(sp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w")
        self._last_t = None
        self.write()

    def sample(self, t: int, bpm: int):
        if t != self._last_t:            # REALTIME_DATA can arrive more than once a second
            self._last_t = t
            self._samples.write(f"{t},{bpm}\n")
            self._samples.flush()

    def write(self, **kw):
        self.doc.update(kw)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.doc, f)
        os.replace(tmp, self.path)


async def session(client, fam, out, stop):
    last_hr = {"t": 0.0}

    def beat(bpm):
        last_hr["t"] = time.time()
        out.write(state="live", bpm=bpm, unix=int(last_hr["t"]))
        out.sample(int(last_hr["t"]), bpm)

    def on_std(_c, data):
        if bpm := wf.parse_standard_hr(bytes(data)):
            beat(bpm)

    def on_whoop(buf):
        def h(_c, data):
            buf.extend(data)
            for f in frames(buf):
                if bpm := realtime_hr(f):
                    beat(bpm)
        return h

    try:
        await client.start_notify(HR_MEASUREMENT, on_std)
    except Exception:
        pass
    for u in fam.notify:                 # REALTIME_DATA arrives here; one reassembly buffer per channel
        try:
            await client.start_notify(u, on_whoop(bytearray()))
        except Exception:
            pass

    async def arm(seq, full):
        if full:
            await client.write_gatt_char(fam.cmd_write, fam.cmd(CMD_SEND_R10_R11_REALTIME, seq=seq, payload=b"\x01"))
        await client.write_gatt_char(fam.cmd_write, fam.cmd(CMD_TOGGLE_REALTIME_HR, seq=seq + 1, payload=b"\x01"))

    full, seq, armed_at = False, 1, time.time()
    out.write(mode="hr", state="waiting" if out.doc["state"] != "live" else "live")
    try:
        while not stop.is_set() and time.time() < out.doc["until_unix"] and client.is_connected:
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

    def extend():
        cap = out.doc["started_unix"] + MAX_TOTAL_S
        out.write(until_unix=min(cap, max(out.doc["until_unix"], int(time.time())) + EXTEND_S))
    loop.add_signal_handler(signal.SIGUSR1, extend)
    note = None
    while not stop.is_set() and time.time() < out.doc["until_unix"]:
        dev = await _acquire(a.address, tries=3)
        if dev is None:
            note = "strap not advertising"
            break
        try:
            async with BleakClient(dev) as client:
                await session(client, fam, out, stop)
        except (BleakError, asyncio.TimeoutError, EOFError, OSError) as e:
            note = f"link error: {e}"
        if not stop.is_set() and time.time() < out.doc["until_unix"]:
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
    ap.add_argument("--minutes", type=float, default=30.0)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
