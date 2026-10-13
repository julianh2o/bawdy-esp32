"""Compile and render Lua patterns with lupa (Lua 5.4), using the shared ``runtime.lua``."""

from __future__ import annotations
import re
from pathlib import Path
from typing import Any

from lupa import lua54

RUNTIME_PATH = Path(__file__).with_name("runtime.lua")
DEFAULT_BUDGET = 2_000_000  # Lua instructions per channel per frame

_LINE_RE = re.compile(r"^pattern:(\d+):\s*")


class PatternError(Exception):
    """A pattern failed to compile or render. ``line`` is the source line when Lua reported one."""

    def __init__(self, message: str) -> None:
        m = _LINE_RE.match(message)
        self.line = int(m.group(1)) if m else None
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {"message": str(self), "line": self.line}


def _to_lua(L: Any, value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("utf-8")
    if isinstance(value, dict):
        return L.table_from({_to_lua(L, k): _to_lua(L, v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return L.table_from([_to_lua(L, v) for v in value])
    return value


def _from_lua(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if lua54.lua_type(value) == "table":
        items = {_from_lua(k): _from_lua(v) for k, v in value.items()}
        if items and all(isinstance(k, int) for k in items):
            return [items[k] for k in sorted(items)]
        return items
    return value


class Pattern:
    """One compiled pattern in its own Lua state. Not thread-safe: render from one thread."""

    def __init__(self, source: str, budget: int = DEFAULT_BUDGET) -> None:
        self.source = source
        self._L = lua54.LuaRuntime(unpack_returned_tuples=True, register_eval=False, register_builtins=False, encoding=None)
        self._rt = self._L.execute(RUNTIME_PATH.read_bytes())
        self._rt[b"budget"] = int(budget)
        result = self._rt[b"compile"](source.encode("utf-8"))
        env, err = result if isinstance(result, tuple) else (result, None)
        if env is None:
            raise PatternError(_from_lua(err) or "compile failed")
        self._env = env
        self.schema: dict[str, dict[str, Any]] = {
            name: {k: v for k, v in spec.items() if v is not None}
            for name, spec in (_from_lua(self._rt[b"schema"](env)) or {}).items()
        } if self._has_params() else {}
        # type = "api" params: param name -> values API key. Filled by set_api_values, not by set_params.
        self.api_keys: dict[str, str] = {
            name: str(spec.get("key") or name) for name, spec in self.schema.items() if spec.get("type") == "api"
        }
        self._api_version: int | None = None
        self.values: dict[str, Any] = {}
        self._p = self._rt[b"params"](env, None)

    def _has_params(self) -> bool:
        return lua54.lua_type(self._env[b"params"]) == "table"

    def set_params(self, values: dict[str, Any] | None) -> None:
        """Replace the param values (merged with schema defaults inside Lua)."""
        self.values = dict(values or {})
        self._p = self._rt[b"params"](self._env, _to_lua(self._L, self.values) if self.values else None)
        self._api_version = None  # p was rebuilt: put the API values back in

    def set_api_values(self, values: dict[str, Any], version: int) -> None:
        """Fill the api params from the values API; a key that isn't set gives the param's default."""
        if not self.api_keys or version == self._api_version:
            return
        self._api_version = version
        for name, key in self.api_keys.items():
            value = values[key] if key in values else self.schema[name].get("default")
            self._p[name.encode("utf-8")] = _to_lua(self._L, value)

    def params(self) -> dict[str, Any]:
        """The p table the pattern currently sees."""
        return _from_lua(self._p) or {}

    def render(self, count: int, t: float, reversed: bool = False, rgbw: bool = False) -> bytes:
        """``count`` pixels as RGB bytes (w added to r, g, b), or RGBW bytes. Raises ``PatternError``."""
        ok, data = self._rt[b"render"](self._env, int(count), float(t), self._p, bool(reversed), bool(rgbw))
        if not ok:
            raise PatternError(_from_lua(data) or "render failed")
        return data
