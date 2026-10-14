"""Pattern assistant: an LLM chat that writes and edits Lua patterns, for the editor's chat panel.

Talks to any OpenAI-compatible chat-completions API, so the same code works with a local Ollama server
(``http://<host>:11434/v1``), DeepSeek's API (``https://api.deepseek.com``), or any other compatible provider.
Configured with environment variables (or the matching ``sequencer.py`` flags):

    ASSISTANT_BASE_URL   API root, e.g. http://10.10.0.40:11434/v1 (unset: the assistant is off)
    ASSISTANT_MODEL      model name, e.g. qwen3-coder:30b or deepseek-chat
    ASSISTANT_API_KEY    bearer token, if the provider needs one (Ollama doesn't)
    ASSISTANT_TIMEOUT    seconds a whole reply may take before it's cut off (default 600)
    ASSISTANT_REASONING_EFFORT  how hard a thinking model thinks: none, low, medium or high; unset = the model's
                         default. Sent as ``reasoning_effort``, which Ollama and DeepSeek both accept.

The browser never sees the key or the provider URL: it posts the conversation and the editor's code to
``/api/assistant/chat`` and gets the reply streamed back as server-sent events.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import urlparse

PROMPT_FILE = Path(__file__).resolve().parent / "assistant_prompt.md"

MAX_MESSAGES = 40  # conversation turns kept per request (the oldest are dropped)
MAX_MESSAGE_CHARS = 20_000  # per message, to keep a runaway paste from blowing the context
MAX_SOURCE_CHARS = 30_000
CONNECT_TIMEOUT = 600.0  # a local model can take minutes to load into VRAM before the first token

_THINK_RE = re.compile(r"<think>.*?(</think>|$)", re.S)


@dataclass(frozen=True)
class AssistantConfig:
    base_url: str = ""
    model: str = ""
    api_key: str = ""
    temperature: float = 0.3
    timeout: float = 600.0  # whole reply, including thinking; stops a model stuck in a reasoning loop
    reasoning_effort: str = ""  # "" leaves the model's default

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> AssistantConfig:
        env: Mapping[str, str] = os.environ if environ is None else environ
        return cls(
            base_url=(env.get("ASSISTANT_BASE_URL") or "").strip().rstrip("/"),
            model=(env.get("ASSISTANT_MODEL") or "").strip(),
            api_key=(env.get("ASSISTANT_API_KEY") or "").strip(),
            temperature=float(env.get("ASSISTANT_TEMPERATURE") or 0.3),
            timeout=float(env.get("ASSISTANT_TIMEOUT") or 600),
            reasoning_effort=(env.get("ASSISTANT_REASONING_EFFORT") or "").strip().lower(),
        )

    def describe(self) -> dict[str, Any]:
        """What the browser may know: whether it's on, the model and the provider's host (no key, no path)."""
        return {
            "enabled": self.enabled,
            "model": self.model,
            "provider": (urlparse(self.base_url).hostname or "") if self.base_url else "",
        }


class AssistantError(Exception):
    """The provider couldn't be reached or returned an error."""


def system_prompt() -> str:
    return PROMPT_FILE.read_text(encoding="utf-8")


def build_messages(history: list[dict[str, Any]], source: str = "", name: str = "") -> list[dict[str, str]]:
    """System prompt + the conversation, with the editor's current code attached to the last user message.

    ``history`` is the browser's transcript: ``[{"role": "user"|"assistant", "content": str}, ...]`` ending with a
    user message. Other roles and empty messages are dropped; long ones are cut.
    """
    turns: list[dict[str, str]] = []
    for m in history[-MAX_MESSAGES:]:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            continue
        content = str(m.get("content") or "").strip()
        if not content:
            continue
        if m["role"] == "assistant":
            content = _THINK_RE.sub("", content).strip()  # don't feed reasoning back in
        turns.append({"role": m["role"], "content": content[:MAX_MESSAGE_CHARS]})
    if not turns or turns[-1]["role"] != "user":
        raise ValueError("The conversation must end with a user message.")

    if source.strip():
        label = f"pattern `{name}`" if name else "an unsaved pattern"
        code = source[:MAX_SOURCE_CHARS]
        turns[-1] = {
            "role": "user",
            "content": f"{turns[-1]['content']}\n\n---\nThe editor currently holds {label}:\n```lua\n{code}\n```",
        }
    return [{"role": "system", "content": system_prompt()}, *turns]


def _parse_sse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if data == "[DONE]":
        return {"done": True}
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def stream_chat(config: AssistantConfig, messages: list[dict[str, str]]) -> Iterator[dict[str, str]]:
    """Yield ``{"type": "text"|"reasoning", "text": ...}`` chunks from a streaming chat completion.

    Reasoning models send their thinking separately (``reasoning_content`` on DeepSeek, ``reasoning`` on Ollama);
    it's passed on as "reasoning" so the UI can show progress without mixing it into the answer.
    """
    if not config.enabled:
        raise AssistantError("The assistant isn't configured (set ASSISTANT_BASE_URL and ASSISTANT_MODEL).")
    payload: dict[str, Any] = {
        "model": config.model,
        "messages": messages,
        "stream": True,
        "temperature": config.temperature,
    }
    if config.reasoning_effort:
        payload["reasoning_effort"] = config.reasoning_effort
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{config.base_url}/chat/completions", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    deadline = time.monotonic() + config.timeout
    if config.api_key:
        req.add_header("Authorization", f"Bearer {config.api_key}")
    try:
        resp = urllib.request.urlopen(req, timeout=min(CONNECT_TIMEOUT, config.timeout))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500]
        try:
            detail = json.loads(detail).get("error", {}).get("message") or detail
        except (ValueError, AttributeError):
            pass
        raise AssistantError(f"{config.model}: HTTP {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = getattr(e, "reason", e)
        raise AssistantError(f"Can't reach the model at {urlparse(config.base_url).hostname}: {reason}") from e

    with resp:
        for raw in resp:
            if time.monotonic() > deadline:
                raise AssistantError(f"{config.model} took longer than {config.timeout:.0f} s; stopped.")
            event = _parse_sse_line(raw.decode("utf-8", "replace"))
            if event is None:
                continue
            if event.get("done"):
                return
            if event.get("error"):
                err = event["error"]
                raise AssistantError(err.get("message") if isinstance(err, dict) else str(err))
            for choice in event.get("choices") or []:
                delta = choice.get("delta") or {}
                thinking = delta.get("reasoning_content") or delta.get("reasoning")
                if thinking:
                    yield {"type": "reasoning", "text": thinking}
                if delta.get("content"):
                    yield {"type": "text", "text": delta["content"]}
