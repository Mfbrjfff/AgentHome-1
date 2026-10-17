"""Text-to-Speech: local piper/espeak-ng with browser-side speechSynthesis fallback.

NO speech-to-text anywhere in the stack — input is text-only by design.
``speak()`` emits a TTS event on the bus; the dashboard listens and can speak
via window.speechSynthesis when the Pi has no audio hardware (or as primary).
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
from typing import Any

import config
from eventbus import emit, EV_TTS
from tools.registry import tool
from tools import sim

_current_lang = {"lang": config.TTS_VOICE}


def set_tts_language(lang: str) -> None:
    _current_lang["lang"] = lang or config.TTS_VOICE


def get_tts_language() -> str:
    return _current_lang["lang"]


def _detect_engine() -> str:
    if config.TTS_ENGINE != "auto":
        return config.TTS_ENGINE
    if shutil.which("piper"):
        return "piper"
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        return "espeak"
    return "browser"


@tool
async def tts_speak(text: str, language: str = "") -> dict:
    """Speak text aloud. Uses local piper/espeak-ng when available, otherwise streams to the browser UI which speaks via speechSynthesis.
    :param text: the sentence(s) to voice
    :param language: locale like lv-LV / en-US; empty uses configured default
    """
    lang = language or get_tts_language()
    engine = _detect_engine()
    result: dict[str, Any] = {"ok": True, "engine": engine, "language": lang,
                              "chars": len(text)}
    if sim.demo() or engine == "browser":
        # Browser will vocalise it; we still announce on the bus for the UI.
        emit(EV_TTS, source="tts", message=f'🔊 [{lang}] "{text[:120]}"',
             data={"text": text, "lang": lang, "engine": "browser"})
        result["mode"] = "browser-speechSynthesis"
        return result
    try:
        if engine == "piper":
            proc = await asyncio.create_subprocess_exec(
                "piper", "--model", f"{config.DATA_DIR}/voices/{lang.split('-')[0]}_medium.onnx",
                "--output-raw", stdout=asyncio.subprocess.PIPE, stdin=asyncio.subprocess.PIPE)
            raw, _ = await proc.communicate(text.encode())
            play = await asyncio.create_subprocess_exec(
                "aplay", "-r", "22050", "-f", "S16_mono", stdin=asyncio.subprocess.PIPE)
            await play.communicate(raw)
        else:  # espeak-ng
            proc = await asyncio.create_subprocess_exec(
                "espeak-ng", "-v", lang.replace("-", "_"), "-s", "160", text,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            await proc.wait()
        emit(EV_TTS, source="tts", message=f'🔊 [{lang}] "{text[:120]}"',
             data={"text": text, "lang": lang, "engine": engine})
        result["mode"] = f"local-{engine}"
    except Exception as exc:  # noqa: BLE001 fall back gracefully to browser
        emit(EV_TTS, source="tts", message=f'🔊 [{lang}] "{text[:120]}" (fallback browser)',
             data={"text": text, "lang": lang, "engine": "browser"})
        result.update({"ok": True, "mode": "browser-fallback", "error": str(exc)})
    return result


@tool
async def tts_config(language: str = "", engine: str = "") -> dict:
    """Configure TTS language/engine at runtime (e.g. language='lv-LV', engine='espeak')."""
    if language:
        set_tts_language(language)
    if engine in ("auto", "piper", "espeak", "browser"):
        config.TTS_ENGINE = engine
    return {"ok": True, "language": get_tts_language(), "engine": _detect_engine()}
