"""Central typed event bus.

Every tool, routine and agent step publishes structured events here. The
WebSocket layer subscribes and forwards them to all connected dashboards,
which map each event *type* to a distinct liquid-glow background effect:

    hue_on / hue_off      -> warm amber mesh morph
    vacuum_clean          -> blue ripple (continuous while cleaning)
    withings_sync         -> green pulse
    spotify_play          -> purple shimmer
    device_arrived/left   -> cyan / rose wave
    unknown_device        -> red security flash
    agent_thought/tool    -> neutral white shimmer in the thought terminal
    routine_*             -> composite glow

Events are also ring-buffered so a freshly opened dashboard can catch up.
"""
from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from typing import Any, Callable

# Canonical event types (UI glow mapping depends on these strings).
EV_HUE_ON = "hue_on"
EV_HUE_OFF = "hue_off"
EV_VACUUM_CLEAN = "vacuum_clean"
EV_VACUUM_DOCK = "vacuum_dock"
EV_WITHINGS_SYNC = "withings_sync"
EV_SPOTIFY_PLAY = "spotify_play"
EV_SPOTIFY_PAUSE = "spotify_pause"
EV_DEVICE_ARRIVED = "device_arrived"
EV_DEVICE_LEFT = "device_left"
EV_UNKNOWN_DEVICE = "unknown_device"
EV_NETWORK_HEALTH = "network_health"
EV_AGENT_THOUGHT = "agent_thought"
EV_TOOL_CALL = "tool_call"
EV_TOOL_RESULT = "tool_result"
EV_ROUTINE_START = "routine_start"
EV_ROUTINE_DONE = "routine_done"
EV_SCHEDULE = "schedule"
EV_SKILL_INSTALLED = "skill_installed"
EV_TTS = "tts"
EV_SYSTEM = "system"
EV_ALERT = "alert"


@dataclass
class Event:
    type: str
    source: str = "system"
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["time"] = time.strftime("%H:%M:%S", time.localtime(d["ts"]))
        return d


class EventBus:
    """Async pub/sub with replay buffer; safe from any task/thread context."""

    def __init__(self, history: int = 200) -> None:
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._history: deque[Event] = deque(maxlen=history)
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from the FastAPI lifespan so thread publishers work."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=512)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        self._subscribers.discard(q)

    # ------------------------------------------------------------- publish
    def publish(self, etype: str, *, source: str = "system",
                message: str = "", data: dict[str, Any] | None = None) -> Event:
        ev = Event(type=etype, source=source, message=message, data=data or {})
        self._history.append(ev)
        for q in list(self._subscribers):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                pass  # slow consumer drops rather than blocking the agent
        print(f"[eventbus] {ev.type:<16} {source}: {message}")
        return ev

    def publish_threadsafe(self, etype: str, **kw: Any) -> None:
        """For callers outside the event loop (worker threads)."""
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self.publish, etype, **kw)
        else:
            self.publish(etype, **kw)

    def history(self, n: int = 50) -> list[dict[str, Any]]:
        return [e.to_dict() for e in list(self._history)[-n:]]


bus = EventBus()


def emit(etype: str, *, source: str = "system", message: str = "",
         data: dict[str, Any] | None = None) -> None:
    """Module-level convenience used by every tool module."""
    bus.publish(etype, source=source, message=message, data=data)
