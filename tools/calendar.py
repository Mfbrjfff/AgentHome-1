"""Calendar via local .ics files / CalDAV — lightweight, no heavy libraries.

Parses VEVENT blocks with a small line-unfolding parser (RFC5545 basics).
Google Calendar can be consumed through its secret iCal URL; Nextcloud etc.
through any CalDAV .ics endpoint. The morning routine reads today's agenda
and auto-schedules reminders for upcoming events.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Any

import config
from tools.registry import tool
from tools import sim

ICS_FILE = config.DATA_DIR / "agenda.ics"


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    buf = ""
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and buf:
            buf += raw[1:]
        else:
            if buf:
                lines.append(buf)
            buf = raw
    if buf:
        lines.append(buf)
    return lines


def parse_ics(text: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    cur: dict[str, Any] | None = None
    for line in _unfold(text):
        u = line.upper()
        if u == "BEGIN:VEVENT":
            cur = {}
        elif u == "END:VEVENT" and cur is not None:
            events.append(_finish(cur))
            cur = None
        elif cur is not None and ":" in line:
            k, v = line.split(":", 1)
            key = k.split(";")[0].upper()
            if key in ("SUMMARY", "DESCRIPTION", "LOCATION"):
                cur[key.lower()] = v.strip()
            elif key == "DTSTART":
                cur["dtstart"] = _parse_dt(v)
            elif key == "DTEND":
                cur["dtend"] = _parse_dt(v)
    return sorted(events, key=lambda e: e.get("start_ts", 0))


def _parse_dt(v: str) -> float:
    v = v.strip()
    try:
        if "T" in v:
            s = v.replace("Z", "").split("+")[0]
            dt = datetime.fromisoformat(s)
        else:
            dt = datetime.strptime(v[:8], "%Y%m%d")
        return dt.timestamp()
    except ValueError:
        return time.time()


def _finish(ev: dict) -> dict:
    st = ev.get("dtstart", time.time())
    return {"title": ev.get("summary", "(bez nosaukuma)"),
            "location": ev.get("location", ""),
            "description": (ev.get("description") or "")[:200],
            "start_ts": st,
            "start": datetime.fromtimestamp(st).strftime("%H:%M"),
            "date": datetime.fromtimestamp(st).strftime("%Y-%m-%d")}


@tool
async def calendar_today(source_url: str = "") -> dict:
    """Read today's agenda from the local ICS file or a remote iCal URL (Google Calendar ical link works)."""
    if sim.demo() and not source_url:
        w = sim.world()["calendar"]
        today = time.strftime("%Y-%m-%d")
        return {"ok": True, "demo": True, "date": today,
                "events": [e for e in w if e["date"] == today],
                "tomorrow": [e for e in w if e["date"] != today]}
    text = ""
    if source_url:
        raw = await _fetch_ics(source_url)
        text = raw
    elif ICS_FILE.exists():
        text = ICS_FILE.read_text(encoding="utf-8", errors="replace")
    if not text:
        # seed a demo agenda so the flow is testable out-of-the-box
        text = _sample_ics()
        ICS_FILE.write_text(text, encoding="utf-8")
    evs = parse_ics(text)
    today = time.strftime("%Y-%m-%d")
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    return {"ok": True, "date": today,
            "events": [e for e in evs if e["date"] == today],
            "tomorrow": [e for e in evs if e["date"] == tomorrow],
            "upcoming": [e for e in evs if e["start_ts"] >= time.time()][:10]}


async def _fetch_ics(url: str) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as c:
        r = await c.get(url)
        r.raise_for_status()
        return r.text


def _sample_ics() -> str:
    d0 = datetime.now().strftime("%Y%m%d")
    d1 = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
    return f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
DTSTART:{d0}T093000
DTEND:{d0}T100000
SUMMARY:Darba sapulce / Team standup
LOCATION:Office
END:VEVENT
BEGIN:VEVENT
DTSTART:{d0}T180000
SUMMARY:Treneris / Gym session
LOCATION:Riga
END:VEVENT
BEGIN:VEVENT
DTSTART:{d1}T110000
SUMMARY:Zobārsts / Dentist
LOCATION:Centrs
END:VEVENT
END:VCALENDAR"""


@tool
async def calendar_autoplan() -> dict:
    """Read the agenda and schedule a heads-up reminder before each upcoming event (30 min lead)."""
    res = await calendar_today.__wrapped__()
    created = []
    for ev in res.get("upcoming", []) or res.get("events", []):
        lead = ev["start_ts"] - 1800
        if lead <= time.time():
            continue
        r = await _sched_event_reminder(ev, lead)
        if r.get("ok"):
            created.append({"title": ev["title"], "remind_at": datetime.fromtimestamp(lead).isoformat()})
    return {"ok": True, "scheduled": created}


async def _sched_event_reminder(ev: dict, lead_ts: float) -> dict:
    from tools.scheduler import schedule_reminder
    mins = max(1, int((lead_ts - time.time()) / 60))
    return await schedule_reminder.__wrapped__(
        text=f"Uzmanlīgi: {ev['title']} plkst. {ev['start']} ({ev.get('location','')})",
        minutes=mins)
