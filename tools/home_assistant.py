"""Home Assistant / Google Home control + device-tracker presence detection.

HA REST API (long-lived token) is used for generic entity control and as the
presence source when configured; otherwise network presence (tools/network.py)
is the geofence alternative.
"""
from __future__ import annotations

from typing import Any

import config
from eventbus import emit, EV_SYSTEM
from tools.registry import tool
from tools import sim


def _hdr() -> dict[str, str]:
    return {"Authorization": f"Bearer {config.HASS_TOKEN}",
            "Content-Type": "application/json"}


async def _ha_state(entity: str) -> dict[str, Any]:
    d = await sim.api_get(f"{config.HASS_URL}/api/states/{entity}", headers=_hdr())
    return d


@tool
async def ha_entities(domain: str = "") -> dict:
    """List Home Assistant entities (optionally filter by domain like light/switch/sensor)."""
    if sim.demo():
        w = sim.world()
        ents = [{"entity_id": f"light.{r}", "state": "on" if s["on"] else "off",
                 "friendly_name": s["name"]} for r, s in w["hue"].items()]
        ents.append({"entity_id": "vacuum.roidmi_eve", "state": w["vacuum"]["state"]})
        ents.append({"entity_id": "media_player.living_room_speaker",
                     "state": "playing" if w["spotify"]["playing"] else "paused"})
        for name, p in w["presence"].items():
            ents.append({"entity_id": f"device_tracker.{name.replace('-', '_')}",
                         "state": "home" if p["home"] else "not_home"})
        return {"entities": [e for e in ents if not domain or e["entity_id"].startswith(domain)]}
    states = await sim.api_get(f"{config.HASS_URL}/api/states", headers=_hdr())
    out = [{"entity_id": s["entity_id"], "state": s["state"],
            "friendly_name": s.get("attributes", {}).get("friendly_name")}
           for s in states if not domain or s["entity_id"].startswith(domain + ".")]
    return {"entities": out}


@tool
async def ha_call(domain: str, service: str, entity_id: str = "",
                  params_json: str = "{}") -> dict:
    """Call any Home Assistant service, e.g. domain=light service=turn_on entity_id=light.kitchen.
    :param params_json: JSON object of extra service data, e.g. '{"brightness_pct":50}'
    """
    import json as _json
    body: dict[str, Any] = {}
    if entity_id:
        body["entity_id"] = entity_id
    try:
        body.update(_json.loads(params_json or "{}"))
    except _json.JSONDecodeError:
        return {"ok": False, "error": "params_json must be valid JSON"}
    if sim.demo():
        emit(EV_SYSTEM, source="home-assistant",
             message=f"[demo] called {domain}.{service} on {entity_id or 'all'}")
        # keep the sim world roughly coherent for common calls
        if domain == "light" and "kitchen" in entity_id:
            await hue_sync_demo(entity_id, service)
        return {"ok": True, "demo": True, "service": f"{domain}.{service}", "entity": entity_id}
    uri = f"{config.HASS_URL}/api/services/{domain}/{service}"
    res = await sim.api_post(uri, json_body=body, headers=_hdr())
    return {"ok": True, "result": res}


async def hue_sync_demo(entity_id: str, service: str) -> None:
    from tools.hue import hue_set
    room = entity_id.split(".")[-1]
    await hue_set.__wrapped__(room=room, on=(service == "turn_on"))


@tool
async def google_home_broadcast(text: str) -> dict:
    """Speak a message on Google Home/Nest speakers via Smart Home broadcast (or HA TTS service)."""
    if sim.demo():
        emit(EV_SYSTEM, source="google-home", message=f'broadcast to speakers: "{text}"')
        return {"ok": True, "demo": True, "broadcast": text}
    # Preferred path: HA notify/tts service against Google Home entities.
    res = await ha_call.__wrapped__(domain="tts", service="say",
                                    entity_id="tts.google_home_en",
                                    params_json=_safe_json({"message": text}))
    return res


def _safe_json(d: dict) -> str:
    import json
    return json.dumps(d)


@tool
async def presence_check(device: str = "") -> dict:
    """Check home/away presence from HA device-trackers (or simulated world in demo mode)."""
    if sim.demo():
        people = sim.world()["presence"]
    else:
        ents = (await ha_entities.__wrapped__(domain="device_tracker")).get("entities", [])
        people = {e["entity_id"].replace("device_tracker.", ""):
                  {"home": e["state"] == "home"} for e in ents}
    if device:
        p = people.get(device)
        if p is None:
            return {"ok": False, "error": f"no tracker for '{device}'"}
        return {"device": device, "home": bool(p.get("home"))}
    anyone_home = any(p.get("home") for p in people.values())
    return {"anyone_home": anyone_home,
            "people": {k: bool(v.get("home")) for k, v in people.items()}}
