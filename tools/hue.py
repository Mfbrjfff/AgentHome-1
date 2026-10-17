"""Philips Hue integration (CLIP v2 REST API) + demo simulator.

Time-of-day & mood based auto-adjustments live in :func:`hue_auto_adjust`,
which the routines and scheduler can call directly.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any

import config
from eventbus import emit, EV_HUE_ON, EV_HUE_OFF
from tools.registry import tool
from tools import sim


def _api() -> str:
    return f"http://{config.HUE_BRIDGE_IP}/clip/v2"


def _hdr() -> dict[str, str]:
    return {"X-Bridge-Application-Name": "smart-agent", "Hue-Application-Runtime": "1.0"}


async def _v2_get(resource: str) -> list[dict[str, Any]]:
    data = await sim.api_get(f"{_api()}/{resource}", headers=_hdr())
    return data.get("data", [])


async def _v2_put(resource: str, light_id: str, body: dict) -> None:
    await sim.api_post(f"{_api()}/{resource}/{light_id}", json_body=body, headers=_hdr())


@tool
async def hue_lights() -> dict:
    """List all Hue lights with on/off, brightness, colour-temp state."""
    if sim.demo():
        w = sim.world()["hue"]
        return {"lights": [{"id": k, **v} for k, v in w.items()]}
    lights = await _v2_get("lights")
    out = []
    for l in lights:
        onoff = l.get("on", {})
        out.append({"id": l["id"], "name": l.get("metadata", {}).get("name"),
                    "on": onoff.get("on"), "bri": l.get("dimming", {}).get("brightness"),
                    "ct": (l.get("color_temperature", {}) or {}).get("mirek")})
    return {"lights": out}


@tool
async def hue_set(room: str, on: bool = True, brightness: int = -1,
                  color_temp_mired: int = -1, hex_color: str = "") -> dict:
    """Turn a Hue room/lights on/off, set brightness 0-100, warmth (mireds 153-454) or hex colour.
    :param room: room id like living_room/kitchen/bedroom/hallway or 'all'
    :param brightness: 0-100 percent, -1 keeps current
    """
    rooms = list(sim.world()["hue"].keys()) if room == "all" else [room]
    changed = []
    if sim.demo():
        w = sim.world()["hue"]
        for r in rooms:
            if r not in w:
                return {"ok": False, "error": f"unknown room '{r}'"}
            st = w[r]
            st["on"] = on
            if brightness >= 0:
                st["bri"] = int(brightness * 2.54)
            if color_temp_mired > 0:
                st["ct"] = color_temp_mired
            if hex_color:
                st["color"] = hex_color
            changed.append(r)
    else:
        ents = await _v2_get("light")
        for e in ents:
            name = (e.get("metadata", {}).get("name") or "").lower().replace(" ", "_")
            if name not in rooms:
                continue
            body: dict[str, Any] = {"on": {"on": on}}
            if brightness >= 0:
                body["dimming"] = {"brightness": max(0, min(100, brightness))}
            if color_temp_mired > 0:
                body["color_temperature"] = {"mirek": color_temp_mired}
            await _v2_put("light", e["id"], body)
            changed.append(name)
    pct = max(0, min(100, round((sim.world()["hue"][rooms[0]].get("bri", 200) / 2.54)))) if sim.demo() else brightness
    emit(EV_HUE_ON if on else EV_HUE_OFF, source="hue",
         message=f"{', '.join(changed)} -> {'ON' if on else 'OFF'} ({pct}% bright)",
         data={"rooms": changed, "on": on, "brightness_pct": pct,
               "color": hex_color or None})
    return {"ok": True, "rooms": changed, "on": on, "brightness_pct": pct}


@tool
async def hue_scene(scene: str) -> dict:
    """Activate a named Hue scene (sunrise-warm, relax-evening, focus, movie, goodnight)."""
    presets = {
        "sunrise-warm":   {"on": True, "bri": 70,  "ct": 400},
        "relax-evening":  {"on": True, "bri": 35,  "ct": 430},
        "focus":          {"on": True, "bri": 95,  "ct": 250},
        "movie":          {"on": True, "bri": 12,  "ct": 380},
        "goodnight":      {"on": False, "bri": 0,  "ct": 450},
    }
    if scene not in presets:
        return {"ok": False, "error": f"unknown scene; have {list(presets)}"}
    p = presets[scene]
    res = await hue_set.__wrapped__(room="all", on=p["on"], brightness=p["bri"],
                                    color_temp_mired=p["ct"])
    res["scene"] = scene
    return res


TIME_MOODS = [
    # (hour_from, hour_to, scene, note)
    (5, 8, "sunrise-warm", "morning warm glow"),
    (8, 12, "focus", "daylight focus"),
    (12, 17, "focus", "afternoon"),
    (17, 21, "relax-evening", "evening wind-down"),
    (21, 24, "goodnight", "night"),
    (0, 5, "goodnight", "night"),
]


@tool
async def hue_auto_adjust(mood: str = "") -> dict:
    """Auto-set lighting by time of day, or an explicit mood word (warm/focus/relax/night)."""
    if mood == "" or mood == "auto":
        h = datetime.now().hour
        scene = next((s for a, b, s, _ in TIME_MOODS if a <= h < b), "relax-evening")
    else:
        mapping = {"warm": "sunrise-warm", "focus": "focus", "bright": "focus",
                   "relax": "relax-evening", "evening": "relax-evening",
                   "night": "goodnight", "movie": "movie"}
        scene = mapping.get(mood.lower(), "relax-evening")
    return await hue_scene.__wrapped__(scene=scene)
