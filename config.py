"""Central configuration for the Smart Home Autonomous AI Agent Harness.

All runtime knobs live here and can be overridden with environment variables.
``DEMO_MODE=true`` makes every integration fall back to a realistic simulator
so the whole system runs with zero physical hardware attached.
"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("SH_DATA_DIR", BASE_DIR / "data"))
LOG_DIR = Path(os.getenv("SH_LOG_DIR", BASE_DIR / "logs"))
SKILLS_DIR = Path(os.getenv("SH_SKILLS_DIR", BASE_DIR / "skills"))
TOOLS_DIR = Path(os.getenv("SH_TOOLS_DIR", BASE_DIR / "tools"))
STATIC_DIR = BASE_DIR / "static"

for _d in (DATA_DIR, LOG_DIR, SKILLS_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def _env_bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------- core model
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "bonsai-2:4b")
LLM_TIMEOUT_S = float(os.getenv("LLM_TIMEOUT_S", "120"))
MAX_TOOL_ITERATIONS = int(os.getenv("MAX_TOOL_ITERATIONS", "6"))

# ---------------------------------------------------------------- behaviour
DEMO_MODE = _env_bool("DEMO_MODE", True)          # out-of-the-box friendly
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{DATA_DIR / 'agent.db'}")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

# ---------------------------------------------------------------- integrations
HASS_URL = os.getenv("HASS_URL", "")              # e.g. http://homeassistant.local:8123
HASS_TOKEN = os.getenv("HASS_TOKEN", "")
GOOGLE_HOME_PROJECT_ID = os.getenv("GOOGLE_HOME_PROJECT_ID", "")

HUE_BRIDGE_IP = os.getenv("HUE_BRIDGE_IP", "")    # e.g. 192.168.1.50
HUE_USERNAME = os.getenv("HUE_USERNAME", "")      # CLIP v2 user token

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_DEVICE_ID = os.getenv("SPOTIFY_DEVICE_ID", "")

WITHINGS_CLIENT_ID = os.getenv("WITHINGS_CLIENT_ID", "")
WITHINGS_CLIENT_SECRET = os.getenv("WITHINGS_CLIENT_SECRET", "")
WITHINGS_USER_ID = os.getenv("WITHINGS_USER_ID", "")
WITHINGS_TOKEN = os.getenv("WITHINGS_TOKEN", "")  # demo/simple bearer token

ROIDMI_IP = os.getenv("ROIDMI_IP", "")
ROIDMI_TOKEN = os.getenv("ROIDMI_TOKEN", "")
ROIDMI_KIND = os.getenv("ROIDMI_KIND", "miio")    # "miio" | "tuya"

OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
WEATHER_LAT = os.getenv("WEATHER_LAT", "56.9496")   # default: Riga, Latvia
WEATHER_LON = os.getenv("WEATHER_LON", "24.1052")
SEARXNG_URL = os.getenv("SEARXNG_URL", "")          # optional self-hosted search
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY", "")

# ---------------------------------------------------------------- presence / network
SUBNET_CIDR = os.getenv("SUBNET_CIDR", "")          # e.g. 192.168.1.0/24 (empty => autodetect)
PRESENCE_INTERVAL_S = int(os.getenv("PRESENCE_INTERVAL_S", "60"))
LEAVE_DWELL_S = int(os.getenv("LEAVE_DWELL_S", "180"))    # unreachable N sec before "Away"
ARRIVE_DWELL_S = int(os.getenv("ARRIVE_DWELL_S", "30"))   # reachable   N sec before "Home"
TRACKED_DEVICES_JSON = os.getenv("TRACKED_DEVICES_JSON", "")  # [{"mac":..,"name":..,"room":..}]
ROUTER_API = os.getenv("ROUTER_API", "")            # unifi | openwrt | fritz | hostapd | ""
ROUTER_URL = os.getenv("ROUTER_URL", "")
ROUTER_USER = os.getenv("ROUTER_USER", "")
ROUTER_PASS = os.getenv("ROUTER_PASS", "")

# ---------------------------------------------------------------- tts / locale
TTS_ENGINE = os.getenv("TTS_ENGINE", "auto")        # auto | piper | espeak | browser
TTS_VOICE = os.getenv("TTS_VOICE", "lv-LV")
AGENT_NAME = os.getenv("AGENT_NAME", "Atlas")

# ---------------------------------------------------------------- autonomy
WORKSPACE_ROOT = BASE_DIR                           # agent file tools operate here
SELF_MODIFICATION_ENABLED = _env_bool("SELF_MODIFICATION", True)

SYSTEM_PROMPT_PATH = DATA_DIR / "system_prompt.txt"

DEFAULT_SYSTEM_PROMPT = f"""You are {AGENT_NAME}, an autonomous AI agent that runs a smart home on a \
Raspberry Pi 5. You have FULL autonomy: you may call any registered tool, create new tools, install \
skills, edit your own files and schedule your own recurring jobs without asking for approval. \
Everything you do is streamed to a live operator log, so transparency replaces confirmation gates.

Operating rules:
- Prefer native function calls over prose when acting on the home.
- Chain tools to fulfil compound requests (e.g. lights AND weather in one turn).
- Keep final spoken replies short, warm and human; they will be read aloud via TTS.
- When health, cleaning or presence data changes, proactively suggest or schedule routines.
- Never invent device state: query it with a tool first.
Reply in the language the user writes in (Latvian is common: voice {TTS_VOICE})."""

if not SYSTEM_PROMPT_PATH.exists():
    SYSTEM_PROMPT_PATH.write_text(DEFAULT_SYSTEM_PROMPT, encoding="utf-8")


def load_system_prompt() -> str:
    """The agent can rewrite this file at runtime (edit_own_prompt tool)."""
    try:
        return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8").strip() or DEFAULT_SYSTEM_PROMPT
    except OSError:
        return DEFAULT_SYSTEM_PROMPT


def set_system_prompt(text: str) -> None:
    SYSTEM_PROMPT_PATH.write_text(text, encoding="utf-8")
