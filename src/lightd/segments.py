"""Known-good WLED segment layouts, so a board that lost them (power cycle → WLED's default segment) can be put back.

Gledopto boards driving Govee lights need two interleaved segments, "Govee RGB" and "Govee White" (spacing 1, one of
them offset by an LED). WLED sometimes comes back from a power cycle with its default single segment, and lightd then
loses the linked channel. This module:

- snapshots a board's ``/json/state`` ``seg`` array into ``<dir>/<controller>.json`` whenever the board shows a linked
  (Govee) channel, or on demand ("save as known-good");
- compares the live segments with the snapshot on every refresh (start, stop, grp, spc, of, name) and flags a mismatch
  on a board that lost its linked channel as ``segments reset``;
- restores the snapshot by POSTing it to the board's ``/json/state`` (extra segments are deleted), and optionally saves
  the result as a WLED preset and makes it the boot preset so the board comes up right by itself next time.
"""

from __future__ import annotations
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

from lightd.config import ID_RE, write_json_atomic

log = logging.getLogger(__name__)

FORMAT_VERSION = 1
# What's kept from each live segment (enough to recreate it) and what's compared.
KEEP_KEYS = ("id", "start", "stop", "grp", "spc", "of", "n", "rev", "on", "bri")
COMPARE_DEFAULTS = {"start": 0, "stop": 0, "grp": 1, "spc": 0, "of": 0, "n": ""}
DEFAULT_PRESET_ID = 250  # WLED preset slot used for the boot preset (1..250)
PRESET_NAME = "lightd segments"


class SegmentError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def clean_segments(seg: Any) -> list[dict[str, Any]]:
    """The parts of a ``/json/state`` ``seg`` array worth keeping (no colors/effects, no empty segments)."""
    out = []
    for s in seg if isinstance(seg, list) else []:
        if not isinstance(s, dict) or int(s.get("stop") or 0) <= int(s.get("start") or 0):
            continue
        out.append({k: s[k] for k in KEEP_KEYS if k in s})
    return sorted(out, key=lambda s: int(s.get("id") or 0))


def layout(seg: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    """Comparable shape of a segment list: (start, stop, grp, spc, of, name) per segment, in start order."""
    rows = []
    for s in seg:
        row = []
        for k, default in COMPARE_DEFAULTS.items():
            v = s.get(k, default)
            row.append((v or "").strip() if k == "n" else int(v if v is not None else default))
        rows.append(tuple(row))
    return sorted(rows)


class SegmentStore:
    """Snapshots in ``<dir>/<controller id>.json`` (in memory only when ``directory`` is None)."""

    def __init__(self, directory: Path | str | None, preset_id: int = DEFAULT_PRESET_ID) -> None:
        self.dir = Path(directory) if directory else None
        self.preset_id = preset_id
        self._memory: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _path(self, cid: str) -> Path | None:
        if not ID_RE.match(cid):
            raise SegmentError(f"Invalid controller id '{cid}'")
        return self.dir / f"{cid}.json" if self.dir else None

    def load(self, cid: str) -> dict[str, Any] | None:
        path = self._path(cid)
        if path is None:
            return self._memory.get(cid)
        if not path.is_file():
            return None
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            log.warning("%s: can't read saved segments %s: %s", cid, path, e)
            return None
        return doc if isinstance(doc, dict) and isinstance(doc.get("seg"), list) else None

    def save(self, cid: str, seg: Any, reason: str = "manual") -> dict[str, Any]:
        segments = clean_segments(seg)
        if not segments:
            raise SegmentError(f"{cid} reports no segments to save")
        doc = {"version": FORMAT_VERSION, "controller": cid, "saved": time.time(), "reason": reason, "seg": segments}
        with self._lock:
            path = self._path(cid)
            if path is None:
                self._memory[cid] = doc
            else:
                write_json_atomic(path, doc)
        return doc

    # ---------- per refresh ----------

    def check(self, entry: Any) -> dict[str, Any]:
        """Compare a freshly refreshed board with its snapshot; take a snapshot while it looks right.

        Sets and returns ``entry.segment_status``: ``{"saved", "match", "reset"}``. ``reset`` means the board lost its
        linked channel and its segments differ from the known-good ones.
        """
        c = entry.controller
        live = clean_segments(getattr(c, "raw_segments", None))
        linked = any(ch.linked for ch in (c.channels or []))
        saved = self.load(entry.id)
        match = saved is not None and layout(saved["seg"]) == layout(live)
        if linked and live and not match:
            try:
                saved = self.save(entry.id, live, reason="auto")
                match = True
                log.info("%s: saved its segments as known-good (%d segments, linked channel present)", entry.id, len(live))
            except (OSError, SegmentError) as e:
                log.warning("%s: couldn't save segments: %s", entry.id, e)
        reset = bool(saved is not None and not match and not linked)
        was = getattr(entry, "segment_status", None) or {}
        if reset and not was.get("reset"):
            log.warning("%s: segments were reset (power cycle?): %d live segment(s) don't match the %d saved; "
                        "restore them from Setup", entry.id, len(live), len(saved["seg"]))
        entry.segment_status = {"saved": saved is not None, "match": match if saved is not None else None, "reset": reset}
        return entry.segment_status

    # ---------- restore ----------

    def restore(self, entry: Any, refresh: Any, boot_preset: bool = False) -> dict[str, Any]:
        """POST the saved segments to the board, re-read it (``refresh(entry)``) and report whether the linked channel
        is back. With ``boot_preset``, also save the restored state as WLED preset ``preset_id`` and make it the
        preset the board applies at boot."""
        saved = self.load(entry.id)
        if saved is None:
            raise SegmentError(f"No saved segments for {entry.id}", 404)
        c = entry.controller
        keep_ids = {int(s.get("id") or 0) for s in saved["seg"]}
        live_ids = {int(s.get("id") or 0) for s in clean_segments(getattr(c, "raw_segments", None))}
        body = [dict(s) for s in saved["seg"]] + [{"id": i, "stop": 0} for i in sorted(live_ids - keep_ids)]
        c.set_state(seg=body)
        log.info("%s: restored %d saved segment(s)%s", entry.id, len(saved["seg"]),
                 f", deleted segment(s) {sorted(live_ids - keep_ids)}" if live_ids - keep_ids else "")
        refresh(entry)
        linked = any(ch.linked for ch in (c.channels or []))
        result: dict[str, Any] = {"status": "ok" if linked else "error", "linked": linked,
                                  "segments": entry_status(entry), "boot_preset": None}
        if not linked:
            result["message"] = f"{entry.id}: segments sent, but no linked channel showed up after the refresh"
        elif boot_preset:
            c.set_state(psave=self.preset_id, n=PRESET_NAME, ib=True, sb=True)
            c.http.set_cfg({"def": {"ps": self.preset_id, "on": True}})
            result["boot_preset"] = self.preset_id
            log.info("%s: saved the restored segments as preset %d and made it the boot preset", entry.id, self.preset_id)
        return result


def entry_status(entry: Any) -> dict[str, Any]:
    return dict(getattr(entry, "segment_status", None) or {"saved": False, "match": None, "reset": False})
