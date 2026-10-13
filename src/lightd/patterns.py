"""Pattern library: built-in ``.lua`` files (read-only) plus user patterns saved through the API."""

from __future__ import annotations
import re
from pathlib import Path
from typing import Any

from lightd.lua import Pattern, PatternError

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
MAX_SOURCE_BYTES = 64 * 1024


class LibraryError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


class PatternLibrary:
    def __init__(self, builtin_dir: Path | str | None, user_dir: Path | str | None) -> None:
        self.builtin_dir = Path(builtin_dir) if builtin_dir else None
        self.user_dir = Path(user_dir) if user_dir else None

    def _path(self, name: str) -> tuple[Path, bool] | None:
        if not NAME_RE.match(name):
            return None
        for base, builtin in ((self.user_dir, False), (self.builtin_dir, True)):
            if base is not None and (base / f"{name}.lua").is_file():
                return base / f"{name}.lua", builtin
        return None

    def names(self) -> list[tuple[str, bool]]:
        seen: dict[str, bool] = {}
        for base, builtin in ((self.builtin_dir, True), (self.user_dir, False)):
            if base is not None and base.is_dir():
                for p in base.glob("*.lua"):
                    if NAME_RE.match(p.stem):
                        seen.setdefault(p.stem, builtin)
        return sorted(seen.items())

    def get(self, name: str) -> dict[str, Any]:
        found = self._path(name)
        if found is None:
            raise LibraryError(f"No pattern named '{name}'", 404)
        path, builtin = found
        return {"name": name, "builtin": builtin, "source": path.read_text(encoding="utf-8")}

    def source(self, name: str) -> str:
        return self.get(name)["source"]

    def list(self) -> list[dict[str, Any]]:
        """Every pattern with its params schema (or its compile error)."""
        out = []
        for name, builtin in self.names():
            item: dict[str, Any] = {"name": name, "builtin": builtin}
            try:
                item["params"] = Pattern(self.source(name)).schema
            except PatternError as e:
                item["error"] = e.to_dict()
            out.append(item)
        return out

    def save(self, name: str, source: str) -> dict[str, Any]:
        """Save a user pattern after checking it compiles. Built-in names can't be overwritten."""
        if self.user_dir is None:
            raise LibraryError("No user pattern directory configured", 409)
        if not NAME_RE.match(name):
            raise LibraryError("Pattern names use lowercase letters, digits, '-' and '_' (max 64)")
        if self.builtin_dir is not None and (self.builtin_dir / f"{name}.lua").is_file():
            raise LibraryError(f"'{name}' is a built-in pattern; save under another name", 409)
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise LibraryError("Pattern source is too large")
        Pattern(source)  # raises PatternError
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / f"{name}.lua").write_text(source, encoding="utf-8")
        return {"name": name, "builtin": False}

    def delete(self, name: str) -> None:
        found = self._path(name)
        if found is None:
            raise LibraryError(f"No pattern named '{name}'", 404)
        path, builtin = found
        if builtin:
            raise LibraryError(f"'{name}' is a built-in pattern", 409)
        path.unlink()
