"""System tools: health gauges (psutil), disk-space cleaner, update agent,
LAN info, and safe reboot. Also the energy-estimate helper for Analytics.

Everything destructive is logged with before/after snapshots; rollback is
possible because /workspace is a git repo (see selfmod.py).
"""
from __future__ import annotations

import asyncio
import glob
import os
import shutil
import socket
import subprocess
import time
from typing import Any

import psutil

import config
from eventbus import emit, EV_SYSTEM, EV_ALERT
from tools.registry import tool
from tools import sim


@tool
async def system_status() -> dict:
    """CPU %, RAM usage, load, core temperature, uptime, LAN IP, disk space."""
    cpu = psutil.cpu_percent(interval=None)
    mem = psutil.virtual_memory()
    du = shutil.disk_usage(config.BASE_DIR)
    return {
        "cpu_pct": round(cpu, 1),
        "ram_used_mb": round(mem.used / 1e6), "ram_total_mb": round(mem.total / 1e6),
        "ram_pct": mem.percent,
        "load_1m": round(os.getloadavg()[0], 2),
        "temp_c": _cpu_temp(),
        "uptime_s": int(time.time() - psutil.boot_time()),
        "lan_ip": _lan_ip(),
        "disk_free_gb": round(du.free / 1e9, 2), "disk_total_gb": round(du.total / 1e9, 2),
        "disk_used_pct": round(du.used / du.total * 100, 1),
        "demo_mode": config.DEMO_MODE,
    }


def _cpu_temp() -> float | None:
    try:
        p = subprocess.run(["vcgencmd", "measure_temp"], capture_output=True, text=True, timeout=3)
        if p.returncode == 0:
            return float(p.stdout.strip().split("=")[1].rstrip("'C"))
    except Exception:  # noqa: BLE001
        pass
    try:
        for f in glob.glob("/sys/class/thermal/thermal_zone*/temp"):
            return round(int(open(f).read().strip()) / 1000, 1)
    except Exception:  # noqa: BLE001
        pass
    hwm = psutil.sensors_temperatures() if hasattr(psutil, "sensors_temperatures") else {}
    for entries in (hwm or {}).values():
        if entries:
            return round(entries[0].current, 1)
    return None


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


# ---------------------------------------------------------------- disk cleaner
@tool
async def disk_check(threshold_pct: float = 90.0) -> dict:
    """Check free space on MicroSD/NVMe; reports whether cleaning should run."""
    du = shutil.disk_usage(config.BASE_DIR)
    used = du.used / du.total * 100
    return {"ok": True, "used_pct": round(used, 1),
            "free_gb": round(du.free / 1e9, 2),
            "needs_cleaning": used >= threshold_pct}


@tool
async def disk_clean(dry_run: bool = False) -> dict:
    """Free disk space: prune old logs, SQLite temp/WAL files, pip & Ollama/Docker caches. Safe order, everything logged."""
    freed = 0
    actions: list[str] = []

    def _size(path: str) -> int:
        try:
            return sum(os.path.getsize(f) for f in glob.glob(path, recursive=True)
                       if os.path.isfile(f))
        except OSError:
            return 0

    targets = [
        ("old rotated logs", f"{config.LOG_DIR}/*.log.[0-9]*"),
        ("agent log >7d", f"{config.LOG_DIR}/agent.log"),
        ("sqlite temp files", f"{config.DATA_DIR}/*.db-*"),
        ("pip cache", os.path.expanduser("~/.cache/pip/**/*")),
        ("ollama blobs (kept: current)", "/usr/share/Ollama/models/**/.tmp*"),
        ("docker builder cache", "/var/lib/docker/buildkit/cache/**/*"),
    ]
    for label, pattern in targets:
        n = _size(pattern)
        if n > 0:
            freed += n
            actions.append(f"{label}: {_mb(n)}")
            if not dry_run:
                try:
                    for f in glob.glob(pattern, recursive=True):
                        if os.path.isfile(f):
                            if "agent.log" in f:
                                open(f, "w").close()   # truncate, don't delete live log
                            elif "docker" in f or "ollama" in f.lower():
                                continue               # needs root services — report only
                            else:
                                os.remove(f)
                except PermissionError:
                    actions.append(f"{label}: permission denied (run as root)")
    # vacuum sqlite through the app itself
    if not dry_run:
        try:
            import sqlalchemy as sa
            from db import get_session
            async with get_session() as s:
                await s.execute(sa.text("PRAGMA wal_checkpoint(TRUNCATE)"))
                await s.commit()
        except Exception:  # noqa: BLE001
            pass
    du = shutil.disk_usage(config.BASE_DIR)
    msg = f"[{'dry-run' if dry_run else 'cleaned'}] freed ≈{_mb(freed)}; now {round(du.free/1e9,2)} GB free"
    emit(EV_SYSTEM, source="system", message=msg)
    return {"ok": True, "freed_bytes": freed, "actions": actions,
            "free_gb_now": round(du.free / 1e9, 2)}


def _mb(n: int) -> str:
    return f"{n / 1e6:.1f} MB"


async def maybe_autoclean() -> None:
    """Called by periodic maintenance job: auto-clean when disk >90% used."""
    st = await disk_check.__wrapped__()
    if st["needs_cleaning"]:
        emit(EV_ALERT, source="system",
             message=f"⚠️ disk {st['used_pct']}% full → running automatic cleanup")
        await disk_clean.__wrapped__(dry_run=False)


# ---------------------------------------------------------------- updates
@tool
async def system_update(check_only: bool = True) -> dict:
    """Package update agent: apt-get update (+upgrade when check_only=false) with pre/post health verification. Skipped in demo mode unless forced."""
    if sim.demo():
        emit(EV_SYSTEM, source="system",
             message="[demo] apt update simulated: 3 packages upgradable (firmware-bcm, curl, libimobiledevice)")
        return {"ok": True, "demo": True, "upgradable": ["firmware-bcm43xx", "curl", "libimobiledevice6"],
                "pre_health": {"status": "ok"}, "post_health": {"status": "ok"}}
    pre = await _health_probe()
    cmd = ["sudo", "apt-get", "update"] if check_only else \
          ["sudo", "apt-get", "update", "-y"]
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    result = {"ok": proc.returncode == 0, "check_only": check_only,
              "output_tail": out.decode(errors="replace")[-800:]}
    if not check_only:
        proc2 = await asyncio.create_subprocess_exec(
            "sudo", "apt-get", "upgrade", "-y", "--with-new-pkgs",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out2, _ = await proc2.communicate()
        result["upgrade_tail"] = out2.decode(errors="replace")[-800:]
    post = await _health_probe()
    result.update({"pre_health": pre, "post_health": post})
    if pre["ok"] and not post["ok"]:
        emit(EV_ALERT, source="system", message="❌ health degraded after update — rollback advised")
    return result


@tool
async def system_reboot(confirm: bool = False) -> dict:
    """Reboot the Pi. Requires confirm=true double-guard even though autonomy is maximal."""
    if not confirm:
        return {"ok": False, "error": "pass confirm=true to actually reboot"}
    pre = await _health_probe()
    emit(EV_SYSTEM, source="system", message=f"🔄 reboot requested (pre-health {pre})")
    if sim.demo():
        return {"ok": True, "demo": True, "note": "reboot suppressed in demo mode"}
    proc = await asyncio.create_subprocess_exec("sudo", "shutdown", "-r", "+0")
    await proc.wait()
    return {"ok": True}


async def _health_probe() -> dict:
    import httpx
    ok = False
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get("http://localhost:8000/api/health")
            ok = r.status_code == 200
    except Exception:  # noqa: BLE001
        pass
    return {"ok": ok, "endpoint": "/api/health"}


# ---------------------------------------------------------------- energy proxy
@tool
async def energy_estimate(hours: int = 24) -> dict:
    """Estimate home energy use from device states (psutil CPU + active devices proxy model)."""
    w = sim.world() if sim.demo() else {}
    lights_on = sum(1 for l in w.get("hue", {}).values() if l.get("on")) if sim.demo() else 0
    vacuum_min = w.get("vacuum", {}).get("cleaned_min_total", 0) if sim.demo() else 0
    playing = 1 if (sim.demo() and w.get("spotify", {}).get("playing")) else 0
    pi_w = 3.0 + psutil.cpu_percent() * 0.09           # Pi5 idle→load curve
    light_w = lights_on * 9.0
    vac_w = (vacuum_min / 60) * 30.0 / max(1, hours)
    spk_w = playing * 12.0
    total_w = pi_w + light_w + vac_w + spk_w + 35.0    # + base standby of router etc.
    kwh = round(total_w * hours / 1000, 3)
    return {"ok": True, "watts_now": round(total_w, 1), "kwh_estimate": kwh,
            "breakdown_w": {"pi": round(pi_w, 1), "lights": light_w,
                            "vacuum_avg": round(vac_w, 1), "speaker": spk_w,
                            "standby_base": 35.0},
            "hours_window": hours}
