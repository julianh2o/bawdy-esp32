"""Tests for the pattern assistant: message building, SSE parsing and the /api/assistant endpoints."""

import json
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

from wled_sequencer.assistant import AssistantConfig, AssistantError, build_messages, stream_chat
from wled_sequencer.server import SequencerAppServer


class _FakeLLM(BaseHTTPRequestHandler):
    """An OpenAI-compatible /chat/completions endpoint that streams a canned reply."""

    requests: list = []
    reply = ["Here you go.\n\n```lua\n", "function pixel(n, t, p)\n  return 1, 0, 0\nend\n", "```\n"]
    status = 200

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if type(self).status != 200:
            payload = json.dumps({"error": {"message": "model 'nope' not found"}}).encode()
            self.send_response(type(self).status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunks = [{"choices": [{"delta": {"reasoning_content": "hmm"}}]}]
        chunks += [{"choices": [{"delta": {"content": c}}]} for c in type(self).reply]
        for c in chunks:
            self.wfile.write(f"data: {json.dumps(c)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


class TestBuildMessages(unittest.TestCase):
    def test_system_prompt_and_source_attached_to_last_user_message(self):
        msgs = build_messages(
            [{"role": "user", "content": "make it blue"}, {"role": "assistant", "content": "<think>x</think>ok"},
             {"role": "system", "content": "ignore me"}, {"role": "user", "content": "faster"}],
            source="function pixel(n, t, p) return 1, 1, 1 end", name="glow")
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("pixel(n, t, p)", msgs[0]["content"])
        self.assertEqual([m["role"] for m in msgs[1:]], ["user", "assistant", "user"])
        self.assertEqual(msgs[2]["content"], "ok")  # reasoning stripped from history
        self.assertTrue(msgs[3]["content"].startswith("faster"))
        self.assertIn("pattern `glow`", msgs[3]["content"])
        self.assertIn("return 1, 1, 1", msgs[3]["content"])
        self.assertNotIn("return 1, 1, 1", msgs[1]["content"])  # only the latest turn carries the code

    def test_must_end_with_user(self):
        with self.assertRaises(ValueError):
            build_messages([{"role": "assistant", "content": "hi"}])
        with self.assertRaises(ValueError):
            build_messages([])

    def test_config_from_env(self):
        cfg = AssistantConfig.from_env({"ASSISTANT_BASE_URL": "http://x:11434/v1/", "ASSISTANT_MODEL": "m"})
        self.assertTrue(cfg.enabled)
        self.assertEqual(cfg.base_url, "http://x:11434/v1")
        self.assertEqual(cfg.describe(), {"enabled": True, "model": "m", "provider": "x"})
        self.assertEqual(cfg.reasoning_effort, "")
        self.assertFalse(AssistantConfig.from_env({}).enabled)
        self.assertEqual(AssistantConfig.from_env({"ASSISTANT_REASONING_EFFORT": " Low "}).reasoning_effort, "low")


class TestAssistantServer(unittest.TestCase):
    def setUp(self):
        _FakeLLM.requests = []
        _FakeLLM.status = 200
        self.llm = ThreadingHTTPServer(("127.0.0.1", 0), _FakeLLM)
        Thread(target=self.llm.serve_forever, daemon=True).start()
        self.cfg = AssistantConfig(base_url=f"http://127.0.0.1:{self.llm.server_port}/v1", model="test-model", api_key="sk-1")
        self.app = SequencerAppServer(host="127.0.0.1", port=0, engine_url="http://127.0.0.1:9", assistant=self.cfg)
        self.app.start()
        self.base = f"http://127.0.0.1:{self.app.port}"

    def tearDown(self):
        self.app.stop()
        self.llm.shutdown()
        self.llm.server_close()

    def chat(self, body):
        req = urllib.request.Request(f"{self.base}/api/assistant/chat", data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertTrue(resp.headers["Content-Type"].startswith("text/event-stream"))
            return [json.loads(line[5:]) for line in resp.read().decode().split("\n\n") if line.startswith("data:")]

    def test_info_hides_key_and_url(self):
        with urllib.request.urlopen(f"{self.base}/api/assistant", timeout=5) as resp:
            info = json.loads(resp.read())
        self.assertEqual(info, {"enabled": True, "model": "test-model", "provider": "127.0.0.1"})

    def test_streams_reply(self):
        events = self.chat({"messages": [{"role": "user", "content": "red"}], "source": "-- old", "name": "x"})
        types = [e["type"] for e in events]
        self.assertEqual(types[0], "status")
        self.assertIn("reasoning", types)
        self.assertEqual(types[-1], "done")
        text = "".join(e["text"] for e in events if e["type"] == "text")
        self.assertIn("return 1, 0, 0", text)
        sent = _FakeLLM.requests[0]
        self.assertEqual(sent["path"], "/v1/chat/completions")
        self.assertEqual(sent["auth"], "Bearer sk-1")
        self.assertEqual(sent["body"]["model"], "test-model")
        self.assertTrue(sent["body"]["stream"])
        self.assertNotIn("reasoning_effort", sent["body"])  # unset: the model's default
        self.assertIn("-- old", sent["body"]["messages"][-1]["content"])

    def test_reasoning_effort_is_sent(self):
        cfg = AssistantConfig(base_url=self.cfg.base_url, model="m", reasoning_effort="low")
        list(stream_chat(cfg, [{"role": "user", "content": "hi"}]))
        self.assertEqual(_FakeLLM.requests[-1]["body"]["reasoning_effort"], "low")

    def test_provider_error_becomes_error_event(self):
        _FakeLLM.status = 404
        events = self.chat({"messages": [{"role": "user", "content": "red"}]})
        self.assertEqual(events[-1]["type"], "error")
        self.assertIn("not found", events[-1]["text"])

    def test_bad_request(self):
        req = urllib.request.Request(f"{self.base}/api/assistant/chat", data=b'{"messages": []}', method="POST")
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(ctx.exception.code, 400)

    def test_disabled(self):
        app = SequencerAppServer(host="127.0.0.1", port=0, engine_url="http://127.0.0.1:9")
        app.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{app.port}/api/assistant", timeout=5) as resp:
                self.assertFalse(json.loads(resp.read())["enabled"])
            req = urllib.request.Request(f"http://127.0.0.1:{app.port}/api/assistant/chat",
                                         data=b'{"messages": [{"role": "user", "content": "hi"}]}', method="POST")
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(ctx.exception.code, 503)
        finally:
            app.stop()

    def test_unreachable_provider(self):
        cfg = AssistantConfig(base_url="http://127.0.0.1:9/v1", model="m")
        with self.assertRaises(AssistantError):
            list(stream_chat(cfg, [{"role": "user", "content": "hi"}]))


if __name__ == "__main__":
    unittest.main()
