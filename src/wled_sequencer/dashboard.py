"""Dashboard (/): per-area brightness sliders and scenes, driving Home Assistant lights and lightd strips.

The browser only talks to this server (/api/dashboard/*); the Home Assistant token stays here. Areas are a hand-picked
list of HA light entities plus lightd targets (dashboard_areas.json, or DASHBOARD_CONFIG), so wall-panel backlights,
status LEDs and voice-assistant rings are never touched.
"""

from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).resolve().parent / "dashboard_areas.json"
WARM_KELVIN = 2700
NON_DIMMABLE_MODES = {"onoff", "unknown"}
CONFIRM_TIMEOUT = 4.0  # seconds to wait for HA + lightd to confirm a house scene
CONFIRM_POLL = 0.25
CLOCK_SLACK = 2.0  # seconds of clock skew allowed between this server and HA


def _is_dimmable(state_obj: dict[str, Any]) -> bool:
    """True when an HA light entity supports dimming (or reports a numeric brightness)."""
    attrs = state_obj.get("attributes") or {}
    modes = attrs.get("supported_color_modes")
    if isinstance(modes, list) and modes:
        return any(m not in NON_DIMMABLE_MODES for m in modes)
    return attrs.get("brightness") is not None


def _parse_iso_timestamp(value: Any) -> float | None:
    if not isinstance(value, str) or value in ("unknown", "unavailable", ""):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class DashboardError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    with open(path or DEFAULT_CONFIG, encoding="utf-8") as f:
        config = json.load(f)
    ids = [a["id"] for a in config.get("areas", [])]
    if len(ids) != len(set(ids)):
        raise ValueError("dashboard config: duplicate area ids")
    return config


def _request(method: str, url: str, body: Any = None, headers: dict[str, str] | None = None, timeout: float = 8.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _describe_http_error(what: str, e: Exception) -> str:
    if isinstance(e, urllib.error.HTTPError):
        detail = e.read().decode("utf-8", "replace")[:200]
        return f"{what}: HTTP {e.code} {detail}".strip()
    return f"{what}: {e}"


class Dashboard:
    def __init__(self, config: dict[str, Any] | None = None, ha_url: str = "", ha_token: str = "",
                 engine_url: str = "http://127.0.0.1:8765", timeout: float = 8.0,
                 confirm_timeout: float = CONFIRM_TIMEOUT) -> None:
        self.config = config if config is not None else load_config()
        self.confirm_timeout = confirm_timeout
        self.ha_url = ha_url.rstrip("/")
        self.ha_token = ha_token
        self.engine_url = engine_url.rstrip("/")
        self.timeout = timeout
        self.areas = {a["id"]: a for a in self.config.get("areas", [])}
        self._area_active_scenes: dict[str, str | None] = {}

    @classmethod
    def from_env(cls, engine_url: str) -> Dashboard:
        """HA_URL/HA_TOKEN, falling back to the HA CLI's HASS_SERVER/HASS_TOKEN."""
        env = os.environ.get
        return cls(load_config(env("DASHBOARD_CONFIG") or None), env("HA_URL") or env("HASS_SERVER") or "",
                   env("HA_TOKEN") or env("HASS_TOKEN") or "", engine_url)

    @property
    def ha_enabled(self) -> bool:
        return bool(self.ha_url and self.ha_token)

    # ---------- upstream calls ----------

    def _ha(self, method: str, path: str, body: Any = None) -> Any:
        if not self.ha_enabled:
            raise DashboardError(503, "Home Assistant isn't configured: set HA_URL and HA_TOKEN on the web server.")
        return _request(method, self.ha_url + path, body, {"Authorization": f"Bearer {self.ha_token}"}, self.timeout)

    def _ha_service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        self._ha("POST", f"/api/services/{domain}/{service}", data)

    def _engine(self, method: str, path: str, body: Any = None) -> Any:
        return _request(method, self.engine_url + "/api" + path, body, timeout=self.timeout)

    def _area(self, area_id: str) -> dict[str, Any]:
        area = self.areas.get(area_id)
        if area is None:
            raise DashboardError(404, f"Unknown area: {area_id}")
        return area

    @staticmethod
    def _finish(errors: list[str], attempted: int) -> dict[str, Any]:
        """Partial failures come back as warnings; if every part failed, the request fails."""
        if errors and len(errors) >= attempted:
            raise DashboardError(502, "; ".join(errors))
        return {"status": "ok", "warnings": errors}

    # ---------- API ----------

    def describe(self) -> dict[str, Any]:
        return {
            "ha": self.ha_enabled,
            "areas": [{"id": a["id"], "name": a["name"], "lights": len(a.get("lights", [])),
                       "strips": len(a.get("strips", []))} for a in self.areas.values()],
            "area_scenes": [{"id": k, "label": v.get("label", k)} for k, v in self.config.get("area_scenes", {}).items()],
            "house_scenes": [{"id": s["id"], "label": s.get("label", s["id"])} for s in self.config.get("house_scenes", [])],
        }

    def state(self) -> dict[str, Any]:
        """Each area's brightness 0..100: the mean of its lit dimmable HA lights and active strips (0 when all off)."""
        states: dict[str, dict[str, Any]] = {}
        errors: list[str] = []
        try:
            states = {s["entity_id"]: s for s in self._ha("GET", "/api/states") or []}
        except DashboardError:
            raise
        except Exception as e:
            errors.append(_describe_http_error("Home Assistant", e))
        strip_level: dict[str, float] = {}
        playing_strips: set[str] | None = None
        setup: list[dict[str, Any]] = []
        try:
            for c in (self._engine("GET", "/controllers") or {}).get("controllers", []):
                for ch in c.get("channels", []):
                    strip_level[ch["target"]] = float(ch.get("brightness") or 0)
                if "segments reset" in (c.get("warnings") or []):
                    names = [ch.get("name") for ch in c.get("channels", []) if ch.get("name")]
                    setup.append({"controller": c.get("id"), "name": names[0] if names else c.get("id"),
                                  "message": "segments reset"})
            runs_resp = self._engine("GET", "/runs")
            if isinstance(runs_resp, dict) and "runs" in runs_resp:
                playing_strips = {
                    t for r in runs_resp.get("runs", [])
                    if not (r.get("pattern") == "solid" and str((r.get("params") or {}).get("color", "")).lower() in ("#000000", "#000"))
                    for t in r.get("targets", [])
                }
        except Exception as e:
            errors.append(_describe_http_error("lightd", e))

        active_lightd_scenes: set[str] | None = None
        try:
            scenes_resp = self._engine("GET", "/scenes")
            if isinstance(scenes_resp, dict) and isinstance(scenes_resp.get("scenes"), list):
                active_lightd_scenes = {s["name"] for s in scenes_resp["scenes"] if s.get("active")}
        except Exception:
            active_lightd_scenes = None

        scene_timestamps: dict[str, float] = {}
        for hs in self.config.get("house_scenes", []):
            sid = hs["id"]
            st = states.get(hs.get("entity", f"scene.{sid}"))
            if st is not None:
                ts = _parse_iso_timestamp(st.get("state"))
                if ts is not None:
                    scene_timestamps[sid] = ts

        # The house scene with the newest HA activation that lightd also reports active (HA only if lightd is down).
        active_scene: str | None = None
        candidates = [(ts, sid) for sid, ts in scene_timestamps.items()
                      if active_lightd_scenes is None or sid in active_lightd_scenes]
        if candidates:
            active_scene = max(candidates)[1]

        out: dict[str, Any] = {}
        for area in self.areas.values():
            aid = area["id"]
            lights = [states[e] for e in area.get("lights", []) if e in states]
            available = [s for s in lights if s.get("state") not in ("unavailable", "unknown")]
            lit = [s for s in available if s.get("state") == "on"]
            dimmable_avail = [s for s in available if _is_dimmable(s)]
            light_pcts = [
                float(b) / 255 * 100 for s in lit if _is_dimmable(s)
                for b in [(s.get("attributes") or {}).get("brightness")]
                if isinstance(b, (int, float))
            ]

            known_strips = [t for t in area.get("strips", []) if t in strip_level]
            playing = [t for t in known_strips if playing_strips is None or t in playing_strips]
            lit_strips = [strip_level[t] * 100 for t in playing if strip_level[t] > 0]
            dimmable = bool(dimmable_avail or known_strips or not available)

            if aid == "pool":
                samples = light_pcts + ([strip_level[t] * 100 for t in playing] if not light_pcts else lit_strips)
                has_sources = bool(available or known_strips)
            elif dimmable_avail:
                samples = light_pcts
                has_sources = True
            elif available and not known_strips:
                # on/off-only lights: the slider is a toggle, so report 100/0 rather than a fake level
                samples = [100.0] if lit else []
                has_sources = True
            else:
                samples = [strip_level[t] * 100 for t in playing]
                has_sources = bool(available or known_strips)

            brightness = (round(sum(samples) / len(samples)) if samples else 0) if has_sources else None
            out[aid] = {
                "brightness": brightness,
                "on": bool(lit or lit_strips),
                "dimmable": dimmable,
                "available": len(available),
                "lights": len(area.get("lights", [])),
                "active_scene": self._area_active_scenes.get(aid),
            }
        return {
            "areas": out,
            "active_scene": active_scene,
            "scene_timestamps": scene_timestamps,
            "setup": setup,
            "lightd_active_scenes": sorted(active_lightd_scenes) if active_lightd_scenes is not None else [],
            "warnings": errors,
        }

    def _start_idle_strips(self, targets: list[str]) -> None:
        """Strips with nothing playing (or playing solid black from pool_off/off) stay dark, so give them warm white."""
        busy = {
            t for run in (self._engine("GET", "/runs") or {}).get("runs", [])
            if not (run.get("pattern") == "solid" and str((run.get("params") or {}).get("color", "")).lower() in ("#000000", "#000"))
            for t in run.get("targets", [])
        }
        idle = [t for t in targets if t not in busy]
        if idle:
            self._engine("POST", "/runs", {"targets": idle, "pattern": "white", "params": {"kelvin": WARM_KELVIN}})

    def set_brightness(self, area_id: str, brightness: Any) -> dict[str, Any]:
        area = self._area(area_id)
        try:
            pct = max(0, min(100, int(round(float(brightness)))))
        except (TypeError, ValueError):
            raise DashboardError(400, "brightness must be a number 0..100")
        errors: list[str] = []
        attempted = 0
        if area.get("lights"):
            attempted += 1
            try:
                if pct == 0:
                    self._ha_service("light", "turn_off", {"entity_id": area["lights"]})
                else:
                    self._ha_service("light", "turn_on", {"entity_id": area["lights"], "brightness_pct": pct})
            except DashboardError:
                raise
            except Exception as e:
                errors.append(_describe_http_error("Home Assistant", e))
        if area.get("strips"):
            attempted += 1
            try:
                if pct > 0:
                    self._start_idle_strips(area["strips"])
                self._engine("PUT", "/brightness", {"targets": area["strips"], "brightness": pct / 100})
            except Exception as e:
                errors.append(_describe_http_error("lightd", e))
        self._area_active_scenes.pop(area_id, None)  # a hand-set level is no longer the area scene
        return self._finish(errors, attempted)

    def apply_area_scene(self, area_id: str, scene_id: str) -> dict[str, Any]:
        area = self._area(area_id)
        scene = self.config.get("area_scenes", {}).get(scene_id)
        if scene is None:
            raise DashboardError(404, f"Unknown scene: {scene_id}")
        pct = int(scene.get("brightness", 100))
        kelvin = int(scene.get("kelvin", WARM_KELVIN))
        party = bool(scene.get("party"))
        errors: list[str] = []
        attempted = 0

        lights = area.get("lights", [])
        if lights:
            if party:
                # Every bulb its own saturated color, evenly spread around the wheel from a random start.
                start = random.uniform(0, 360)
                calls = [{"entity_id": e, "brightness_pct": pct, "hs_color": [(start + i * 360 / len(lights)) % 360, 100]}
                         for i, e in enumerate(lights)]
            else:
                calls = [{"entity_id": lights, "brightness_pct": pct, "color_temp_kelvin": kelvin}]
            attempted += len(calls)

            def call(data: dict[str, Any]) -> str | None:
                try:
                    self._ha_service("light", "turn_on", data)
                    return None
                except DashboardError:
                    raise
                except Exception as e:
                    return _describe_http_error(f"Home Assistant {data['entity_id']}", e)

            with ThreadPoolExecutor(max_workers=8) as pool:
                errors += [r for r in pool.map(call, calls) if r]

        strips = area.get("strips", [])
        if strips:
            attempted += 1
            run = ({"targets": strips, "pattern": "rainbow", "params": {}} if party
                   else {"targets": strips, "pattern": "white", "params": {"kelvin": kelvin}})
            try:
                self._engine("POST", "/runs", run)
                self._engine("PUT", "/brightness", {"targets": strips, "brightness": pct / 100})
            except Exception as e:
                errors.append(_describe_http_error("lightd", e))
        res = self._finish(errors, attempted)
        self._area_active_scenes[area_id] = scene_id
        res["active_scene"] = scene_id
        return res

    def activate_house_scene(self, scene_id: str) -> dict[str, Any]:
        """Whole-house HA scene; HA's lights_sync_scene automation mirrors it onto the strips."""
        scene = next((s for s in self.config.get("house_scenes", []) if s["id"] == scene_id), None)
        if scene is None:
            raise DashboardError(404, f"Unknown scene: {scene_id}")
        try:
            requested = time.time()
            self._ha_service("scene", "turn_on", {"entity_id": scene["entity"]})
        except DashboardError:
            raise
        except Exception as e:
            raise DashboardError(502, _describe_http_error("Home Assistant", e))
        self._area_active_scenes.clear()
        confirmed, lightd_active = self._confirm_house_scene(scene, requested)
        return {"status": "ok", "warnings": [], "confirmed": confirmed, "lightd_active": lightd_active,
                "active_scene": scene_id if confirmed else None}

    def _confirm_house_scene(self, scene: dict[str, Any], requested: float) -> tuple[bool, bool | None]:
        """Wait (up to confirm_timeout) for HA's scene timestamp to move past the click, and look at lightd.

        Confirmed = HA reports the scene activated at/after the click (CLOCK_SLACK allows small clock skew) and,
        when lightd knows a scene of that name, lightd reports it active. lightd_active is None if it has no such scene.
        """
        deadline = time.monotonic() + self.confirm_timeout
        ha_ok, lightd_active = False, None
        while True:
            if not ha_ok:
                try:
                    st = self._ha("GET", f"/api/states/{scene['entity']}") or {}
                    ts = _parse_iso_timestamp(st.get("state"))
                    ha_ok = ts is not None and ts >= requested - CLOCK_SLACK
                except Exception:
                    pass
            try:
                lightd_active = bool((self._engine("GET", f"/scenes/{scene['id']}") or {}).get("active"))
            except urllib.error.HTTPError as e:
                lightd_active = None if e.code == 404 else False
            except Exception:
                lightd_active = False
            if ha_ok and lightd_active is not False:
                return True, lightd_active
            if time.monotonic() >= deadline:
                return False, lightd_active
            time.sleep(CONFIRM_POLL)
