"""Async SQLAlchemy models & session helpers.

Tables: agent memory (facts), chat history, health readings, scheduled jobs,
notes/reminders, presence log, network snapshots, system events.
"""
from __future__ import annotations

import json
import time
from typing import Any

from sqlalchemy import (Boolean, Float, Integer, String, Text, BigInteger,
                        select, delete, func)
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

import config


class Base(DeclarativeBase):
    pass


class Memory(Base):
    """Long-term agent memory: durable facts the agent chooses to remember."""
    __tablename__ = "memory"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    kind: Mapped[str] = mapped_column(String(32), default="fact")   # fact|preference|note
    key: Mapped[str] = mapped_column(String(128), default="")
    content: Mapped[str] = mapped_column(Text, default="")


class ChatMessage(Base):
    __tablename__ = "chat_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    role: Mapped[str] = mapped_column(String(16))                   # user|assistant|tool
    content: Mapped[str] = mapped_column(Text, default="")
    tool_name: Mapped[str] = mapped_column(String(64), default="")


class HealthReading(Base):
    __tablename__ = "health_readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    taken_at: Mapped[float] = mapped_column(Float, default=time.time)
    weight_kg: Mapped[float | None] = mapped_column(Float, nullable=True)
    bmi: Mapped[float | None] = mapped_column(Float, nullable=True)
    body_fat_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    heart_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="withings")


class Job(Base):
    """Persisted scheduler jobs — restored on boot by restore_jobs()."""
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(16), default="interval")  # interval|cron|date
    spec_json: Mapped[str] = mapped_column(Text, default="{}")         # trigger kwargs
    payload_json: Mapped[str] = mapped_column(Text, default="{}")      # what to run
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[str] = mapped_column(String(16), default="user")  # user|agent
    next_run: Mapped[float | None] = mapped_column(Float, nullable=True)


class Note(Base):
    """Local quick notes & reminders (garage code, 'fetch laundry in 40 min')."""
    __tablename__ = "notes"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    title: Mapped[str] = mapped_column(String(160), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    remind_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    done: Mapped[bool] = mapped_column(Boolean, default=False)


class PresenceEventLog(Base):
    __tablename__ = "presence_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    device_mac: Mapped[str] = mapped_column(String(32))
    device_name: Mapped[str] = mapped_column(String(96), default="")
    status: Mapped[str] = mapped_column(String(16))            # home|away|unknown_new


class NetworkSnapshot(Base):
    __tablename__ = "network_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    gateway_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    external_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    devices_online: Mapped[int] = mapped_column(Integer, default=0)
    detail_json: Mapped[str] = mapped_column(Text, default="{}")


class SystemEventLog(Base):
    """Durable mirror of the event bus for analytics/audit."""
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[float] = mapped_column(Float, default=time.time)
    etype: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    data_json: Mapped[str] = mapped_column(Text, default="{}")


# ---------------------------------------------------------------- engine
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker | None = None


async def init_db() -> None:
    global _engine, _sessionmaker
    _engine = create_async_engine(config.DATABASE_URL, echo=False, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # mirror event bus into system_events table
    from eventbus import bus
    orig_publish = bus.publish

    def _publish_with_db(etype, *, source="system", message="", data=None):
        ev = orig_publish(etype, source=source, message=message, data=data)
        try:
            asyncio_create_task(persist_event(ev))
        except Exception:
            pass
        return ev

    bus.publish = _publish_with_db  # type: ignore[method-assign]


def asyncio_create_task(coro):
    import asyncio
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        pass


async def persist_event(ev) -> None:
    try:
        async with get_session() as s:
            s.add(SystemEventLog(etype=ev.type, source=ev.source,
                                 message=ev.message,
                                 data_json=json.dumps(ev.data, default=str)))
            await s.commit()
    except Exception:
        pass


def get_session():
    assert _sessionmaker is not None, "init_db() not called"
    return _sessionmaker()


# ---------------------------------------------------------------- helpers
async def remember(kind: str, content: str, key: str = "") -> int:
    async with get_session() as s:
        m = Memory(kind=kind, content=content, key=key)
        s.add(m); await s.commit(); return m.id


async def recall(query: str = "", limit: int = 20) -> list[dict[str, Any]]:
    async with get_session() as s:
        q = select(Memory).order_by(Memory.ts.desc()).limit(limit)
        if query:
            q = select(Memory).where(Memory.content.contains(query)).order_by(
                Memory.ts.desc()).limit(limit)
        rows = (await s.execute(q)).scalars().all()
        return [{"id": r.id, "kind": r.kind, "key": r.key,
                 "content": r.content, "ts": r.ts} for r in rows]


async def log_chat(role: str, content: str, tool_name: str = "") -> None:
    async with get_session() as s:
        s.add(ChatMessage(role=role, content=content, tool_name=tool_name))
        await s.commit()


async def recent_chat(limit: int = 30) -> list[dict[str, Any]]:
    async with get_session() as s:
        rows = (await s.execute(select(ChatMessage).order_by(
            ChatMessage.id.desc()).limit(limit))).scalars().all()
        return [{"role": r.role, "content": r.content, "ts": r.ts,
                 "tool": r.tool_name} for r in reversed(rows)]


async def save_health(weight=None, bmi=None, fat=None, hr=None, taken_at=None) -> None:
    async with get_session() as s:
        s.add(HealthReading(weight_kg=weight, bmi=bmi, body_fat_pct=fat,
                            heart_rate=hr, taken_at=taken_at or time.time()))
        await s.commit()


async def health_history(days: int = 90) -> list[dict[str, Any]]:
    since = time.time() - days * 86400
    async with get_session() as s:
        rows = (await s.execute(select(HealthReading).where(
            HealthReading.taken_at >= since).order_by(HealthReading.taken_at))).scalars().all()
        return [{"ts": r.taken_at, "weight": r.weight_kg, "bmi": r.bmi,
                 "fat": r.body_fat_pct, "hr": r.heart_rate} for r in rows]


async def upsert_job(job_id: str, name: str, kind: str, spec: dict,
                     payload: dict, created_by: str = "user",
                     enabled: bool = True) -> None:
    async with get_session() as s:
        row = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
        if row is None:
            row = Job(job_id=job_id)
            s.add(row)
        row.name, row.kind, row.enabled, row.created_by = name, kind, enabled, created_by
        row.spec_json = json.dumps(spec)
        row.payload_json = json.dumps(payload)
        await s.commit()


async def delete_job_row(job_id: str) -> None:
    async with get_session() as s:
        await s.delete(delete(Job).where(Job.job_id == job_id))
        await s.commit()


async def list_job_rows(include_disabled: bool = True) -> list[dict[str, Any]]:
    async with get_session() as s:
        q = select(Job).order_by(Job.id)
        if not include_disabled:
            q = q.where(Job.enabled.is_(True))
        rows = (await s.execute(q)).scalars().all()
        return [{"job_id": r.job_id, "name": r.name, "kind": r.kind,
                 "enabled": r.enabled, "created_by": r.created_by,
                 "spec": json.loads(r.spec_json), "payload": json.loads(r.payload_json),
                 "next_run": r.next_run} for r in rows]


async def set_job_enabled(job_id: str, enabled: bool) -> None:
    async with get_session() as s:
        row = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
        if row:
            row.enabled = enabled
            await s.commit()


async def update_job_next_run(job_id: str, next_run: float | None) -> None:
    async with get_session() as s:
        row = (await s.execute(select(Job).where(Job.job_id == job_id))).scalar_one_or_none()
        if row:
            row.next_run = next_run
            await s.commit()


async def add_note(title: str, body: str = "", remind_at: float | None = None) -> int:
    async with get_session() as s:
        n = Note(title=title, body=body, remind_at=remind_at)
        s.add(n); await s.commit(); return n.id


async def list_notes(limit: int = 50) -> list[dict[str, Any]]:
    async with get_session() as s:
        rows = (await s.execute(select(Note).order_by(Note.id.desc()).limit(limit)
                                )).scalars().all()
        return [{"id": r.id, "title": r.title, "body": r.body,
                 "remind_at": r.remind_at, "done": r.done, "ts": r.ts} for r in rows]


async def complete_note(note_id: int) -> None:
    async with get_session() as s:
        r = await s.get(Note, note_id)
        if r:
            r.done = True; await s.commit()


async def log_presence(mac: str, name: str, status: str) -> None:
    async with get_session() as s:
        s.add(PresenceEventLog(device_mac=mac, device_name=name, status=status))
        await s.commit()


async def save_network_snapshot(gateway_ms, external_ms, count, detail: dict) -> None:
    async with get_session() as s:
        s.add(NetworkSnapshot(gateway_ms=gateway_ms, external_ms=external_ms,
                              devices_online=count, detail_json=json.dumps(detail, default=str)))
        await s.commit()


async def network_history(limit: int = 120) -> list[dict[str, Any]]:
    async with get_session() as s:
        rows = (await s.execute(select(NetworkSnapshot).order_by(
            NetworkSnapshot.ts.desc()).limit(limit))).scalars().all()
        return [{"ts": r.ts, "gateway_ms": r.gateway_ms,
                 "external_ms": r.external_ms, "devices": r.devices_online}
                for r in reversed(rows)]
