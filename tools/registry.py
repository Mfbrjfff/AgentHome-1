"""Tool registry + @tool decorator.

Every module in ``tools/`` decorates async functions with ``@tool``; the
registry converts their signature + docstring into an OpenAI/Ollama-compatible
JSON schema consumed by the native function-calling loop in ``agent.py``.

Skill modules dropped into ``skills/<name>/skill.py`` are hot-loaded with
:func:`load_skills`, which simply imports them — their decorators re-register
everything, so a freshly installed skill is callable *immediately*, no restart.
"""
from __future__ import annotations

import asyncio
import functools
import importlib
import inspect
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, get_type_hints

import config
from eventbus import emit, EV_TOOL_CALL, EV_TOOL_RESULT, EV_SKILL_INSTALLED

_TYPE_MAP = {str: "string", int: "integer", float: "number", bool: "boolean",
             list: "array", dict: "object"}


@dataclass
class ToolDef:
    name: str
    func: Callable[..., Any]
    description: str
    parameters: dict[str, Any] = field(default_factory=dict)
    module: str = ""

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


REGISTRY: dict[str, ToolDef] = {}


def tool(func: Callable[..., Any]) -> Callable[..., Any]:
    """Register an async (or sync) callable as an agent tool."""
    name = getattr(func, "tool_name", func.__name__)
    doc = inspect.getdoc(func) or ""
    desc = doc.split("\n")[0].strip() if doc else name
    try:
        hints = get_type_hints(func)
    except Exception:
        hints = {}
    props: dict[str, Any] = {}
    required: list[str] = []
    sig = inspect.signature(func)
    for pname, p in sig.parameters.items():
        if pname in ("self", "kwargs") or p.kind == p.VAR_KEYWORD:
            continue
        if pname == "args" and p.kind == p.VAR_POSITIONAL:
            continue
        t = hints.get(pname, str)
        origin = getattr(t, "__origin__", None)
        jtype = _TYPE_MAP.get(t if origin is None else origin, "string")
        prop: dict[str, Any] = {"type": jtype}
        # per-parameter description:  ":param x: text" lines in docstring
        for line in doc.splitlines():
            s = line.strip()
            if s.startswith(f":param {pname}:"):
                prop["description"] = s[len(f":param {pname}:"):].strip()
        if origin is not None and origin in (list,):
            prop["items"] = {"type": "string"}
        props[pname] = prop
        if p.default is inspect.Parameter.empty:
            required.append(pname)
    REGISTRY[name] = ToolDef(
        name=name, func=func, description=desc,
        parameters={"type": "object", "properties": props,
                    "required": [r for r in required if r != "args"]},
        module=func.__module__,
    )

    @functools.wraps(func)
    async def wrapper(*a, **kw):
        return await call_tool(name, kw or dict(zip([p for p in sig.parameters], a)))

    wrapper._is_tool = True  # type: ignore[attr-defined]
    return wrapper


def tool_names() -> list[str]:
    return sorted(REGISTRY)


def tool_schemas() -> list[dict[str, Any]]:
    return [t.schema() for t in REGISTRY.values()]


async def call_tool(name: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
    """Single entry point: streams call/result to the event bus, catches errors."""
    args = args or {}
    td = REGISTRY.get(name)
    if td is None:
        return {"ok": False, "error": f"unknown tool '{name}'"}
    emit(EV_TOOL_CALL, source="agent", message=f"{name}({json.dumps(args, default=str)[:200]})",
         data={"tool": name, "args": args})
    t0 = time.time()
    try:
        if inspect.iscoroutinefunction(td.func):
            result = await td.func(**args)
        else:  # sync tools run off-loop so blocking IO never stalls the harness
            result = await asyncio.to_thread(functools.partial(td.func, **args))
        if not isinstance(result, dict):
            result = {"ok": True, "result": result}
    except Exception as exc:  # noqa: BLE001 - agent must see the error and adapt
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    out = {"ok": result.get("ok", True), "tool": name,
           "ms": round((time.time() - t0) * 1000), "data": result}
    emit(EV_TOOL_RESULT, source="agent",
         message=f"{name} -> {'ok' if out['ok'] else 'ERROR'} ({out['ms']}ms)",
         data=out)
    return out


# ---------------------------------------------------------------- skills
def load_core_tools() -> None:
    """Import every tools/*.py so decorators register."""
    from pathlib import Path
    for path in sorted(Path(config.TOOLS_DIR).glob("*.py")):
        if path.name.startswith("_"):
            continue
        mod = f"tools.{path.stem}"
        try:
            if mod in sys.modules:
                importlib.reload(sys.modules[mod])
            else:
                importlib.import_module(mod)
        except Exception as exc:  # keep booting even if one integration fails
            print(f"[registry] WARN core tool {mod} failed to load: {exc}")


def load_skills() -> list[str]:
    """Hot-load skills/<name>/skill.py (or <name>.py) into the registry."""
    loaded: list[str] = []
    from pathlib import Path
    if not config.SKILLS_DIR.exists():
        return loaded
    sys.path.insert(0, str(config.SKILLS_DIR))
    for entry in sorted(config.SKILLS_DIR.iterdir()):
        files: list[Path] = []
        if entry.is_dir() and (entry / "skill.py").exists():
            files = [entry / "skill.py"]
            manifest = entry / "skill.json"
            if manifest.exists():
                try:
                    json.loads(manifest.read_text())
                except json.JSONDecodeError:
                    print(f"[registry] skill {entry.name}: bad skill.json (ignored)")
        elif entry.suffix == ".py" and entry.name != "__init__.py":
            files = [entry]
        for f in files:
            mod_name = f"skill_{f.parent.name if f.name == 'skill.py' else f.stem}".replace("-", "_")
            try:
                spec = importlib.util.spec_from_file_location(mod_name, f)
                if spec and spec.loader:
                    m = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = m
                    spec.loader.exec_module(m)
                    new = [n for n in tool_names() if getattr(REGISTRY[n], "module", "") == mod_name]
                    if new:
                        loaded.extend(new)
                        emit(EV_SKILL_INSTALLED, source="registry",
                             message=f"skill '{f.parent.name}' provides {', '.join(new)}")
            except Exception as exc:  # noqa: BLE001
                print(f"[registry] WARN skill {f} failed: {exc}")
    return loaded
