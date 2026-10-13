"""Minimal client for WLED's JSON API (/json/info, /json/cfg, /json/state)."""

from __future__ import annotations
import json
import urllib.error
import urllib.request
from typing import Any

from wled.models import WledError

DEFAULT_TIMEOUT = 2.0


class WledHttp:
    """Talks to one board's HTTP API. ``host`` may change (DHCP); set it directly."""

    def __init__(self, host: str, timeout: float = DEFAULT_TIMEOUT) -> None:
        self.host = host
        self.timeout = timeout

    def _url(self, path: str) -> str:
        return f"http://{self.host}{path}"

    def get(self, path: str) -> dict[str, Any]:
        try:
            with urllib.request.urlopen(self._url(path), timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise WledError(f"{self.host}{path}: {getattr(e, 'reason', e)}") from e
        if not isinstance(data, dict):
            raise WledError(f"{self.host}{path}: expected a JSON object")
        return data

    def post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            self._url(path), data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:
            raise WledError(f"{self.host}{path}: {getattr(e, 'reason', e)}") from e
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        return data if isinstance(data, dict) else {}

    def info(self) -> dict[str, Any]:
        return self.get("/json/info")

    def cfg(self) -> dict[str, Any]:
        return self.get("/json/cfg")

    def state(self) -> dict[str, Any]:
        return self.get("/json/state")

    def set_state(self, **state: Any) -> dict[str, Any]:
        return self.post("/json/state", state)
