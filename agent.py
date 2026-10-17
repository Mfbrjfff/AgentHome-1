"""The autonomous agent core: native Ollama function-calling loop.

- single ``asyncio.Lock`` serialises LLM turns (memory-conscious on 4GB Pi)
- max :data:`config.MAX_TOOL_ITERATIONS` (6) tool iterations per turn
- streams reasoning/tool events to the event bus → Agent Thought Terminal
- persists chat history + long-term memory in SQLite
- optional offline fallback when Ollama is unreachable so the harness stays alive
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx

import config
from eventbus import emit, EV_AGENT_THOUGHT, EV_TOOL_CALL, EV_TTS
from tools.registry import call_tool, tool_schemas, REGISTRY
import db

LLM_LOCK = asyncio.Lock()          # ← one model turn at a time (bonsai-2:4b)
_history_cache: list[dict[str, str]] = []


# ---------------------------------------------------------------- ollama
async def _ollama_chat(messages: list[dict], tools: list[dict]) -> dict:
    payload = {"model": config.OLLAMA_MODEL, "messages": messages,
               "tools": tools, "stream": False,
               "options": {"temperature": 0.3, "num_ctx": 4096}}
    async with httpx.AsyncClient(timeout=config.LLM_TIMEOUT_S) as c:
        r = await c.post(f"{config.OLLAMA_URL}/api/chat", json=payload)
        r.raise_for_status()
        return r.json()


def _fallback_reply(user_text: str, tool_results: list[dict]) -> str:
    """Tiny deterministic planner used ONLY when Ollama is unreachable."""
    lines = ["(offline mode — Ollama nav pieejams, izpildīju tiešos rīkus)"]
    for tr in tool_results:
        d = tr.get("data", {})
        if tr["tool"] == "weather_forecast":
            cur = d.get("current", {})
            lines.append(f"Laiks: {cur.get('temp_c')}°C, {cur.get('condition_lv')}.")
        elif tr["tool"] == "hue_set":
            lines.append(f"Gaisma {d.get('rooms')}: {'ieslēgta' if d.get('on') else 'izslēgta'}.")
        elif tr["ok"]:
            lines.append(f"{tr['tool']} ok.")
    if not tool_results:
        lines.append(f"Saprotu: “{user_text[:80]}”. Modelis nav pieejams; palaid "
                     f"`ollama serve && ollama pull {config.OLLAMA_MODEL}` pilnīgai sarunai.")
    return " ".join(lines)


def _fallback_tool_calls(text: str) -> list[dict]:
    t = text.lower()
    calls = []
    if any(k in t for k in ("light", "gaisma", "gaismas", "lamp")):
        want_on = not any(k in t for k in ("off", "izslēdz", "nodzēst"))
        calls.append({"name": "hue_set", "arguments": {"room": "all", "on": want_on}})
    if any(k in t for k in ("weather", "laika", "lietus", "forecast")):
        calls.append({"name": "weather_forecast", "arguments": {}})
    if any(k in t for k in ("vacuum", "sūcējs", "roidmi", "clean")):
        calls.append({"name": "vacuum_start", "arguments": {"room": "all"}})
    if any(k in t for k in ("music", "spotify", "mūzika")):
        calls.append({"name": "spotify_play", "arguments": {}})
    return calls


# ---------------------------------------------------------------- main loop
async def agent_turn(user_text: str, *, source: str = "chat",
                     speak: bool = True) -> dict[str, Any]:
    """Run one full agent turn (serialised through LLM_LOCK)."""
    async with LLM_LOCK:
        return await _turn_locked(user_text, source, speak)


async def _turn_locked(user_text: str, source: str, speak: bool) -> dict[str, Any]:
    t0 = time.time()
    await db.log_chat("user", user_text)
    emit(EV_AGENT_THOUGHT, source="agent", message=f"🧠 turn start ({source}): {user_text[:120]}")

    memories = await db.recall(limit=8)
    system = config.load_system_prompt()
    if memories:
        system += "\n\nLong-term memory:\n" + "\n".join(
            f"- [{m['kind']}] {m['content']}" for m in memories)
    recent = await db.recent_chat(16)
    messages = [{"role": "system", "content": system}]
    messages += [{"role": m["role"], "content": m["content"]}
                 for m in recent if m["role"] in ("user", "assistant")]
    messages.append({"role": "user", "content": user_text})

    tool_trace: list[dict] = []
    final_text = ""
    llm_available = True

    for iteration in range(config.MAX_TOOL_ITERATIONS):
        try:
            resp = await _ollama_chat(messages, tool_schemas())
        except Exception as exc:  # noqa: BLE001 - Ollama down → fallback path
            llm_available = False
            emit(EV_AGENT_THOUGHT, source="agent",
                 message=f"⚠️ Ollama nepieejams ({exc.__class__.__name__}) — fallback režīms")
            auto_calls = _fallback_tool_calls(user_text) if iteration == 0 else []
            for c in auto_calls:
                res = await call_tool(c["name"], c["arguments"])
                tool_trace.append(res)
            final_text = _fallback_reply(user_text, tool_trace)
            break

        msg = resp.get("message", {})
        thinking = msg.get("thinking") or ""
        calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if thinking:
            emit(EV_AGENT_THOUGHT, source="agent", message=f"💭 {thinking[:300]}")

        if not calls:
            final_text = content or "(tukša atbilde)"
            break

        # append assistant tool-call message verbatim (Ollama multi-turn format)
        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for c in calls:
            fn = c.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args or "{}")
                except json.JSONDecodeError:
                    args = {}
            emit(EV_TOOL_CALL, source="agent",
                 message=f"iter {iteration + 1}: {name}({json.dumps(args, default=str)[:150]})")
            result = await call_tool(name, args)
            tool_trace.append(result)
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(result["data"], default=str)[:4000]})
    else:
        final_text = final_text or "Sasniedzu instrumentu iterāciju limitu (6); tekošie rezultāti augstāk."

    await db.log_chat("assistant", final_text)
    ms = int((time.time() - t0) * 1000)
    emit(EV_AGENT_THOUGHT, source="agent",
         message=f"✅ turn done in {ms}ms · {len(tool_trace)} tool call(s)"
                 f"{'' if llm_available else ' · FALLBACK'}")

    if speak and final_text:
        from tools.tts import tts_speak, get_tts_language
        await tts_speak.__wrapped__(text=_plain(final_text), language=get_tts_language())

    return {"reply": final_text, "tools": tool_trace, "ms": ms,
            "llm_available": llm_available}


def _plain(text: str) -> str:
    """Strip markdown/emoji noise for TTS."""
    import re
    text = re.sub(r"[*_`#]", "", text)
    return text.replace("\n", " ").strip()


async def warm_memory_seed() -> None:
    """Seed a couple of demo facts so memory recall is visibly working."""
    existing = await db.recall(limit=1)
    if not existing and config.DEMO_MODE:
        await db.remember("preference", "Lietotājs vēlas latviskus atbildes un lv-LV balsi")
        await db.remember("fact", "Garāžas vārtu kods saglabāts piezīmēs (sk. notes)")
