"""Advanced automation routines — Morning, Away Cleaning (4h cooldown),
Evening Wind-Down, Welcome Home, Dynamic Alarm (Hue sunrise + Spotify).

Routines are plain async functions callable by the scheduler payload
{"kind":"routine","name":"morning"}, by presence events, or manually via
POST /api/routines/{name}/run and the chat ("run the morning routine").
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta

import config
from eventbus import emit, EV_ROUTINE_START, EV_ROUTINE_DONE, EV_HUE_ON, EV_ALERT
from tools.registry import call_tool
from tools import sim

_last_away_run = {"ts": 0.0}
AWAY_COOLDOWN_S = 4 * 3600


async def run_routine(name: str) -> dict:
    name = (name or "").lower().replace("-", "_")
    fn = ROUTINES.get(name)
    if fn is None:
        return {"ok": False, "error": f"unknown routine '{name}'",
                "known": list(ROUTINES)}
    emit(EV_ROUTINE_START, source="routines", message=f"▶ routine: {name}")
    t0 = time.time()
    try:
        res = await fn()
    except Exception as exc:  # noqa: BLE001
        emit(EV_ROUTINE_DONE, source="routines", message=f"{name} FAILED: {exc}",
             data={"routine": name, "ok": False})
        return {"ok": False, "error": str(exc)}
    ms = int((time.time() - t0) * 1000)
    emit(EV_ROUTINE_DONE, source="routines",
         message=f"✔ routine {name} finished in {ms}ms",
         data={"routine": name, "ok": True, "ms": ms})
    return {"ok": True, "routine": name, "ms": ms, "detail": res}


# ------------------------------------------------------------------ morning
async def morning_routine() -> dict:
    """weather → Withings sync → Hue warm sunrise → Spotify playlist → RSS → spoken summary."""
    steps: dict[str, object] = {}
    wx = await call_tool("weather_forecast", {})
    steps["weather"] = wx["data"].get("current")
    hw = await call_tool("withings_sync", {"days": 7})
    steps["health"] = hw["data"].get("latest")
    cal = await call_tool("calendar_today", {})
    steps["agenda"] = cal["data"].get("events", [])
    await call_tool("hue_scene", {"scene": "sunrise-warm"})
    await call_tool("spotify_play", {"playlist": "Morning Latvian Chill",
                                     "device_name": "Kitchen Hub"})
    rss = await call_tool("rss_digest", {"max_items": 4})
    steps["news"] = [i["title"] for i in rss["data"].get("items", [])][:4]
    await call_tool("calendar_autoplan", {})
    cur = steps["weather"] or {}
    evs = ", ".join(e["start"] + " " + e["title"] for e in steps["agenda"]) or "dienas kalendārā tukšs"
    news = "; ".join(steps["news"][:2])
    summary = (f"Labrīt! Šodien {cur.get('temp_c', '?')} grādi, {cur.get('condition_lv', '—')}. "
               f"Svars {steps['health']['weight'] if steps['health'] else '—'} kilogrami. "
               f"Kalendārā: {evs}. Ziņas: {news}. Gaisma ieslēgta, mūzika skan.")
    await call_tool("tts_speak", {"text": summary})
    await call_tool("note_save", {"title": "Morning digest", "body": summary})
    return steps | {"summary": summary}


# ------------------------------------------------------------------ away
async def away_routine() -> dict:
    """Nobody home → lights off, spotify pause, Roidmi clean (4h cooldown guard)."""
    now = time.time()
    if now - _last_away_run["ts"] < AWAY_COOLDOWN_S:
        left = int((AWAY_COOLDOWN_S - (now - _last_away_run["ts"])) / 60)
        return {"skipped": True, "reason": f"cooldown active — next eligible in {left} min"}
    pres = await call_tool("presence_check", {})
    if pres["data"].get("anyone_home"):
        return {"skipped": True, "reason": "someone is still home"}
    await call_tool("hue_set", {"room": "all", "on": False})
    await call_tool("spotify_pause", {})
    vac = await call_tool("vacuum_start", {"room": "all", "mode": "standard"})
    _last_away_run["ts"] = now
    return {"presence": pres["data"], "vacuum": vac["data"]}


# ------------------------------------------------------------------ evening
async def evening_routine() -> dict:
    """Dim/off Hue, cap Spotify volume ≤25%, read tomorrow's agenda aloud."""
    await call_tool("hue_auto_adjust", {"mood": "relax"})
    await call_tool("hue_set", {"room": "bedroom", "on": True, "brightness": 8,
                                "color_temp_mired": 450})
    st = await call_tool("spotify_status", {})
    vol = st["data"].get("volume") or 0
    if st["data"].get("playing") and vol > 25:
        await call_tool("spotify_volume", {"percent": 25})
    cal = await call_tool("calendar_today", {})
    tom = cal["data"].get("tomorrow", [])
    agenda = ", ".join(f"{e.get('start', e.get('date'))} {e['title']}" for e in tom) \
        if tom else "rīt diena brīva"
    text = f"Vakara atgādinājums. Rītdienas plāns: {agenda}. Gaismas nomērķētas, skaļums ierobežots. Labu nakti."
    await call_tool("tts_speak", {"text": text})
    await call_tool("hue_scene", {"scene": "goodnight"})
    return {"tomorrow": tom, "spoken": text}


# ------------------------------------------------------------------ welcome
async def welcome_routine() -> dict:
    """Owner phone reconnects to Wi-Fi → lights on, weather brief, upbeat music."""
    emit(EV_ALERT, source="routines", message="🏠 Welcome home triggered")
    await call_tool("hue_auto_adjust", {"mood": "warm"})
    wx = await call_tool("weather_forecast", {})
    cur = wx["data"].get("current", {})
    await call_tool("spotify_play", {"query": "feel good hits", "device_name": "Living Room Speaker"})
    text = (f"Mājās atnācis! Istabas apsildītas, āra temperatura "
            f"{cur.get('temp_c', '?')} grādi, {cur.get('condition_lv', '')}. Palaidu mūziku.")
    await call_tool("tts_speak", {"text": text})
    return {"weather": cur, "spoken": text}


# ------------------------------------------------------------------ dynamic alarm
import asyncio  # noqa: E402


async def dynamic_alarm() -> dict:
    """Smart alarm: 10-min Hue sunrise ramp, then Spotify/voice. The scheduler
    fires this at ramp start; in demo mode the ramp is compressed to seconds."""
    for pct in (0, 20, 40, 65, 90):
        await call_tool("hue_set", {"room": "bedroom", "on": True,
                                    "brightness": max(1, pct),
                                    "color_temp_mired": max(153, 450 - pct * 2)})
        emit(EV_HUE_ON, source="alarm", message=f"sunrise ramp {pct}%",
             data={"rooms": ["bedroom"], "on": True, "brightness_pct": pct})
        if not sim.demo():
            await asyncio.sleep(120)
    wx = await call_tool("weather_forecast", {})
    cal = await call_tool("calendar_today", {})
    first = (cal["data"].get("events") or [{}])[0]
    await call_tool("spotify_play", {"playlist": "Slow Morning"})
    cur = wx["data"].get("current", {})
    text = (f"Labrīt! Pulks {datetime.now():%H:%M}. Ārā {cur.get('temp_c', '?')} grādi, "
            f"{cur.get('condition_lv', '')}. ")
    text += (f"Pirmā sapulce: {first.get('title', '')} plkst. {first.get('start', '')}."
             if first.get("title") else "Šodien kalendārā brīvs laiks — priecājos par tevi!")
    await call_tool("tts_speak", {"text": text})
    return {"spoken": text}


@tool
async def run_named_routine(name: str) -> dict:
    """Trigger a smart routine immediately: morning | away | evening | welcome | alarm."""
    return await run_routine(name)


@tool
async def set_dynamic_alarm(time_of_day: str = "07:00", label: str = "Darba diena") -> dict:
    """Create/update the dynamic alarm: schedules sunrise-ramp job 10 min before wake time (weekdays).
    :param time_of_day: HH:MM local wake time
    """
    hh, mm = map(int, time_of_day.split(":")[:2])
    ramp_min = (mm - 10) % 60
    ramp_hour = hh - 1 if mm >= 10 else (hh - 1) % 24
    r1 = await call_tool("schedule_job", {
        "name": f"Alarm sunrise ramp ({label})", "kind": "cron",
        "spec_json": f'{{"hour":"{ramp_hour}","minute":"{ramp_min}","day_of_week":"mon-fri"}}',
        "payload_json": '{"kind":"routine","name":"alarm"}'})
    return {"ok": bool(r1["ok"]), "alarm_at": f"{hh:02d}:{mm:02d}",
            "ramp_starts": f"{ramp_hour:02d}:{ramp_min:02d}", "job": r1["data"]}


ROUTINES = {
    "morning": morning_routine,
    "away": away_routine,
    "evening": evening_routine,
    "welcome": welcome_routine,
    "alarm": dynamic_alarm,
}
