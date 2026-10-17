"""WiFi & Network Presence Engine.

- Local subnet discovery: system ``arp``/``ip neighbor``/``arp-scan`` wrappers,
  optional scapy ping-sweep; maps MAC→IP with vendor lookup (OUI prefix table).
- Continuous presence monitor task with configurable Leave/Arrive dwell timers
  that emits device_arrived / device_left on the event bus (hooks into routines).
- Router integration stubs: UniFi API, OpenWrt ubus/luci, FRITZ!Box, hostapd
  → real-time RSSI, rx speed, band (2.4/5 GHz).
- Unknown-device security alerts over WebSocket/TTS.
- Gateway + external latency monitor persisted for UI sparklines.
"""
from __future__ import annotations

import asyncio
import ipaddress
import random
import re
import socket
import subprocess
import time
from typing import Any

import config
from eventbus import emit, EV_DEVICE_ARRIVED, EV_DEVICE_LEFT, EV_UNKNOWN_DEVICE, EV_NETWORK_HEALTH
from tools.registry import tool
from tools import sim
import db

# small static OUI vendor map (real deployments can load full IEEE dump)
_OUI = {
    "AA:BB:CC": "Test Vendor", "B8:27:EB": "Raspberry Pi Foundation",
    "EC:1A:59": "Philips Lighting", "64:16:66": "Shenzhen Roidmi",
    "F4:1B:67": "Google", "C8:CB:19": "Samsung CNK", "3C:07:54": "Apple",
    "DC:A6:32": "Espressif",
}

# live state used by dashboard tile
STATE: dict[str, Any] = {"devices": [], "tracked": {}, "gateway_ms": None,
                         "external_ms": None, "ssid": "", "last_scan_ts": 0}


def _vendor(mac: str) -> str:
    return _OUI.get(mac.upper()[:8], "Unknown")


# ---------------------------------------------------------------- discovery
@tool
async def network_scan(cidr: str = "") -> dict:
    """Discover devices on the LAN via ARP tables / ping sweep. Returns MAC, IP, vendor, name guesses."""
    net = cidr or config.SUBNET_CIDR or _guess_subnet()
    if sim.demo():
        devs = sim.world()["network"]["devices"]
        # occasionally a brand-new device appears to exercise the alert path
        if random.random() < 0.25 and not any(d["name"].startswith("UNKNOWN") for d in devs):
            devs.append({"ip": f"192.168.1.{random.randint(100, 200)}",
                         "mac": f"3C:07:54:{random.randint(16,255):02X}:{random.randint(16,255):02X}:FF",
                         "name": "UNKNOWN", "vendor": "Apple", "kind": "unknown",
                         "room": "", "online": True, "rssi": -77, "band_ghz": 5,
                         "rx_rate": 240, "first_seen": time.time()})
        STATE["devices"] = devs
        STATE["last_scan_ts"] = time.time()
        await _diff_devices(devs)
        return {"ok": True, "demo": True, "subnet": net, "count": len(devs),
                "devices": devs}
    devices = await _real_scan(net)
    STATE["devices"] = devices
    STATE["last_scan_ts"] = time.time()
    await _diff_devices(devs)
    return {"ok": True, "subnet": net, "count": len(devics := devices), "devices": devics}


def _guess_subnet() -> str:
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return ".".join(ip.split(".")[:3]) + ".0/24"
    except OSError:
        return "192.168.1.0/24"


async def _real_scan(cidr: str) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    devices: dict[str, dict] = {}

    def _collect():
        out: list[str] = []
        for cmd in (["arp", "-an"], ["ip", "neighbor", "show"]):
            try:
                out += subprocess.check_output(cmd, text=True, timeout=10).splitlines()
            except Exception:  # noqa: BLE001
                pass
        return out

    lines = await loop.run_in_executor(None, _collect)
    pat = re.compile(r"(\d+\.\d+\.\d+\.\d+)\s+.*?((?:[0-9a-f]{2}:){5}[0-9a-f]{2})", re.I)
    for l in lines:
        m = pat.search(l)
        if m and not m.group(1).startswith(("ff:", "0.0")):
            ip, mac = m.group(1), m.group(2).upper()
            devices[mac] = {"ip": ip, "mac": mac, "vendor": _vendor(mac),
                            "name": _guess_name(mac, ip), "online": True,
                            "rssi": None, "band_ghz": None, "kind": "lan"}
    # ping sweep fallback for hosts not yet in ARP table
    try:
        net = ipaddress.ip_network(cidr, strict=False)
        if net.num_addresses <= 300:
            async def _ping(host):
                p = await asyncio.create_subprocess_exec(
                    "ping", "-c", "1", "-W", "1", str(host),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                if await p.wait() == 0:
                    return str(host)
                return None
            hits = await asyncio.gather(*[_ping(h) for h in net.hosts()])
            for ip in [h for h in hits if h]:
                if not any(d["ip"] == ip for d in devices.values()):
                    devices[ip] = {"ip": ip, "mac": "?", "vendor": "?",
                                   "name": "", "online": True, "kind": "ping"}
    except ValueError:
        pass
    res = list(devices.values())
    for d in res:
        d.setdefault("first_seen", time.time())
    return res


_NAME_HINTS = {"raspberry": "Pi", "google": "Chromecast/Home", "apple": "iPhone/iPad",
               "samsung": "TV/Phone", "espressif": "ESP sensor"}


def _guess_name(mac: str, ip: str) -> str:
    v = _vendor(mac).lower()
    for k, label in _NAME_HINTS.items():
        if k in v:
            return label
    return ""


async def _diff_devices(devs: list[dict]) -> None:
    """Detect unknown MACs joining → security alert (WebSocket + TTS)."""
    known = sim.world()["network"]["known_macs"] if sim.demo() else _KNOWN_FILE
    if not sim.demo():
        known = set(_load_known())
    for d in devs:
        mac = d.get("mac", "")
        if not mac or mac == "?":
            continue
        is_new = mac not in known
        if is_new:
            known.add(mac) if sim.demo() else _save_known(known)
            emit(EV_UNKNOWN_DEVICE, source="network",
                 message=f"⚠️ new device joined: {d['ip']} [{mac}] vendor={d.get('vendor')}",
                 data={"ip": d["ip"], "mac": mac, "vendor": d.get("vendor")})
            try:
                from tools.tts import tts_speak
                await tts_speak.__wrapped__(
                    text=f"Uzmanību: tīklā parādījās jauna ierīce {d.get('vendor') or d['ip']}.")
            except Exception:  # noqa: BLE001
                pass


_KNOWN_FILE = config.DATA_DIR / "known_macs.txt"


def _load_known() -> list[str]:
    if _KNOWN_FILE.exists():
        return _KNOWN_FILE.read_text().split()
    return []


def _save_known(macs) -> None:
    _KNOWN_FILE.write_text("\n".join(sorted(macs)))


# ---------------------------------------------------------------- presence engine
def tracked_list() -> list[dict[str, Any]]:
    import json as _json
    if config.TRACKED_DEVICES_JSON:
        try:
            return _json.loads(config.TRACKED_DEVICES_JSON)
        except _json.JSONDecodeError:
            pass
    w = sim.world()["presence"] if sim.demo() else {}
    return [{"name": k, "mac": v["mac"]} for k, v in w.items()] or [
        {"name": "andris-phone", "mac": "AA:BB:CC:11:22:33"}]


async def presence_monitor_loop(stop: asyncio.Event) -> None:
    """Background task: ping/ARP probe tracked MACs, apply dwell timers, emit events."""
    dwell: dict[str, dict[str, float]] = {}   # name -> {seen_since, gone_since}
    home_state: dict[str, bool] = {}
    while not stop.is_set():
        try:
            await _presence_tick(dwell, home_state)
        except Exception as exc:  # noqa: BLE001
            print(f"[presence] tick error: {exc}")
        try:
            await asyncio.wait_for(stop.wait(), timeout=config.PRESENCE_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


async def _presence_tick(dwell: dict, home_state: dict) -> None:
    now = time.time()
    online_macs = await _probe_online()
    for dev in tracked_list():
        name, mac = dev["name"], dev.get("mac", "").upper()
        reachable = mac in online_macs or dev.get("ip", "") in online_macs
        st = dwell.setdefault(name, {"seen_since": 0.0, "gone_since": 0.0})
        if reachable:
            st["gone_since"] = 0.0
            st["seen_since"] = st["seen_since"] or now
            if not home_state.get(name) and now - st["seen_since"] >= config.ARRIVE_DWELL_S:
                home_state[name] = True
                await db.log_presence(mac, name, "home")
                emit(EV_DEVICE_ARRIVED, source="presence",
                     message=f"🏠 {name} arrived (dwell {config.ARRIVE_DWELL_S}s satisfied)",
                     data={"device": name, "mac": mac})
                await _maybe_all_present(home_state)
        else:
            st["seen_since"] = 0.0
            st["gone_since"] = st["gone_since"] or now
            if home_state.get(name) and now - st["gone_since"] >= config.LEAVE_DWELL_S:
                home_state[name] = False
                await db.log_presence(mac, name, "away")
                emit(EV_DEVICE_LEFT, source="presence",
                     message=f"👋 {name} left (unreachable {config.LEAVE_DWELL_S}s)",
                     data={"device": name, "mac": mac})
                await _maybe_all_gone(home_state)
        STATE["tracked"][name] = {"home": bool(home_state.get(name)), "mac": mac,
                                  "rssi": next((d.get("rssi") for d in STATE["devices"]
                                                if d.get("mac", "").upper() == mac), None)}


async def _probe_online() -> set[str]:
    if sim.demo():
        w = sim.world()
        # simulate: phones leave when world presence says away; ARP table mirrors it
        online = set()
        for name, p in w["presence"].items():
            if p["home"]:
                online.add(p["mac"])
        for d in w["network"]["devices"]:
            if d.get("online"):
                online.add(d["mac"])
        return online
    scan = await network_scan.__wrapped__()
    return {d.get("mac", "").upper() for d in scan.get("devices", []) if d.get("online")}


async def _maybe_all_gone(home_state: dict) -> None:
    """All tracked devices away → hook into Away Cleaning routine (cooldown guarded inside)."""
    if home_state and not any(home_state.values()):
        from tools.routines import run_routine
        await run_routine("away")


async def _maybe_all_present(home_state: dict) -> None:
    if any(home_state.values()):
        from tools.routines import run_routine
        await run_routine("welcome")


# ---------------------------------------------------------------- router APIs
@tool
async def router_clients(vendor: str = "") -> dict:
    """Pull live Wi-Fi clients (RSSI, rx speed, band) from UniFi/OpenWrt/FRITZ!Box/hostapd."""
    vendor = (vendor or config.ROUTER_API).lower()
    if sim.demo() or not vendor:
        devs = [d for d in (sim.world()["network"]["devices"] if sim.demo() else STATE["devices"])
                if d.get("rssi") is not None]
        return {"ok": True, "demo": sim.demo(), "source": vendor or "sim",
                "clients": [{"mac": d["mac"], "ip": d["ip"], "name": d["name"],
                             "rssi_dbm": d.get("rssi"), "band_ghz": d.get("band_ghz"),
                             "rx_rate_mbps": d.get("rx_rate")} for d in devs]}
    if vendor == "unifi":
        # UniFi Controller API requires self-signed cert handling + CSRF; keep minimal:
        d = await sim.api_post(f"{config.ROUTER_URL}/api/login",
                               json_body={"username": config.ROUTER_USER,
                                          "password": config.ROUTER_PASS})
        c = await sim.api_get(f"{config.ROUTER_URL}/api/s/default/stat/sta", headers=d.get("csrf", {}) and {"x-csrf-token": d["csrf"]} or {})
        return {"ok": True, "source": "unifi",
                "clients": [{"mac": x["mac"], "ip": x["ip"], "name": x.get("hostname"),
                             "rssi_dbm": x.get("rssi"), "band_ghz": x.get("band_ghz", 5) / 1000,
                             "rx_rate_mbps": x.get("rx_rate")} for x in c.get("data", [])]}
    if vendor == "openwrt":
        proc = await asyncio.create_subprocess_shell(
            "ubus call iwinfo info '{\"device\":\"wlan0\"}' ; "
            "wlanconfig wlan0 list sta 2>/dev/null || true",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return {"ok": True, "source": "openwrt", "raw": out.decode()[:2000]}
    if vendor == "fritz":
        login = await sim.api_get(f"{config.ROUTER_URL}/login.lua",
                                  params={"username": config.ROUTER_USER,
                                          "password": config.ROUTER_PASS})
        return {"ok": True, "source": "fritz", "note": "session established", "meta": login}
    if vendor == "hostapd":
        proc = await asyncio.create_subprocess_shell(
            "hostap_cli=$(ls /var/run/hostapd*.sock 2>/dev/null | head -1); "
            "[ -n \"$hostap_cli\" ] && echo 'STA' | socat - UNIX-CONNECT:$hostap_cli || true",
            stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        return {"ok": True, "source": "hostapd", "raw": out.decode()[:2000]}
    return {"ok": False, "error": f"unsupported router vendor '{vendor}'"}


# ---------------------------------------------------------------- health
@tool
async def network_health(samples: int = 1) -> dict:
    """Ping gateway and an external host to measure latency; stores history for sparklines."""
    gw_ms, ext_ms = None, None
    gw_ip = "192.168.1.1"
    if sim.demo():
        gw_ms = round(random.uniform(0.4, 3.5), 1)
        ext_ms = round(random.uniform(8, 45), 1)
        if random.random() < 0.08:
            ext_ms = round(random.uniform(120, 400), 1)   # occasional congestion spike
    else:
        import os
        gw_ip = _default_gateway() or gw_ip
        gw_ms = await _ping_ms(gw_ip)
        ext_ms = await _ping_ms("1.1.1.1")
    STATE["gateway_ms"], STATE["external_ms"] = gw_ms, ext_ms
    STATE["ssid"] = sim.world()["network"]["ssid"] if sim.demo() else _local_ssid()
    await db.save_network_snapshot(gw_ms, ext_ms, len(STATE["devices"]),
                                   {"ssid": STATE["ssid"]})
    hist = await db.network_history(120)
    emit(EV_NETWORK_HEALTH, source="network",
         message=f"latency gw={gw_ms}ms ext={ext_ms}ms devices={len(STATE['devices'])}",
         data={"gateway_ms": gw_ms, "external_ms": ext_ms})
    return {"ok": True, "gateway_ms": gw_ms, "external_ms": ext_ms,
            "devices_online": len(STATE["devices"]), "ssid": STATE["ssid"],
            "history": hist[-samples * 60:]}


async def _ping_ms(host: str) -> float | None:
    proc = await asyncio.create_subprocess_exec(
        "ping", "-c", "1", "-W", "2", host, stdout=asyncio.subprocess.PIPE)
    out, _ = await proc.communicate()
    m = re.search(r"time=(\d+(?:\.\d+)?)", out.decode(errors="replace"))
    return float(m.group(1)) if m else None


def _default_gateway() -> str | None:
    try:
        out = subprocess.check_output(["ip", "route"], text=True)
        m = re.search(r"default via (\d+\.\d+\.\d+\.\d+)", out)
        return m.group(1) if m else None
    except Exception:  # noqa: BLE001
        return None


def _local_ssid() -> str:
    try:
        out = subprocess.check_output(["iwgetid", "-r"], text=True).strip()
        return out
    except Exception:  # noqa: BLE001
        return ""


@tool
async def network_block_device(mac: str, reason: str = "unknown device") -> dict:
    """Flag/block a suspicious device on the network (router-side ACL where supported; logged always)."""
    await db.remember("security", f"blocked {mac}: {reason}", key=f"block:{mac}")
    blocked = config.DATA_DIR / "blocked_macs.txt"
    blocked.write_text(blocked.read_text() + f"\n{mac}" if blocked.exists() else mac)
    emit(EV_UNKNOWN_DEVICE, source="network",
         message=f"🚫 flagged/blocked {mac} ({reason})", data={"mac": mac, "action": "block"})
    return {"ok": True, "mac": mac, "note": "added to blocklist; apply via router ACL or iptables"}


@tool
async def network_devices() -> dict:
    """Return last scan snapshot: connected devices with aliases, rooms, RSSI, band."""
    return {"ok": True, "devices": STATE["devices"], "tracked": STATE["tracked"],
             "last_scan": STATE["last_scan_ts"], "ssid": STATE["ssid"]}
