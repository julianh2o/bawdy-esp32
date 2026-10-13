"""Pattern library: built-in ``.lua`` files (read-only) plus user patterns saved through the API.

Saving under a built-in's name stores an edited copy in the user directory, which is used instead of the built-in.
Deleting that copy reverts to the built-in; the built-in files themselves are never changed.
"""

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

    def _is_builtin(self, name: str) -> bool:
        return self.builtin_dir is not None and (self.builtin_dir / f"{name}.lua").is_file()

    def _info(self, name: str, path: Path, user: bool) -> dict[str, Any]:
        """``builtin``: a built-in of this name exists. ``modified``: an edited copy is used instead of it."""
        builtin = self._is_builtin(name)
        return {"name": name, "builtin": builtin, "modified": builtin and user}

    def names(self) -> list[str]:
        seen: set[str] = set()
        for base in (self.builtin_dir, self.user_dir):
            if base is not None and base.is_dir():
                seen.update(p.stem for p in base.glob("*.lua") if NAME_RE.match(p.stem))
        return sorted(seen)

    def get(self, name: str) -> dict[str, Any]:
        found = self._path(name)
        if found is None:
            raise LibraryError(f"No pattern named '{name}'", 404)
        path, builtin_file = found
        return {**self._info(name, path, not builtin_file), "source": path.read_text(encoding="utf-8")}

    def source(self, name: str) -> str:
        return self.get(name)["source"]

    def list(self) -> list[dict[str, Any]]:
        """Every pattern with its params schema (or its compile error)."""
        out = []
        for name in self.names():
            found = self._path(name)
            if found is None:
                continue
            item = self._info(name, found[0], not found[1])
            try:
                item["params"] = Pattern(found[0].read_text(encoding="utf-8")).schema
            except PatternError as e:
                item["error"] = e.to_dict()
            out.append(item)
        return out

    def save(self, name: str, source: str) -> dict[str, Any]:
        """Save a user pattern after checking it compiles. A built-in's name saves an edited copy of it."""
        if self.user_dir is None:
            raise LibraryError("No user pattern directory configured", 409)
        if not NAME_RE.match(name):
            raise LibraryError("Pattern names use lowercase letters, digits, '-' and '_' (max 64)")
        if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise LibraryError("Pattern source is too large")
        Pattern(source)  # raises PatternError
        self.user_dir.mkdir(parents=True, exist_ok=True)
        (self.user_dir / f"{name}.lua").write_text(source, encoding="utf-8")
        return self._info(name, self.user_dir / f"{name}.lua", True)

    def delete(self, name: str) -> None:
        """Delete a user pattern, or an edited built-in's copy (reverting to the built-in)."""
        found = self._path(name)
        if found is None:
            raise LibraryError(f"No pattern named '{name}'", 404)
        path, builtin_file = found
        if builtin_file:
            raise LibraryError(f"'{name}' is a built-in pattern and hasn't been edited", 409)
        path.unlink()
