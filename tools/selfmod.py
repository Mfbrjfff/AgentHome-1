"""MAXIMUM AUTONOMY: self-modification tools.

read_file / write_file / patch_file / list_dir / run_shell / restart_server /
edit_own_prompt — all operate under WORKSPACE_ROOT with path escapes blocked,
every action streamed to the live log, destructive writes snapshot the old
content into data/snapshots/ (plus git commit in main repo) for rollback.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import subprocess
import time
from pathlib import Path

import config
from eventbus import emit, EV_SYSTEM, EV_ALERT
from tools.registry import tool

ROOT = Path(config.WORKSPACE_ROOT).resolve()


def _safe(rel: str) -> Path:
    p = (ROOT / rel).resolve() if not os.path.isabs(rel) else Path(rel).resolve()
    if not str(p).startswith(str(ROOT)):
        raise PermissionError(f"path escape blocked: {rel} outside {ROOT}")
    return p


def _snapshot(path: Path) -> str | None:
    if not path.exists():
        return None
    snapdir = config.DATA_DIR / "snapshots"
    snapdir.mkdir(parents=True, exist_ok=True)
    dest = snapdir / f"{path.name}.{int(time.time())}.bak"
    dest.write_bytes(path.read_bytes())
    return str(dest)


@tool
async def list_dir(path: str = ".") -> dict:
    """List files/dirs under the workspace."""
    p = _safe(path)
    if not p.is_dir():
        return {"ok": False, "error": "not a directory"}
    entries = []
    for e in sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name)):
        if e.name == ".git":
            continue
        entries.append({"name": e.name, "dir": e.is_dir(),
                        "size": e.stat().st_size if e.is_file() else None})
    return {"ok": True, "path": str(p.relative_to(ROOT)), "entries": entries[:400]}


@tool
async def read_file(path: str, max_lines: int = 400) -> dict:
    """Read a text file from the workspace (agent can inspect its own source)."""
    p = _safe(path)
    if not p.is_file():
        return {"ok": False, "error": "file not found"}
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"ok": True, "path": str(p), "total_lines": len(lines),
                "content": "\n".join(lines[:max_lines])}
    except OSError as e:
        return {"ok": False, "error": str(e)}


@tool
async def write_file(path: str, content: str) -> dict:
    """Create or overwrite a file inside the workspace. Old version is snapshotted; change logged with diff summary. Use for new tools/skills and self-rewrites."""
    p = _safe(path)
    before = _snapshot(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    old = p.read_text(errors="replace") if p.exists() else ""
    p.write_text(content, encoding="utf-8")
    diff = list(difflib.unified_diff(old.splitlines(), content.splitlines(), lineterm=""))[:60]
    emit(EV_SYSTEM, source="selfmod",
         message=f"✍️ wrote {p.relative_to(ROOT)} ({len(content)} chars)"
                 + (f", snapshot {Path(before).name}" if before else " (new file)"),
         data={"path": str(p), "snapshot": before, "diff_head": diff[:20]})
    _git_commit_autosave(p)
    # hot-reload if it's a tool module we already imported
    _maybe_hot_reload(p)
    return {"ok": True, "path": str(p), "bytes": len(content), "snapshot": before}


@tool
async def patch_file(path: str, find: str, replace: str) -> dict:
    """Surgical edit: replace an exact string in a workspace file (fails loudly if not unique/found)."""
    p = _safe(path)
    if not p.is_file():
        return {"ok": False, "error": "file not found"}
    text = p.read_text(encoding="utf-8", errors="replace")
    n = text.count(find)
    if n == 0:
        return {"ok": False, "error": "find-string not present"}
    if n > 1:
        return {"ok": False, "error": f"find-string appears {n}× — make it unique"}
    before = _snapshot(p)
    p.write_text(text.replace(find, replace, 1), encoding="utf-8")
    emit(EV_SYSTEM, source="selfmod", message=f"🩹 patched {p.relative_to(ROOT)}",
         data={"path": str(p), "snapshot": before})
    _git_commit_autosave(p)
    _maybe_hot_reload(p)
    return {"ok": True, "path": str(p), "snapshot": before}


@tool
async def run_shell(command: str, timeout_seconds: int = 60) -> dict:
    """Run any shell command on the Pi (whitelist-free, fully logged). Long output truncated."""
    emit(EV_SYSTEM, source="shell", message=f"$ {command}", data={"cmd": command})
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(ROOT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
        text = out.decode(errors="replace")
        emit(EV_SYSTEM, source="shell",
             message=f"exit {proc.returncode}: {text.strip()[:180] or '(no output)'}")
        return {"ok": proc.returncode == 0, "exit": proc.returncode,
                "output": text[-6000:]}
    except asyncio.TimeoutError:
        proc.kill()
        return {"ok": False, "error": f"timeout after {timeout_seconds}s"}


@tool
async def restart_server() -> dict:
    """Restart the agent harness process (systemd respawns it; demo-safe soft exit)."""
    emit(EV_SYSTEM, source="selfmod", message="♻️ restart requested by agent")
    if sim_reboot_guard():
        return {"ok": True, "demo": True, "note": "restart suppressed in DEMO_MODE"}
    async def _later():
        await asyncio.sleep(0.6)
        os._exit(3)     # systemd Restart=always brings us back
    asyncio.get_running_loop().create_task(_later())
    return {"ok": True, "note": "exiting shortly; systemd will respawn"}


def sim_reboot_guard() -> bool:
    from tools import sim
    return sim.demo()


@tool
async def edit_own_prompt(new_prompt: str = "", append: str = "") -> dict:
    """Rewrite or extend the agent's own system prompt/instructions (persisted in data/system_prompt.txt, effective next turn)."""
    cur = config.load_system_prompt()
    if new_prompt.strip():
        cfg_set = new_prompt
    elif append.strip():
        cfg_set = cur + "\n\n" + append
    else:
        return {"ok": True, "current_prompt": cur}
    config.set_system_prompt(cfg_set)
    emit(EV_SYSTEM, source="selfmod", message="🧠 system prompt updated",
         data={"chars": len(cfg_set)})
    return {"ok": True, "chars": len(cfg_set)}


@tool
async def get_own_prompt() -> dict:
    """Read the current system prompt the agent operates under."""
    return {"ok": True, "prompt": config.load_system_prompt()}


def _git_commit_autosave(path: Path) -> None:
    try:
        subprocess.run(["git", "-C", str(ROOT), "add", "-A"], check=False,
                       capture_output=True, timeout=10)
        subprocess.run(["git", "-C", str(ROOT), "commit", "-q", "-m",
                        f"agent autosave: {path.name} @ {time.strftime('%F %T')}"],
                       check=False, capture_output=True, timeout=10)
    except Exception:  # noqa: BLE001
        pass


def _maybe_hot_reload(path: Path) -> None:
    """If the agent rewrote a loaded tool/skill module, re-import it immediately."""
    import sys, importlib
    try:
        rel = path.resolve().relative_to(ROOT)
    except ValueError:
        return
    parts = rel.parts
    if len(parts) == 2 and parts[0] == "tools" and parts[1].endswith(".py"):
        mod = f"tools.{parts[1][:-3]}"
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])
            emit(EV_SYSTEM, source="selfmod", message=f"⚡ hot-reloaded {mod}")
    elif len(parts) >= 2 and parts[0] == "skills":
        from tools.registry import load_skills
        load_skills()
        emit(EV_SYSTEM, source="selfmod", message="⚡ skills registry refreshed")
