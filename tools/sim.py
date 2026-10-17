"""Shared helpers: demo-mode aware httpx client + a lightweight simulator.

In DEMO_MODE every integration routes through :func:`sim_state`, a single
in-memory world model (lights, spotify, vacuum, weather, people, network...)
that evolves realistically over time so simulated responses are consistent
across tools and turns.
"""
from __future__ import annotations

import random
import time
from typing import Any

import httpx
import config

# ------------------------------------------------------------------ world sim
_WORLD: dict[str, Any] | None = None


def world() -> dict[str, Any]:
    global _WORLD
    if _WORLD is None:
        _WORLD = _fresh_world()
    return _WORLD


def _fresh_world() -> dict[str, Any]:
    now = time.time()
    return {
        "hue": {
            "living_room": {"on": False, "bri": 180, "ct": 366, "color": "#FFD27F", "name": "Living Room"},
            "kitchen":     {"on": False, "bri": 200, "ct": 300, "color": "#FFFFFF", "name": "Kitchen"},
            "bedroom":     {"on": False, "bri": 90,  "ct": 400, "color": "#FFB56B", "name": "Bedroom"},
            "hallway":     {"on": True,  "bri": 140, "ct": 350, "color": "#FFE0B2", "name": "Hallway"},
        },
        "scenes": ["sunrise-warm", "relax-evening", "focus", "movie", "goodnight"],
        "spotify": {
            "playing": False, "track": None, "playlist": None, "volume": 45,
            "device": "Living Room Speaker",
            "queue": [
                ("Zvaigžņu ceļš", "Instrumental Latvian Radio"),
                ("Weightless", "Marconi Union"),
                ("Clair de Lune", "Debussy"),
                ("Radio Ga Ga", "Queen"),
                ("Stardust", "Martin Gore"),
            ],
            "idx": 0,
        },
        "vacuum": {
            "state": "docked", "battery": 87, "dustbin_pct": 22, "mode": "quiet",
            "room": None, "cleaned_min_total": 1240, "last_run_start": None,
        },
        "withings": {
            "height_cm": 182,
            "history": _gen_health_history(),
        },
        "weather": _gen_weather(),
        "presence": {
            "andris-phone": {"mac": "AA:BB:CC:11:22:33", "home": True, "rssi": -52},
            "maijs-laptop": {"mac": "AA:BB:CC:44:55:66", "home": True, "rssi": -61},
        },
        "network": {
            "ssid": "HOME-5G", "gateway": "192.168.1.1",
            "devices": _gen_net_devices(),
            "known_macs": set(),
        },
        "bluetooth": _gen_bt(),
        "calendar": _gen_calendar(),
        "energy_wh_hour": 0.0,
        "seed_ts": now,
    }


def _gen_health_history() -> list[dict[str, Any]]:
    out, w = [], 84.6
    for d in range(90, 0, -1):
        if random.random() < 0.72:
            w += random.uniform(-0.35, 0.28)
            w = max(78.0, min(88.0, w))
            bmi = round(w / (1.82 ** 2), 1)
            out.append({"ts": time.time() - d * 86400 + random.uniform(-3000, 3000),
                        "weight": round(w, 1), "bmi": bmi,
                        "fat": round(random.uniform(17.5, 21.5), 1),
                        "hr": random.randint(54, 72)})
    return out


def _gen_weather() -> dict[str, Any]:
    conds = [("clear", "Skaidrs"), ("partly-cloudy", "Daļēji mākoņains"),
             ("rain", "Liets"), ("clouds", "Mākoņains"), ("snow", "Sniegs")]
    c = random.choice(conds)
    t = random.uniform(2, 18)
    return {"temp_c": round(t, 1), "feels": round(t - random.uniform(0, 3), 1),
            "cond": c[0], "cond_lv": c[1], "humidity": random.randint(45, 92),
            "wind_ms": round(random.uniform(0.5, 9), 1),
            "forecast": [{"day": i, "min": round(t - random.uniform(2, 6), 1),
                          "max": round(t + random.uniform(1, 5), 1),
                          "cond": random.choice(conds)[0]} for i in range(5)]}


def _gen_net_devices() -> list[dict[str, Any]]:
    base = [
        ("192.168.1.10", "AA:BB:CC:11:22:33", "Andris Phone", "Apple, Inc.", "phone", "Living Room", -52, 5),
        ("192.168.1.12", "AA:BB:CC:44:55:66", "Maijs Laptop", "Lenovo", "laptop", "Office", -61, 5),
        ("192.168.1.21", "B8:27:EB:A1:B2:C3", "Raspberry Pi 5", "Raspberry Pi Foundation", "server", "Hall", -44, 5),
        ("192.168.1.30", "EC:1A:59:B0:C1:D2", "Hue Bridge", "Philips Lighting", "iot", "Living Room", -58, 2),
        ("192.168.1.31", "64:16:66:D0:E1:F2", "Roidmi Eve", "Shenzhen Roidmi", "iot", "Hall", -70, 2),
        ("192.168.1.35", "F4:1B:67:AA:BB:CC", "Google Home Hub", "Google", "speaker", "Kitchen", -66, 5),
        ("192.168.1.40", "C8:CB:19:11:22:33", "Smart TV", "Samsung CNK", "tv", "Living Room", -73, 5),
    ]
    devs = []
    for ip, mac, name, vendor, kind, room, rssi, band in base:
        devs.append({"ip": ip, "mac": mac, "name": name, "vendor": vendor,
                     "kind": kind, "room": room, "online": True,
                     "rssi": rssi + random.randint(-3, 3), "band_ghz": band,
                     "rx_rate": random.choice([78, 144, 300, 433, 585, 866]),
                     "first_seen": time.time() - random.uniform(3e5, 9e6)})
    return devs


def _gen_bt() -> list[dict[str, str]]:
    return [
        {"mac": "11:22:33:44:55:66", "name": "WH-1000XM4", "paired": True, "trusted": True, "connected": True},
        {"mac": "77:88:99:AA:BB:CC", "name": "Mi Band 8", "paired": True, "trusted": True, "connected": False},
        {"mac": "DD:EE:FF:00:11:22", "name": "Kitchen Speaker", "paired": False, "trusted": False, "connected": False},
    ]


def _gen_calendar() -> list[dict[str, Any]]:
    today = time.strftime("%Y-%m-%d")
    return [
        {"date": today, "start": "09:30", "title": "Darba sapulce / Team standup", "location": "Office"},
        {"date": today, "start": "18:00", "title": "Treneris / Gym session", "location": "Riga"},
        {"date": time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400)),
         "start": "11:00", "title": "Zobārsts / Dentist", "location": "Centrs"},
    ]


# ------------------------------------------------------------------ helpers
def demo() -> bool:
    return config.DEMO_MODE


async def api_get(url: str, params: dict | None = None, headers: dict | None = None,
                  timeout: float = 10.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(url, params=params, headers=headers or {})
        r.raise_for_status()
        return r.json()


async def api_post(url: str, json_body: dict | None = None, headers: dict | None = None,
                   payload: dict | None = None, timeout: float = 15.0) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=json_body, data=payload, headers=headers or {})
        r.raise_for_status()
        try:
            return r.json()
        except ValueError:
            return {"ok": True, "status_code": r.status_code, "text": r.text[:400]}
