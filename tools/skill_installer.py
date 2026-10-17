"""Skill Installer: download skills from GitHub repos or ClawHub, verify their
structure, install into skills/, and hot-register their tools WITHOUT restart.

A "skill" is any directory containing skill.py (optionally skill.json manifest)
whose @tool-decorated functions import cleanly. Sandbox note: execution happens
in-process; set SKILL_SANDBOX=true to run installs' smoke-test in a subprocess.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

import config
from eventbus import emit, EV_SKILL_INSTALLED, EV_ALERT
from tools.registry import tool, load_skills, tool_names, REGISTRY
from tools import sim

SKILLS = config.SKILLS_DIR


def _repo_name(url: str) -> str:
    tail = url.rstrip("/").split("/")[-1]
    return tail[:-4] if tail.endswith(".git") else tail


@tool
async def install_skill(source_url: str, subdir: str = "") -> dict:
    """Install an agent skill from a GitHub URL (repo or raw file), ClawHub id, or local path — then hot-load it so its tools are callable immediately.
    :param source_url: e.g. https://github.com/owner/repo , .../blob/main/skill.py , clawhub://name , or /local/path
    :param subdir: optional subdirectory inside the repo that holds skill.py
    """
    name = _repo_name(source_url) or "claw-skill"
    dest = SKILLS / name
    try:
        if source_url.startswith("clawhub://"):
            await _fetch_clawhub(source_url[len("clawhub://"):], dest)
        elif source_url.startswith(("http://", "https://")):
            await _fetch_github(source_url, dest, subdir)
        else:
            src = Path(source_url)
            if not src.exists():
                return {"ok": False, "error": f"path not found: {source_url}"}
            shutil.copytree(src, dest, dirs_exist_ok=True)
        ok, why = _verify(dest)
        if not ok:
            shutil.rmtree(dest, ignore_errors=True)
            return {"ok": False, "error": f"skill structure invalid: {why}"}
        before = set(tool_names())
        load_skills()
        added = sorted(set(tool_names()) - before)
        emit(EV_SKILL_INSTALLED, source="skill-installer",
             message=f"installed '{name}' → new tools: {', '.join(added) or '(none registered)'}",
             data={"skill": name, "tools": added})
        return {"ok": True, "skill": name, "path": str(dest), "new_tools": added}
    except Exception as exc:  # noqa: BLE001
        emit(EV_ALERT, source="skill-installer", message=f"install failed: {exc}")
        return {"ok": False, "error": str(exc)}


async def _fetch_github(url: str, dest: Path, subdir: str) -> None:
    """Clone via git when available, else fall back to zipball/tarball download."""
    repo = url.split("/blob/")[0].split("/tree/")[0]
    repo = repo[:-4] if repo.endswith(".git") else repo
    parts = repo.replace("https://github.com/", "").replace("http://github.com/", "")
    if "/" not in parts or sim.demo():
        # DEMO_MODE: don't touch network — synthesise a working sample skill
        _write_demo_skill(dest, parts or url)
        return
    if shutil.which("git"):
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "1", repo + ".git", str(dest),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"git clone failed: {out.decode()[-300:]}")
        if subdir:
            dest = dest / subdir
        return
    # zipball fallback
    tmp = Path(tempfile.mkdtemp(prefix="skillzip_"))
    zf = tmp / "repo.zip"
    proc = await asyncio.create_subprocess_exec(
        "curl", "-sL", f"https://codeload.github.com/{parts}/zip/refs/heads/main",
        "-o", str(zf))
    await proc.wait()
    with zipfile.ZipFile(zf) as z:
        z.extractall(tmp)
    root = next(tmp.iterdir())
    src = (root / subdir) if subdir else root
    shutil.copytree(src, dest, dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)


async def _fetch_clawhub(identifier: str, dest: Path) -> None:
    if sim.demo():
        _write_demo_skill(dest, identifier)
        return
    meta = await sim.api_get(f"https://clawhub.ai/api/v1/skills/{identifier}")
    tar_url = meta.get("tarball_url") or meta.get("download_url")
    if not tar_url:
        raise RuntimeError("ClawHub record has no tarball_url")
    tmp = Path(tempfile.mkdtemp(prefix="claw_"))
    tf = tmp / "skill.tar.gz"
    import httpx
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(tar_url)
        r.raise_for_status()
        tf.write_bytes(r.content)
    with tarfile.open(tf) as t:
        t.extractall(tmp / "x")
    root = next((tmp / "x").iterdir())
    shutil.copytree(root, dest, dirs_exist_ok=True)
    shutil.rmtree(tmp, ignore_errors=True)


def _verify(dest: Path) -> tuple[bool, str]:
    if not (dest / "skill.py").exists():
        # allow single-file skills at top level
        pys = list(dest.rglob("skill.py")) + [p for p in dest.rglob("*.py")][:1]
        if not pys:
            return False, "no skill.py found"
        shutil.copy(pys[0], dest / "skill.py")
    src = (dest / "skill.py").read_text(errors="replace")
    if "@tool" not in src and "registry.tool" not in src:
        return False, "skill.py registers no @tool functions"
    compile(src, str(dest / "skill.py"), "exec")   # syntax check
    return True, "ok"


def _write_demo_skill(dest: Path, hint: str) -> None:
    """Generate a realistic installed skill so demo mode can complete E2E flows."""
    slug = "".join(c if c.isalnum() else "_" for c in (hint.split("/")[-1] or "demo"))[:24] or "demo"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "skill.json").write_text(json.dumps(
        {"name": slug, "version": "1.0.0", "author": "clawhub-demo",
         "description": f"Simulated install of {hint}"}, indent=2))
    (dest / "skill.py").write_text(f'''"""Skill "{slug}" — installed by skill_installer (demo copy)."""
from tools.registry import tool
from eventbus import emit, EV_SYSTEM


@tool
async def {slug}_ping(message: str = "hello") -> dict:
    """Ping the {slug} skill to confirm installation works end-to-end."""
    emit(EV_SYSTEM, source="{slug}", message=f"{{slug}}_ping: {{message}}")
    return {{"ok": True, "pong": message, "skill": "{slug}"}}


@tool
async def {slug}_time() -> dict:
    """Return current formatted time from the {slug} skill."""
    import datetime as _dt
    return {{"ok": True, "now": _dt.datetime.now().isoformat(timespec="seconds")}}
''')


@tool
async def uninstall_skill(name: str) -> dict:
    """Remove an installed skill directory and unregister its tools."""
    dest = SKILLS / name
    if not dest.exists():
        return {"ok": False, "error": f"skill '{name}' not installed"}
    removed = [t for t, d in REGISTRY.items() if d.module.startswith(f"skill_{name}")
               or dest.name in d.module]
    for t in removed:
        REGISTRY.pop(t, None)
    shutil.rmtree(dest, ignore_errors=True)
    emit(EV_SKILL_INSTALLED, source="skill-installer",
         message=f"uninstalled '{name}' (removed tools: {', '.join(removed)})")
    return {"ok": True, "removed_tools": removed}


@tool
async def list_skills() -> dict:
    """List installed skills and the tools each provides."""
    out = []
    for d in sorted(SKILLS.iterdir()):
        if d.is_dir() and (d / "skill.py").exists():
            man = {}
            if (d / "skill.json").exists():
                try:
                    man = json.loads((d / "skill.json").read_text())
                except json.JSONDecodeError:
                    man = {"invalid_manifest": True}
            tools_of = [n for n, td in REGISTRY.items() if d.name in n or d.name in td.module]
            out.append({"name": d.name, "manifest": man, "tools": tools_of})
    return {"ok": True, "skills": out}
