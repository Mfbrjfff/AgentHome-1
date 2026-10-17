"""Smart Home Autonomous AI Agent Harness — entry point.

Run:  python main.py            (DEMO_MODE=true by default → zero hardware needed)

BOOT ORDERING (critical): the APScheduler is started INSIDE the FastAPI
lifespan handler via tools.scheduler.start_scheduler(), then persisted jobs are
restored; it is stopped after yield on shutdown. scheduler.start() is never
called at module/import level, avoiding the classic "attached to a different
loop" event-loop error.
"""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import config
import db
import agent
from eventbus import bus, emit, EV_SYSTEM
from tools.registry import load_core_tools, load_skills, tool_schemas, call_tool, REGISTRY
from tools.scheduler import start_scheduler, stop_scheduler, restore_jobs, scheduler
from tools.network import presence_monitor_loop, network_scan, network_health
from tools.system import maybe_autoclean


# ---------------------------------------------------------------- lifespan
@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_running_loop()
    bus.bind_loop(loop)
    await db.init_db()
    load_core_tools()
    load_skills()
    await agent.warm_memory_seed()

    # ---- CRITICAL: start scheduler here, not at import time
    start_scheduler()
    restored = await restore_jobs()
    seed_default_jobs()

    stop_event = asyncio.Event()
    app.state.stop_event = stop_event
    tasks = [
        loop.create_task(presence_monitor_loop(stop_event), name="presence"),
        loop.create_task(periodic_maintenance(stop_event), name="maintenance"),
        loop.create_task(bus_pump(), name="log-writer"),
    ]
    emit(EV_SYSTEM, source="boot",
         message=f"harness ready · {len(REGISTRY)} tools · demo={config.DEMO_MODE} · jobs restored={restored}")
    yield
    # ---- shutdown
    stop_event.set()
    for t in tasks:
        t.cancel()
    stop_scheduler()


def seed_default_jobs() -> None:
    """Register the three flagship routines if no jobs exist yet (user can edit them)."""
    from tools.scheduler import _add_apjob
    defaults = [
        ("routine-morning", "Morning Routine", "cron",
         {"hour": "7", "minute": "15", "day_of_week": "mon-fri"},
         {"kind": "routine", "name": "morning", "label": "Morning routine"}),
        ("routine-evening", "Evening Wind-Down", "cron",
         {"hour": "22", "minute": "0"},
         {"kind": "routine", "name": "evening", "label": "Evening wind-down"}),
        ("maint-disk", "Disk Watchdog", "interval",
         {"seconds": str(6 * 3600)},
         {"kind": "tool", "tool": "disk_check", "args": {}, "label": "disk check"}),
    ]
    loop = asyncio.get_running_loop()

    async def _seed():
        rows = await db.list_job_rows()
        existing = {r["job_id"] for r in rows}
        for jid, name, kind, spec, payload in defaults:
            if jid in existing or scheduler.get_job(jid):
                continue
            _add_apjob(jid, kind, spec, payload)
            await db.upsert_job(jid, name, kind, spec, payload, created_by="agent")
        # wire auto-clean into maintenance interval job
        if "maint-autoclean" not in {r['job_id'] for r in await db.list_job_rows()}:
            _add_apjob("maint-autoclean", "interval", {"seconds": "21600"},
                       {"kind": "chat", "prompt": "pārbaudi diska vietu un iztīri ja vajag",
                        "label": "autoclean"})
            await db.upsert_job("maint-autoclean", "Auto Disk Cleaner", "interval",
                                {"seconds": 21600},
                                {"kind": "chat", "prompt": "pārbaudi diska vietu un iztīri ja vajag"},
                                created_by="agent")
    loop.create_task(_seed())


async def periodic_maintenance(stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await network_scan.__wrapped__()
            await network_health.__wrapped__()
            await maybe_autoclean()
        except Exception as exc:  # noqa: BLE001
            print(f"[maintenance] {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=90)
        except asyncio.TimeoutError:
            pass


LOG_FILE = config.LOG_DIR / "agent.log"


async def bus_pump() -> None:
    """Mirror every event into logs/agent.log for durable audit."""
    q = bus.subscribe()
    try:
        while True:
            ev = await q.get()
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(ev.to_dict(), default=str) + "\n")
    except asyncio.CancelledError:
        pass
    finally:
        bus.unsubscribe(q)


# ---------------------------------------------------------------- app
app = FastAPI(title="Smart Home Autonomous AI Agent Harness", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def index() -> Any:
    return (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/api/health")
async def health() -> dict:
    ollama_ok = False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{config.OLLAMA_URL}/api/tags")
            ollama_ok = r.status_code == 200
    except Exception:  # noqa: BLE001
        pass
    return {"status": "ok", "model": config.OLLAMA_MODEL,
            "demo_mode": config.DEMO_MODE, "ollama_reachable": ollama_ok,
            "tools": len(REGISTRY), "scheduler_running": scheduler.running,
            "uptime_ts": BOOT_TS}


BOOT_TS = time.time()


@app.post("/api/chat")
async def chat(req: Request) -> dict:
    body = await req.json()
    text = (body.get("text") or "").strip()
    speak = bool(body.get("speak", True))
    if not text:
        return {"reply": "", "error": "empty text"}
    result = await agent.agent_turn(text, source="chat", speak=speak)
    return result


@app.get("/api/chat/history")
async def chat_history() -> list[dict]:
    return await db.recent_chat(200)


@app.get("/api/tools")
async def api_tools() -> dict:
    return {"tools": tool_schemas()}


@app.post("/api/tools/{name}/call")
async def api_tool_call(name: str, req: Request) -> JSONResponse:
    args = await req.json()
    res = await call_tool(name, args if isinstance(args, dict) else {})
    return JSONResponse(res)


@app.get("/api/jobs")
async def api_jobs() -> dict:
    rows = await db.list_job_rows()
    out = []
    for r in rows:
        j = scheduler.get_job(r["job_id"])
        r["next_run_iso"] = j.next_run_time.isoformat() if j and j.next_run_time else None
        out.append(r)
    return {"jobs": out}


@app.post("/api/jobs")
async def api_jobs_create(req: Request) -> dict:
    b = await req.json()
    res = await call_tool("schedule_job", {
        "name": b.get("name", "Job"), "kind": b.get("kind", "interval"),
        "spec_json": json.dumps(b.get("spec", {})),
        "payload_json": json.dumps(b.get("payload", {}))})
    return res["data"] | {"created_by": "user"}


@app.post("/api/jobs/{job_id}/toggle")
async def api_jobs_toggle(job_id: str, req: Request) -> dict:
    b = await req.json()
    res = await call_tool("toggle_job", {"job_id": job_id, "enabled": bool(b.get("enabled"))})
    return res["data"]


@app.delete("/api/jobs/{job_id}")
async def api_jobs_delete(job_id: str) -> dict:
    res = await call_tool("delete_job", {"job_id": job_id})
    return res["data"]


@app.post("/api/jobs/reorder")
async def api_jobs_reorder(req: Request) -> dict:
    b = await req.json()
    ids = b.get("order", [])
    rows = {r["job_id"]: r for r in await db.list_job_rows()}
    for i, jid in enumerate(ids):        # persist drag order via next_run metadata trick:
        if jid in rows:                  # store order index in spec for UI stability
            r = rows[jid]
            r["spec"]["ui_order"] = i
            await db.upsert_job(jid, r["name"], r["kind"], r["spec"], r["payload"],
                                r["created_by"], r["enabled"])
    return {"ok": True}


@app.post("/api/routines/{name}/run")
async def api_routine_run(name: str) -> dict:
    from tools.routines import run_routine
    return await run_routine(name)


@app.get("/api/dashboard")
async def api_dashboard() -> dict:
    """One-shot aggregate for the glass tiles."""
    out: dict[str, Any] = {}
    for key, tool_name, args in [("hue", "hue_lights", {}),
                                 ("spotify", "spotify_status", {}),
                                 ("vacuum", "vacuum_status", {}),
                                 ("weather", "weather_forecast", {}),
                                 ("network", "network_devices", {}),
                                 ("system", "system_status", {}),
                                 ("notes", "note_list", {"limit": 8})]:
        try:
            r = await call_tool(tool_name, args)
            out[key] = r["data"]
        except Exception as e:  # noqa: BLE001
            out[key] = {"error": str(e)}
    try:
        hw = await db.health_history(30)
        out["withings"] = {"latest": hw[-1] if hw else None, "points": hw[-40:]}
    except Exception as e:  # noqa: BLE001
        out["withings"] = {"error": str(e)}
    return out


@app.get("/api/analytics")
async def api_analytics(days: int = 90) -> dict:
    health_rows = await db.health_history(days)
    net_rows = await db.network_history(240)
    energy = await call_tool("energy_estimate", {"hours": 24})
    return {"health": health_rows, "network": net_rows,
            "energy": energy["data"], "events": bus.history(100)}


@app.get("/api/settings")
async def api_settings() -> dict:
    from tools.tts import get_tts_language, _detect_engine
    return {"demo_mode": config.DEMO_MODE, "tts_language": get_tts_language(),
            "tts_engine": _detect_engine(), "model": config.OLLAMA_MODEL,
            "ollama_url": config.OLLAMA_URL, "max_iterations": config.MAX_TOOL_ITERATIONS,
            "workspace": str(config.WORKSPACE_ROOT)}


@app.post("/api/settings/tts")
async def api_tts(req: Request) -> dict:
    b = await req.json()
    res = await call_tool("tts_config", {"language": b.get("language", ""),
                                         "engine": b.get("engine", "")})
    return res["data"]


@app.post("/api/skills/install")
async def api_install_skill(req: Request) -> dict:
    b = await req.json()
    res = await call_tool("install_skill", {"source_url": b.get("url", "")})
    return res["data"]


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    """Real-time stream: events + live log lines + dashboard heartbeats."""
    await ws.accept()
    q = bus.subscribe()
    await ws.send_json({"type": "hello", "data": {
        "events": bus.history(60), "model": config.OLLAMA_MODEL,
        "demo": config.DEMO_MODE, "tools": len(REGISTRY)}})
    hb = asyncio.create_task(heartbeat(ws))
    try:
        while True:
            ev = await q.get()
            await ws.send_json(ev.to_dict())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        hb.cancel()
        bus.unsubscribe(q)


async def heartbeat(ws: WebSocket) -> None:
    while True:
        await asyncio.sleep(15)
        try:
            st = await call_tool("system_status", {})
            await ws.send_json({"type": "heartbeat", "data": st["data"]})
        except Exception:  # noqa: BLE001
            return


if __name__ == "__main__":
    print(f"🏠 Smart Home Agent Harness — http://{config.HOST}:{config.PORT} "
          f"(DEMO_MODE={config.DEMO_MODE}, model={config.OLLAMA_MODEL})")
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="info")
