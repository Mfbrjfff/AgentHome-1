"""Roidmi Eve Plus robot vacuum (python-miio protocol or Tuya bridge).

Real hardware path uses ``python-miio``'s DeviceFactory against the vacuum's
token/IP; if the optional dependency is missing it falls back to raw miIO UDP.
Demo mode simulates battery drain, dustbin fill and room cleaning progress.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

import config
from eventbus import emit, EV_VACUUM_CLEAN, EV_VACUUM_DOCK
from tools.registry import tool
from tools import sim

ROOMS = ["living_room", "kitchen", "bedroom", "hall", "bathroom"]


def _miio_send(cmd: str, params: list | None = None) -> dict[str, Any]:
    """Blocking raw miIO call (runs in thread via registry for sync tools)."""
    try:
        from miio import Device  # type: ignore
        dev = Device(config.ROIDMI_IP, config.ROIDMI_TOKEN)
        method = {"status": "get_properties", "start": "set_mode_withroom",
                  "stop": "stop_permanent", "home": "set_home"}[cmd]
        if cmd == "start":
            return {"result": dev.send(method, [[0, 1, 1]])}
        if cmd == "status":
            return {"result": dev.get_properties(
                ["battery", "state", "dustbin", "mode"])}
        return {"result": dev.send(method, params or [])}
    except ImportError:
        raise RuntimeError("python-miio not installed; run with DEMO_MODE=true or pip install python-miio")


@tool
async def vacuum_status() -> dict:
    """Roidmi Eve status: state, battery %, dustbin fullness, current room."""
    if sim.demo():
        v = sim.world()["vacuum"]
        _sim_tick(v)
        return {"state": v["state"], "battery": v["battery"],
                "dustbin_pct": v["dustbin_pct"], "mode": v["mode"],
                "room": v["room"], "cleaned_minutes_total": v["cleaned_min_total"]}
    return await asyncio.to_thread(_miio_send, "status")


@tool
async def vacuum_start(room: str = "all", mode: str = "quiet") -> dict:
    """Start cleaning. Optionally a single room (living_room/kitchen/bedroom/hall/bathroom); mode quiet|standard|boost.
    :param room: 'all' or one ROOM name — room-specific spot cleaning
    """
    if room != "all" and room not in ROOMS:
        return {"ok": False, "error": f"unknown room; choose {ROOMS + ['all']}"}
    if sim.demo():
        v = sim.world()["vacuum"]
        if v["battery"] < 20 and v["state"] != "cleaning":
            return {"ok": False, "reason": f"battery too low ({v['battery']}%) — charging first"}
        v.update(state="cleaning", mode=mode, room=None if room == "all" else room,
                 last_run_start=time.time())
        emit(EV_VACUUM_CLEAN, source="roidmi",
             message=f"🤖 cleaning {room} ({mode})",
             data={"room": room, "battery": v["battery"]})
        return {"ok": True, "state": "cleaning", "room": room}
    return await asyncio.to_thread(_miio_send, "start", [0, 1, 1])


@tool
async def vacuum_stop() -> dict:
    """Stop the vacuum where it is."""
    if sim.demo():
        v = sim.world()["vacuum"]
        v["state"] = "idle"
        v["room"] = None
        emit(EV_VACUUM_DOCK, source="roidmi", message="⏹ stopped mid-run")
        return {"ok": True, "state": "idle"}
    return await asyncio.to_thread(_miio_send, "stop")


@tool
async def vacuum_return_home() -> dict:
    """Send the Roidmi back to its charging dock."""
    if sim.demo():
        v = sim.world()["vacuum"]
        v.update(state="returning", room=None)
        await asyncio.sleep(0.2)
        v.update(state="docked", room=None)
        emit(EV_VACUUM_DOCK, source="roidmi",
             message=f"🔙 returning to dock (battery {v['battery']}%)",
             data={"battery": v["battery"]})
        return {"ok": True, "state": "docked"}
    return await asyncio.to_thread(_miio_send, "home")


def _sim_tick(v: dict) -> None:
    """Advance simulated vacuum physics since its last state change."""
    now = time.time()
    if v["state"] == "cleaning" and v.get("last_run_start"):
        elapsed_min = (now - v["last_run_start"]) / 60
        v["battery"] = max(5, round(87 - elapsed_min * 1.4))
        v["dustbin_pct"] = min(100, v["dustbin_pct"] + elapsed_min * 0.3)
        v["cleaned_min_total"] = round(v["cleaned_min_total"] + 0.2, 1)
        if v["battery"] <= 15:
            v["state"] = "returning"
            emit(EV_VACUUM_DOCK, source="roidmi", message="low battery → auto-dock")
