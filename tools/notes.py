"""Local notes & quick reminders stored in SQLite.

The agent can jot facts ("garage code is 4921") and set reminders that fire
through the scheduler → TTS + UI notification. Also backs the Notes tile.
"""
from __future__ import annotations

import time

import config
from eventbus import emit, EV_ALERT
from tools.registry import tool
import db


async def add_note_impl(title: str, body: str = "", remind_at=None) -> int:
    return await db.add_note(title, body, remind_at)


@tool
async def note_save(title: str, body: str = "") -> dict:
    """Save a quick local note (codes, ideas, shopping items). Stored in SQLite, searchable later.
    :param title: short label, e.g. 'garāžas vārtu kods'
    :param body: full content, e.g. '4921'
    """
    nid = await add_note_impl(title, body)
    await db.remember("note", f"{title}: {body}" if body else title, key=f"note:{nid}")
    return {"ok": True, "id": nid}


@tool
async def note_list(limit: int = 30, search: str = "") -> dict:
    """List saved notes/reminders; optional case-insensitive search across titles+bodies."""
    rows = await db.list_notes(limit=max(1, min(200, limit)))
    if search:
        s = search.lower()
        rows = [r for r in rows if s in r["title"].lower() or s in r["body"].lower()]
    return {"ok": True, "notes": rows}


@tool
async def note_complete(note_id: int) -> dict:
    """Mark a note/reminder as done."""
    await db.complete_note(note_id)
    return {"ok": True, "id": note_id}


async def fire_reminder(note_id: int) -> None:
    """Scheduler entry point when a reminder job fires: speak + UI alert + mark done."""
    rows = {r["id"]: r for r in await db.list_notes(500)}
    n = rows.get(note_id)
    text = n["title"] if n else "Atgādinājums"
    emit(EV_ALERT, source="notes", message=f"⏰ reminder: {text}",
         data={"note_id": note_id, "text": text})
    try:
        from tools.tts import tts_speak
        await tts_speak.__wrapped__(text=text, language="")
    except Exception:  # noqa: BLE001
        pass
    if n:
        await db.complete_note(note_id)
