#!/usr/bin/env python3
"""Read a WHOOP 4.0 strap's battery with WHOOP's own GET_BATTERY_LEVEL (cmd 26) command.

The standard BLE Battery Service the strap also exposes (what BlueZ / `bluetoothctl info` show)
reported a flat 100% after days off the charger, so the readings here come from the proprietary
command. The response decode follows NOOP's analyze_command_responses.py: payload u16 @2 / 10 = %.
GET_EXTENDED_BATTERY_INFO (cmd 98) adds millivolts (u16 @7).

Run with NOOP linux-capture's venv python, from its directory (whoop-sync does this, holding the
strap lock):  whoop_battery.py --address MAC --out ~/.local/state/whoop-poc/battery.json
Writes battery.json atomically, and only when the strap answered. Exit 0 = read, 1 = no answer.
"""
import argparse
import asyncio
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.getcwd())          # NOOP linux-capture dir (cwd): whoop_sync, whoop_frame
import whoop_frame as wf                 # noqa: E402
from whoop_sync import Family, _acquire  # noqa: E402

CMD_GET_BATTERY_LEVEL = 26
CMD_GET_EXTENDED_BATTERY_INFO = 98
TYPE_COMMAND_RESPONSE = 36


def u16(b, o):
    return int.from_bytes(b[o:o + 2], "little") if o + 2 <= len(b) else None


def frames(buf: bytearray):
    """Pop complete 4.0 frames ([AA][len u16][crc8]...; total = len + 4) from a notify byte stream."""
    while True:
        i = buf.find(0xAA)
        if i < 0:
            buf.clear()
            return
        del buf[:i]
        if len(buf) < 4:
            return
        total = u16(buf, 1) + 4
        if total > 4096:                 # not a real header: skip this 0xAA
            del buf[:1]
            continue
        if len(buf) < total:
            return
        f = bytes(buf[:total])
        del buf[:total]
        yield f


def decode(f: bytes):
    """(cmd, value) for a CRC-valid battery COMMAND_RESPONSE, else None."""
    if len(f) < 12 or f[4] != TYPE_COMMAND_RESPONSE or not wf.verify_whoop4_frame(f):
        return None
    cmd, pay = f[6], f[7:-4]
    if cmd == CMD_GET_BATTERY_LEVEL and (v := u16(pay, 2)) is not None:
        return "pct", v / 10
    if cmd == CMD_GET_EXTENDED_BATTERY_INFO and (v := u16(pay, 7)) is not None:
        return "mv", v
    return None


def bas_pct(address):
    """Standard Battery Service value as BlueZ caches it while connected (kept for comparison only)."""
    try:
        out = subprocess.run(["bluetoothctl", "info", address], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for ln in out.splitlines():
        if "Battery Percentage" in ln and "(" in ln:
            return int(ln.rsplit("(", 1)[1].rstrip(")"))
    return None


async def read(address, tries):
    from bleak import BleakClient
    fam = Family("whoop4")
    dev = await _acquire(address, tries=tries)
    if dev is None:
        print("battery: strap not advertising", flush=True)
        return None
    got, raw, buf = {}, [], bytearray()

    def on_n(_c, d):
        buf.extend(d)
        for f in frames(buf):
            if f[4:5] == bytes([TYPE_COMMAND_RESPONSE]):
                raw.append(f.hex())
            if r := decode(f):
                got[r[0]] = r[1]

    async with BleakClient(dev) as client:
        try:   # same MTU bump NOOP does, so responses arrive as single packets
            if getattr(client, "mtu_size", 23) <= 23 and hasattr(client._backend, "_acquire_mtu"):
                await client._backend._acquire_mtu()
        except Exception:
            pass
        for u in fam.notify:
            try:
                await client.start_notify(u, on_n)
            except Exception:
                pass
        for seq, cmd in enumerate((CMD_GET_BATTERY_LEVEL, CMD_GET_EXTENDED_BATTERY_INFO), start=1):
            await client.write_gatt_char(fam.cmd_write, fam.cmd(cmd, seq=seq, payload=b"\x00"), response=True)
        for _ in range(20):
            if "pct" in got and "mv" in got:
                break
            await asyncio.sleep(0.25)
        bas = bas_pct(address)
    if "pct" not in got:
        print(f"battery: no GET_BATTERY_LEVEL response (raw type-36: {raw[:4]})", flush=True)
        return None
    return {"schema": 1, "pct": round(got["pct"]), "pct_exact": got["pct"], "mv": got.get("mv"),
            "unix": int(time.time()), "source": "whoop-cmd-26", "bas_pct": bas}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--tries", type=int, default=2)
    a = ap.parse_args()
    doc = asyncio.run(read(a.address, a.tries))
    if not doc:
        sys.exit(1)
    out = os.path.expanduser(a.out)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out + ".tmp", "w") as f:
        json.dump(doc, f)
        f.write("\n")
    os.chmod(out + ".tmp", 0o600)
    os.replace(out + ".tmp", out)
    print(f"battery: {doc['pct_exact']}%  {doc['mv'] or '?'} mV  (standard BLE service says {doc['bas_pct']}%)", flush=True)


if __name__ == "__main__":
    main()
