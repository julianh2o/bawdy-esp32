"""Server-side proxy for telemeter's music API."""
import json
import os
import urllib.error
import urllib.request


class MusicError(Exception):
    def __init__(self, status, message, code="error"):
        super().__init__(message)
        self.status, self.code = status, code


FIELDS = ("on_screen", "state", "track", "artist", "album", "artwork_url", "position", "duration",
          "volume", "muted", "busy", "idle_teardown_at")


class Music:
    def __init__(self, url="", token="", timeout=8):
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout

    @classmethod
    def from_env(cls):
        return cls(os.getenv("TELEMETER_URL", ""), os.getenv("TELEMETER_TOKEN", ""))

    @property
    def enabled(self):
        return bool(self.url and self.token)

    def _call(self, method, path, body=None, timeout=None):
        if not self.enabled:
            raise MusicError(503, "Music isn't configured.", "not_configured")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as e:
            try: payload = json.loads(e.read())
            except Exception: payload = {}
            raise MusicError(e.code, payload.get("error", "Music request failed."), payload.get("code", "error"))
        except Exception:
            raise MusicError(502, "Couldn't reach demeter.", "telemeter_unreachable")

    def state(self):
        return self._call("GET", "/music")

    def action(self, name):
        return self._call("POST", "/music/" + name, {}, timeout=45 if name == "play" else None)

    def play(self): return self.action("play")
    def pause(self): return self.action("pause")
    def next(self): return self.action("next")
    def previous(self): return self.action("previous")
    def set_volume(self, value): return self._call("PUT", "/music/volume", {"volume": value})

    @staticmethod
    def public(state):
        out = {k: state.get(k) for k in FIELDS}
        out["speaker"] = {k: (state.get("speaker") or {}).get(k) for k in ("name", "connected")}
        jam = state.get("jam") or {}
        out["jam"] = {k: jam.get(k) for k in ("active", "url", "extension_online")}
        return out
