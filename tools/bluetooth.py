"""Bluetooth control via bluetoothctl / dbus subprocess wrappers.

Works on a real Pi (bluez installed). In DEMO_MODE everything is simulated
against tools/sim world state so the agent still has a coherent BT "device".
Exposed as agent tools: scan, list, pair, connect, disconnect, trust, audio switch.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import config
from eventbus import emit, EV_SYSTEM
from tools.registry import tool
from tools import sim


async def _btctl(*args: str, timeout: float = 20.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        return out.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        return "(bluetoothctl timed out)"


@tool
async def bluetooth_scan(seconds: int = 8) -> dict:
    """Scan for nearby Bluetooth devices (real: bluetoothctl; demo: simulated list)."""
    if sim.demo():
        devs = sim.world()["bluetooth"]
        emit(EV_SYSTEM, source="bluetooth",
             message=f"[demo] scan found {len(devs)} devices")
        return {"ok": True, "demo": True,
                "devices": [{"mac": d["mac"], "name": d["name"], "paired": d["paired"],
                             "connected": d["connected"]} for d in devs]}
    txt = await _btctl("--timeout", str(seconds), "scan", "on")
    lines = [l.strip() for l in txt.splitlines() if "Device" in l and "-" in l]
    out = []
    for l in lines:
        parts = l.replace("Device", "").strip().split(" ", 1)
        mac = parts[0]
        name = parts[1].strip() if len(parts) > 1 else ""
        out.append({"mac": mac, "name": name})
    return {"ok": True, "raw": txt[-500:], "devices": out}


@tool
async def bluetooth_devices() -> dict:
    """List paired/trusted/known Bluetooth devices with connection state."""
    if sim.demo():
        return {"ok": True, "demo": True,
                "devices": sim.world()["bluetooth"]}
    txt = await _btctl("devices", "Paired")
    out = []
    for line in txt.splitlines():
        if "Device" in line:
            p = line.split("Device")[1].strip().split(" ", 1)
            out.append({"mac": p[0], "name": p[1] if len(p) > 1 else ""})
    return {"ok": True, "devices": out}


@tool
async def bluetooth_pair(mac: str) -> dict:
    """Pair with a Bluetooth device by MAC address."""
    if sim.demo():
        for d in sim.world()["bluetooth"]:
            if d["mac"].lower() == mac.lower():
                d["paired"] = True
                emit(EV_SYSTEM, source="bluetooth", message=f"[demo] paired {d['name']}")
                return {"ok": True, "demo": True, "paired": d}
        newd = {"mac": mac, "name": f"New Device {mac[-5:]}", "paired": True,
                "trusted": False, "connected": False}
        sim.world()["bluetooth"].append(newd)
        return {"ok": True, "demo": True, "paired": newd}
    txt = await _btctl("pair", mac)
    ok = "Pairing successful" in txt or "successful" in txt.lower()
    return {"ok": ok, "raw": txt[-400:]}


@tool
async def bluetooth_connect(mac: str) -> dict:
    """Connect to a paired Bluetooth device (headphones, speakers, wearables)."""
    if sim.demo():
        for d in sim.world()["bluetooth"]:
            if d["mac"].lower() == mac.lower():
                others = [x for x in sim.world()["bluetooth"] if x["connected"]]
                for o in others:
                    o["connected"] = False
                d["connected"] = True
                emit(EV_SYSTEM, source="bluetooth", message=f"[demo] connected {d['name']}")
                return {"ok": True, "demo": True, "connected": d["name"]}
        return {"ok": False, "error": f"unknown mac {mac} — scan first"}
    txt = await _btctl("connect", mac)
    return {"ok": "Successful" in txt, "raw": txt[-300:]}


@tool
async def bluetooth_disconnect(mac: str = "") -> dict:
    """Disconnect a Bluetooth device (empty mac → disconnect whatever is connected)."""
    if sim.demo():
        for d in sim.world()["bluetooth"]:
            if (not mac or d["mac"].lower() == mac.lower()) and d.get("connected"):
                d["connected"] = False
                return {"ok": True, "demo": True, "disconnected": d["name"]}
        return {"ok": True, "demo": True, "note": "nothing was connected"}
    if not mac:
        info = await _btctl("info")
        # best-effort: grab Address from powered device
        for line in info.splitlines():
            if line.startswith("Device"):
                mac = line.split()[1]
                break
    txt = await _btctl("disconnect", mac)
    return {"ok": "Successful" in txt, "raw": txt[-300:]}


@tool
async def bluetooth_trust(mac: str) -> dict:
    """Mark a Bluetooth device trusted so it can auto-reconnect."""
    if sim.demo():
        for d in sim.world()["bluetooth"]:
            if d["mac"].lower() == mac.lower():
                d["trusted"] = True
                return {"ok": True, "demo": True, "device": d}
        return {"ok": False, "error": "unknown device"}
    txt = await _btctl("trust", mac)
    return {"ok": "Trust succeeded" in txt or "changing" in txt, "raw": txt[-200:]}


@tool
async def bluetooth_set_audio(mac: str) -> dict:
    """Switch system audio output to a connected Bluetooth speaker/headset (PulseAudio pactl)."""
    if sim.demo():
        dev = next((d for d in sim.world()["bluetooth"] if d["mac"].lower() == mac.lower()), None)
        if not dev:
            return {"ok": False, "error": "unknown device"}
        emit(EV_SYSTEM, source="bluetooth", message=f"[demo] audio output → {dev['name']}")
        return {"ok": True, "demo": True, "audio_sink": dev["name"]}
    # find sink name then set default
    sinks = await _run("pactl", "list", "short")
    target = None
    for line in sinks.splitlines():
        cols = line.split("\t")
        if len(cols) >= 3 and "bluez" in cols[2] and mac.replace(":", "_") in cols[2]:
            target = cols[1]
    if not target:
        return {"ok": False, "error": f"no pulse sink matching {mac}; is it connected?"}
    await _run("pactl", "set-default-sink", target)
    return {"ok": True, "sink": target}


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return out.decode(errors="replace")
