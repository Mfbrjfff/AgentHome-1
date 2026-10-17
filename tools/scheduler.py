"""Proactive scheduler built on APScheduler (AsyncIOScheduler).

BOOT ORDERING RULE: ``scheduler.start()`` is NEVER called at module level.
main.py's FastAPI lifespan calls :func:`start_scheduler` → :func:`restore_jobs`,
and :func:`stop_scheduler` after yield.

Jobs are persisted in SQLite (db.Job) so they survive restarts, and can be
created by BOTH the user (REST/UI) and the agent (tools below). Payload kinds:
  routine   -> run a named smart routine (morning/away/evening/alarm)
  chat      -> ask the agent something later ("remind me to ..." style)
  tool      -> call any registered tool with fixed args
  tts       -> speak a message at fire time
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.date import DateTrigger

import config
from eventbus import emit, EV_SCHEDULE
from tools.registry import tool, call_tool
import db

scheduler = AsyncIOScheduler(timezone="Europe/Riga")
_started = False


# ---------------------------------------------------------------- lifecycle
def start_scheduler() -> None:
    """Idempotent start — guarded by if not scheduler.running (never at import!)."""
    global _started
    if not scheduler.running:
        scheduler.start()
        _started = True
        print("[scheduler] AsyncIOScheduler started")


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("[scheduler] stopped")


async def restore_jobs() -> int:
    """Re-register every enabled job from SQLite after boot."""
    rows = await db.list_job_rows(include_disabled=False)
    n = 0
    for r in rows:
        try:
            _add_apjob(r["job_id"], r["kind"], r["spec"], r["payload"])
            n += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] failed restoring {r['job_id']}: {exc}")
    emit(EV_SCHEDULE, source="scheduler", message=f"restored {n} persisted jobs")
    return n


def _trigger_for(kind: str, spec: dict):
    if kind == "cron":
        return CronTrigger(**{k: v for k, v in spec.items() if v not in (None, "")})
    if kind == "interval":
        return IntervalTrigger(seconds=float(spec.get("seconds", 3600)))
    if kind == "date":
        ts = spec.get("run_at") or spec.get("timestamp")
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts)
        else:
            dt = datetime.fromtimestamp(float(ts))
        return DateTrigger(run_date=dt)
    raise ValueError(f"unknown trigger kind {kind}")


def _add_apjob(job_id: str, kind: str, spec: dict, payload: dict) -> None:
    trig = _trigger_for(kind, spec)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(_fire, trig, id=job_id, args=[job_id, payload],
                      replace_existing=True, misfire_grace_time=300, coalesce=True)


async def _fire(job_id: str, payload: dict) -> None:
    """Every persisted job runs through this dispatcher."""
    kind = payload.get("kind", "tts")
    emit(EV_SCHEDULE, source="scheduler",
         message=f"firing job {job_id} ({kind}: {payload.get('label', '')})",
         data={"job_id": job_id, "payload": payload})
    try:
        if kind == "routine":
            from tools import routines
            await routines.run_routine(payload["name"])
        elif kind == "tool":
            await call_tool(payload["tool"], payload.get("args", {}))
        elif kind == "chat":
            import agent
            await agent.agent_turn(payload["prompt"], source="schedule")
        elif kind == "tts":
            await call_tool("tts_speak", {"text": payload.get("text", "")})
        elif kind == "reminder":
            from tools.notes import fire_reminder
            await fire_reminder(int(payload.get("note_id", 0)))
    except Exception as exc:  # noqa: BLE001
        emit(EV_SCHEDULE, source="scheduler", message=f"job {job_id} FAILED: {exc}")
    finally:
        row = next((r for r in await db.list_job_rows() if r["job_id"] == job_id), None)
        if row:
            j = scheduler.get_job(job_id)
            nxt = j.next_run_time.timestamp() if j and j.next_run_time else None
            await db.update_job_next_run(job_id, nxt)
            if kind == "date" and nxt is None:      # one-shot finished
                await db.delete_job_row(job_id)


# ---------------------------------------------------------------- tools
@tool
async def schedule_job(name: str, kind: str, spec_json: str, payload_json: str) -> dict:
    """Create a scheduled job. kind=interval|cron|date.
    :param name: human label like 'Evening cleaning'
    :param kind: interval | cron | date
    :param spec_json: trigger spec. interval: '{"seconds":3600}'. cron: '{"hour":"7","minute":"30"}'. date: '{"run_at":"2026-01-01T08:00"}'
    :param payload_json: what to run: '{"kind":"routine","name":"morning"}' | '{"kind":"tool","tool":"vacuum_start","args":{"room":"kitchen"}}' | '{"kind":"tts","text":"Ūdens ziediem!"}' | '{"kind":"chat","prompt":"check disk space"}'
    """
    try:
        spec = json.loads(spec_json or "{}")
        payload = json.loads(payload_json or "{}")
    except json.JSONDecodeError as e:
        return {"ok": False, "error": f"bad JSON: {e}"}
    if payload.get("kind") not in ("routine", "tool", "tts", "chat", "reminder"):
        return {"ok": False, "error": "payload.kind must be routine|tool|tts|chat|reminder"}
    job_id = f"job-{uuid.uuid4().hex[:10]}"
    try:
        _add_apjob(job_id, kind, spec, payload)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    payload.setdefault("label", name)
    j = scheduler.get_job(job_id)
    await db.upsert_job(job_id, name, kind, spec, payload, created_by="agent")
    if j and j.next_run_time:
        await db.update_job_next_run(job_id, j.next_run_time.timestamp())
    emit(EV_SCHEDULE, source="scheduler",
         message=f"created '{name}' ({kind}) → {payload.get('kind')}",
         data={"job_id": job_id})
    return {"ok": True, "job_id": job_id,
            "next_run": j.next_run_time.isoformat() if j and j.next_run_time else None}


@tool
async def list_jobs() -> dict:
    """List all scheduled jobs (enabled and disabled) with next-run times."""
    rows = await db.list_job_rows()
    out = []
    for r in rows:
        j = scheduler.get_job(r["job_id"])
        out.append({"job_id": r["job_id"], "name": r["name"], "kind": r["kind"],
                    "enabled": r["enabled"], "created_by": r["created_by"],
                    "payload": r["payload"],
                    "next_run": j.next_run_time.isoformat() if j and j.next_run_time else None})
    return {"ok": True, "jobs": out}


@tool
async def delete_job(job_id: str) -> dict:
    """Delete a scheduled job by its id (agent may delete its own routines freely)."""
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    await db.delete_job_row(job_id)
    emit(EV_SCHEDULE, source="scheduler", message=f"deleted job {job_id}")
    return {"ok": True, "deleted": job_id}


@tool
async def toggle_job(job_id: str, enabled: bool = True) -> dict:
    """Enable or disable a scheduled job without deleting it."""
    await db.set_job_enabled(job_id, enabled)
    j = scheduler.get_job(job_id)
    if enabled:
        row = next((r for r in await db.list_job_rows() if r["job_id"] == job_id), None)
        if row and j is None:
            _add_apjob(job_id, row["kind"], row["spec"], row["payload"])
    elif j:
        scheduler.remove_job(job_id)
    emit(EV_SCHEDULE, source="scheduler",
         message=f"{'enabled' if enabled else 'disabled'} job {job_id}")
    return {"ok": True, "job_id": job_id, "enabled": enabled}


@tool
async def schedule_reminder(text: str, minutes: float = 0, time_of_day: str = "") -> dict:
    """Quick reminder: fires TTS + UI notification. Either in N minutes, or at HH:MM today/tomorrow.
    :param text: what to remind about, e.g. 'izņemt drēbes no veļasmašīnas'
    :param minutes: relative delay in minutes (use this OR time_of_day)
    :param time_of_day: 'HH:MM' local clock time; if already passed → tomorrow
    """
    import asyncio
    from tools.notes import add_note_impl
    if minutes > 0:
        when = time.time() + minutes * 60
    elif time_of_day:
        hh, mm = map(int, time_of_day.split(":")[:2])
        now = datetime.now()
        cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand <= now:
            cand += timedelta(days=1)
        when = cand.timestamp()
    else:
        return {"ok": False, "error": "give minutes or time_of_day"}
    note_id = await add_note_impl(text, "", remind_at=when)
    job_id = f"rem-{uuid.uuid4().hex[:8]}"
    _add_apjob(job_id, "date", {"run_at": when},
               {"kind": "reminder", "note_id": note_id, "label": f"Reminder: {text[:40]}"})
    await db.upsert_job(job_id, f"Reminder: {text[:60]}", "date",
                        {"run_at": when}, {"kind": "reminder", "note_id": note_id},
                        created_by="agent")
    await db.update_job_next_run(job_id, when)
    emit(EV_SCHEDULE, source="scheduler",
         message=f"reminder set for {datetime.fromtimestamp(when):%H:%M}: {text[:60]}")
    return {"ok": True, "job_id": job_id, "note_id": note_id,
            "fires_at": datetime.fromtimestamp(when).isoformat()}
