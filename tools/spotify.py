"""Spotify Connect via the Web API (Spotipy-compatible endpoints, httpx-based).

Output targeting includes Google Home Hub speakers exposed as Spotify Connect
devices. Demo mode simulates a coherent player with queue & device list.
"""
from __future__ import annotations

import time
from typing import Any

import config
from eventbus import emit, EV_SPOTIFY_PLAY, EV_SPOTIFY_PAUSE
from tools.registry import tool
from tools import sim

_TOKEN: dict[str, Any] = {"value": None, "exp": 0}


async def _token() -> str:
    if _TOKEN["value"] and _TOKEN["exp"] > time.time() + 60:
        return _TOKEN["value"]
    import base64
    cred = base64.b64encode(
        f"{config.SPOTIFY_CLIENT_ID}:{config.SPOTIFY_CLIENT_SECRET}".encode()).decode()
    d = await sim.api_post("https://accounts.spotify.com/api/token",
                           payload={"grant_type": "client_credentials"},
                           headers={"Authorization": f"Basic {cred}"})
    _TOKEN.update(value=d["access_token"], exp=time.time() + int(d.get("expires_in", 3600)))
    return _TOKEN["value"]


async def _call(method: str, path: str, **kw) -> dict:
    tok = await _token()
    hdr = {"Authorization": f"Bearer {tok}"}
    url = f"https://api.spotify.com/v1/{path.lstrip('/')}"
    if method == "GET":
        return await sim.api_get(url, params=kw.get("params"), headers=hdr)
    return await sim.api_post(url, json_body=kw.get("json"), headers=hdr)


@tool
async def spotify_status() -> dict:
    """Show Spotify now-playing: track, artist, progress, volume, active device."""
    if sim.demo():
        s = sim.world()["spotify"]
        t = s["queue"][s["idx"]] if s["playing"] else None
        return {"playing": s["playing"], "track": t[0] if t else None,
                "artist": t[1] if t else None, "volume": s["volume"],
                "device": s["device"], "shuffle": True}
    d = await _call("GET", "me/player/currently-playing")
    it = (d.get("item") or {})
    return {"playing": d.get("is_playing"), "track": it.get("name"),
            "artist": ", ".join(a["name"] for a in it.get("artists", [])),
            "volume": d.get("device", {}).get("volume_percent"),
            "device": d.get("device", {}).get("name")}


@tool
async def spotify_devices() -> dict:
    """List Spotify Connect devices (speakers, Google Home Hubs, phones)."""
    if sim.demo():
        return {"devices": [{"id": "living-room", "name": "Living Room Speaker", "type": "Speaker"},
                            {"id": "ghub-kitchen", "name": "Kitchen Hub (Google Home)", "type": "Speaker"},
                            {"id": "roidmi-dock-led", "name": "Hall Display", "type": "TV"}]}
    d = await _call("GET", "me/devices")
    return {"devices": [{"id": x["id"], "name": x["name"], "type": x["type"]}
                        for x in d.get("devices", [])]}


@tool
async def spotify_play(query: str = "", playlist: str = "", device_name: str = "") -> dict:
    """Play music: optionally by search query (song/artist), named playlist, and target output device.
    :param query: search text like 'weightless marconi union' — starts a radio-ish play
    :param playlist: playlist name among user's playlists
    :param device_name: Spotify Connect device to target, e.g. 'Kitchen Hub'
    """
    s = sim.world()["spotify"]
    if sim.demo():
        if device_name:
            s["device"] = device_name
        if s["playing"] is False or query or playlist:
            pass
        s["playing"] = True
        s["playlist"] = playlist or s["playlist"]
        if query:
            s["queue"][s["idx"]] = (query.title(), "Simulated Artist")
        track = s["queue"][s["idx"]]
        emit(EV_SPOTIFY_PLAY, source="spotify",
             message=f'▶ "{track[0]}" — {track[1]} on {s["device"]}',
             data={"track": track[0], "artist": track[1], "device": s["device"]})
        return {"ok": True, "playing": True, "track": track[0], "device": s["device"]}
    body: dict[str, Any] = {}
    if device_name:
        devs = (await spotify_devices.__wrapped__)()["devices"]
        dev = next((x for x in devs if device_name.lower() in x["name"].lower()), None)
        if dev:
            body["device_id"] = dev["id"]
    if playlist:
        pls = (await _call("GET", "me/playlists")).get("items", [])
        pl = next((p for p in pls if playlist.lower() in p["name"].lower()), None)
        if pl:
            res = await _call("PUT", f"me/player/play?device_id={body.get('device_id', config.SPOTIFY_DEVICE_ID)}",
                              json={"context_uri": pl["uri"]})
            emit(EV_SPOTIFY_PLAY, source="spotify", message=f'▶ playlist "{pl["name"]}"')
            return {"ok": True, "playlist": pl["name"]}
    if query:
        sr = await _call("GET", "search", params={"q": query, "type": "track", "limit": 1})
        tracks = sr.get("tracks", {}).get("items", [])
        if tracks:
            body["uris"] = [tracks[0]["uri"]]
    res = await _call("PUT", "me/player/play", json=body)
    emit(EV_SPOTIFY_PLAY, source="spotify", message=f"▶ playing ({query or 'resume'})")
    return {"ok": True, "result": res}


@tool
async def spotify_pause() -> dict:
    """Pause Spotify playback on the active device."""
    if sim.demo():
        s = sim.world()["spotify"]
        s["playing"] = False
        emit(EV_SPOTIFY_PAUSE, source="spotify", message="⏸ paused")
        return {"ok": True, "playing": False}
    await _call("PUT", "me/player/pause")
    emit(EV_SPOTIFY_PAUSE, source="spotify", message="⏸ paused")
    return {"ok": True}


@tool
async def spotify_skip(next_track: bool = True) -> dict:
    """Skip to the next (or previous) track in the current context."""
    if sim.demo():
        s = sim.world()["spotify"]
        s["idx"] = (s["idx"] + (1 if next_track else -1)) % len(s["queue"])
        t = s["queue"][s["idx"]]
        emit(EV_SPOTIFY_PLAY, source="spotify", message=f'⏭ "{t[0]}" — {t[1]}',
             data={"track": t[0], "artist": t[1]})
        return {"ok": True, "track": t[0], "artist": t[1]}
    await _call("POST", f"me/player/{'next' if next_track else 'previous'}")
    return await spotify_status.__wrapped__()


@tool
async def spotify_volume(percent: int) -> dict:
    """Set Spotify playback volume to an absolute percent 0-100."""
    percent = max(0, min(100, percent))
    if sim.demo():
        sim.world()["spotify"]["volume"] = percent
        return {"ok": True, "volume": percent}
    await _call("PUT", "me/player/volume", params={"volume_percent": percent})
    return {"ok": True, "volume": percent}
