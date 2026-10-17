"""Withings smart scale (OAuth2 Data API) + trend analysis with AI insights.

Real path: OAuth2 (authorize → token → /measure/v2/subscribe). We persist the
access token in data/withings_token.json; demo mode generates a believable
measurement stream and stores it through db.save_health().
"""
from __future__ import annotations

import json
import random
import time
from typing import Any

import config
from eventbus import emit, EV_WITHINGS_SYNC
from tools.registry import tool
from tools import sim
import db

TOKEN_FILE = config.DATA_DIR / "withings_token.json"


def _load_token() -> dict[str, Any]:
    if TOKEN_FILE.exists():
        return json.loads(TOKEN_FILE.read_text())
    return {}


async def _refresh_if_needed() -> str:
    t = _load_token()
    if not t.get("access_token"):
        raise RuntimeError("Withings not authorized — set WITHINGS_* env vars or use DEMO_MODE")
    if t.get("expires_at", 0) < time.time() + 120:
        d = await sim.api_post("https://wbsapi.withings.net/v2/oauth2",
                               payload={"grant_type": "refresh_token",
                                        "refresh_token": t["refresh_token"],
                                        "client_id": config.WITHINGS_CLIENT_ID},
                               headers={"Authorization": "Basic " + json.dumps(
                                   {"u": config.WITHINGS_CLIENT_ID,
                                    "p": config.WITHINGS_CLIENT_SECRET})[:0] or ""})
        t.update({"access_token": d["body"]["access_token"],
                  "refresh_token": d["body"].get("refresh_token", t["refresh_token"]),
                  "expires_at": time.time() + d["body"].get("expires_in", 7200)})
        TOKEN_FILE.write_text(json.dumps(t))
    return t["access_token"]


_MEAS_TYPES = {"weight": 1, "height": 5, "fat": 6, "bp_diastolic": 9,
               "bp_systolic": 10, "heart_rate": 11}


@tool
async def withings_sync(days: int = 7) -> dict:
    """Fetch recent measurements from the Withings scale (weight, BMI, body fat %, heart rate), store them locally and return latest + trend."""
    readings: list[dict[str, Any]] = []
    if sim.demo():
        hist = sim.world()["withings"]["history"]
        # occasionally add a fresh "stepped on the scale today" measurement
        if random.random() < 0.8:
            last = hist[-1] if hist else {"weight": 84.0, "bmi": 25.3, "fat": 19.5, "hr": 62}
            w = round(max(78, min(88, last["weight"] + random.uniform(-0.4, 0.3))), 1)
            fresh = {"ts": time.time(), "weight": w,
                     "bmi": round(w / (1.82 ** 2), 1),
                     "fat": round(last["fat"] + random.uniform(-0.2, 0.2), 1),
                     "hr": random.randint(54, 72)}
            hist.append(fresh)
        readings = [r for r in hist if r["ts"] > time.time() - days * 86400]
    else:
        tok = await _refresh_if_needed()
        d = await sim.api_get("https://wbsapi.withings.net/measure",
                              params={"action": "getmeas",
                                      "startdate": int(time.time() - days * 86400),
                                      "category": json.dumps(list(_MEAS_TYPES.values()))},
                              headers={"Authorization": f"Bearer {tok}"})
        for g in d.get("body", {}).get("uptable", []):
            m = {x["type"]: x["value"] for x in g.get("measures", [])}
            wt = m.get(1, 0) / 100000.0
            fat = m.get(6, 0) / 100000.0
            hr = m.get(11)
            readings.append({"ts": g["date"], "weight": round(wt, 1),
                             "bmi": round(wt / (1.82 ** 2), 1) if wt else None,
                             "fat": round(fat * 100, 1) if fat else None,
                             "hr": hr})
    for r in readings:
        await db.save_health(r.get("weight"), r.get("bmi"), r.get("fat"),
                             r.get("hr"), taken_at=r["ts"])
    latest = readings[-1] if readings else None
    trend = _trend([r["weight"] for r in readings if r.get("weight")])
    insight = health_insight(latest, trend)
    emit(EV_WITHINGS_SYNC, source="withings",
         message=f"synced {len(readings)} readings · {latest['weight'] if latest else '—'} kg",
         data={"latest": latest, "trend": trend})
    return {"ok": True, "count": len(readings), "latest": latest,
            "trend": trend, "insight": insight}


def _trend(values: list[float]) -> dict[str, Any]:
    if len(values) < 2:
        return {"direction": "insufficient-data", "delta_kg": 0, "slope_per_week": 0}
    first, last = values[0], values[-1]
    weeks = max(0.15, len(values) / 7)
    slope = (last - first) / weeks
    return {"direction": "decreasing" if slope < -0.05 else
            "increasing" if slope > 0.05 else "stable",
            "delta_kg": round(last - first, 2),
            "slope_per_week": round(slope, 2),
            "avg_kg": round(sum(values) / len(values), 1)}


def health_insight(latest: dict | None, trend: dict) -> str:
    """Deterministic micro-insight generator (the LLM can also be asked to elaborate)."""
    if not latest:
        return "Nav jaunu mērījumu — uzkāpiet uz svariem, lai sinhronizētu."
    parts = [f"Svars {latest['weight']} kg, ĶMI {latest['bmi']}, "
             f"tauki {latest.get('fat')}%, pulsa {latest.get('hr')}."]
    if trend["direction"] == "decreasing":
        parts.append(f"Tendence pozitīva: −{abs(trend['delta_kg'])} kg pēdējās dienās.")
    elif trend["direction"] == "increasing":
        parts.append(f"Svars aug (+{trend['delta_kg']} kg) — vērts pārskatīt vakariņu paradumus.")
    else:
        parts.append("Svars stabils — labs līdzsvars.")
    if latest.get("bmi") and latest["bmi"] > 27:
        parts.append("ĶMI virs normas robežas; ieteicu soļu mērķi 8000/dienā.")
    return " ".join(parts)


@tool
async def withings_history(days: int = 90) -> dict:
    """Return stored weight/BMI/fat/HR history for charts and analysis."""
    rows = await db.health_history(days)
    return {"ok": True, "days": days, "points": len(rows), "history": rows[-400:]}
