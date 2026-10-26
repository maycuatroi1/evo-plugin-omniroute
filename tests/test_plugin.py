"""Tests of omniroute: the backend over plugin/1, as the hub drives it, against a fake OmniRoute, and the files the hub
reads.

Standard library only: `python -m unittest discover -s tests -v`. `evo-agents plugin test .` then runs the
conformance checks a hub relies on (docs/plugins.md of evo-agents, section Writing and checking a plugin).

The fake gateway listens on plain http at 127.0.0.1, which the backend refuses unless EVO_OMNIROUTE_ALLOW_LOCAL=1;
the tests set it for the backend they start, and one test starts a backend without it to see the refusal.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "evo-plugin.json").read_text(encoding="utf-8"))
ENTRY = json.loads((ROOT / "market-entry.json").read_text(encoding="utf-8"))
KEY = "sk-northwind-test-key-0001"
USAGE_KEY = "sk-northwind-usage-key-0002"

MODELS = [
    {"id": "auto", "owned_by": "combo", "context_length": None, "capabilities": {}},
    {"id": "gemini/gemini-flash", "owned_by": "gemini", "context_length": 1048576,
     "capabilities": {"vision": True, "tool_calling": True, "reasoning": True}},
    {"id": "glm/glm-lite", "owned_by": "glm", "context_length": 128000, "capabilities": {"tool_calling": True}},
    {"id": "slow/model", "owned_by": "slow", "context_length": 8000, "capabilities": {}},
]


class FakeGateway(BaseHTTPRequestHandler):
    """The few routes of OmniRoute the plugin calls, with its auth and error shapes."""

    seen: list[dict] = []

    def log_message(self, *_args):
        pass

    def reply(self, status: int, body) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def key(self) -> str:
        return self.headers.get("Authorization", "").removeprefix("Bearer ")

    def do_GET(self):
        url = urlsplit(self.path)
        query = {name: values[0] for name, values in parse_qs(url.query).items()}
        FakeGateway.seen.append({"method": "GET", "path": url.path, "query": query, "host": self.headers.get("Host")})
        if self.key() not in (KEY, USAGE_KEY):
            return self.reply(401, {"error": {"code": "AUTH_002", "message": "Authentication required"}})
        if url.path == "/v1/models":
            rows = MODELS
            if query.get("q"):
                rows = [row for row in rows if query["q"] in row["id"]]
            return self.reply(200, {"object": "list", "data": rows})
        if url.path == "/api/usage/om-usage":
            if self.key() != USAGE_KEY:
                return self.reply(403, {"allowed": False, "error": {"message": "usage command is off for this key"}})
            return self.reply(200, {
                "allowed": True,
                "personal": {"dailySpentUsd": 0.5, "dailyLimitUsd": 5, "weeklySpentUsd": 2, "weeklyLimitUsd": 20},
                "provider": None,
                "providers": [{"connectionId": "c1", "provider": "gemini", "plan": "free", "quotas": {}}],
            })
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlsplit(self.path)
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        FakeGateway.seen.append({"method": "POST", "path": url.path, "body": body})
        if self.key() not in (KEY, USAGE_KEY):
            return self.reply(401, {"error": {"code": "AUTH_002", "message": "Authentication required"}})
        if url.path != "/v1/chat/completions":
            return self.reply(404, {"error": "not found"})
        model = body.get("model")
        if model == "missing/model":
            return self.reply(502, {"error": {"message": "no connection serves missing/model", "code": "bad_gateway"}})
        if model == "slow/model":
            time.sleep(1.5)
        question = body["messages"][-1]["content"]
        served = "gemini-flash" if model == "auto" else model
        return self.reply(200, {
            "model": served,
            "choices": [{"message": {"role": "assistant", "content": f"echo: {question}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        })


class Gateway:
    """The fake gateway in a thread, at http://127.0.0.1:<port>."""

    def __init__(self):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class Backend:
    """The backend as a process, spoken to as the hub does: one JSON-RPC request per line."""

    def __init__(self, allow_local: bool = True):
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
        if allow_local:
            env["EVO_OMNIROUTE_ALLOW_LOCAL"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / MANIFEST["backend"]["entry"])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
            env=env,
        )
        self.next = 0
        self.lines: list[str] = []

    def write(self, payload: dict) -> int:
        self.next += 1
        payload = {"jsonrpc": "2.0", "id": self.next, **payload}
        self.process.stdin.write(json.dumps(payload) + "\n")
        self.process.stdin.flush()
        return self.next

    def read(self) -> dict:
        line = self.process.stdout.readline()
        self.lines.append(line)
        return json.loads(line)

    def send(self, line: str) -> dict:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        return self.read()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.write({"method": method, "params": params or {}})
        return self.read()

    def close(self) -> str:
        if self.process.poll() is None:
            self.call("shutdown")
            self.process.wait(timeout=5)
        stderr = self.process.stderr.read()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()
        return stderr


def initialize(backend: Backend, instance: dict | None = None, projects: dict | None = None) -> dict:
    return backend.call(
        "initialize",
        {
            "protocol": "plugin/1",
            "plugin": {"id": MANIFEST["id"], "version": MANIFEST["version"]},
            "hub": {"url": "http://hub.test", "token": "test-token", "version": "0.47.3"},
            "config": {"instance": instance or {}, "projects": projects or {}},
            "grants": {},
        },
    )


def route(backend: Backend, name: str, method: str = "GET", body: dict | None = None, query: dict | None = None,
          project: str = "northwind", role: str = "writer") -> dict:
    request = {"method": method, "path": "/", "query": query or {}}
    if body is not None:
        request["body"] = base64.b64encode(json.dumps(body).encode()).decode()
    caller = {"login": "ada", "project": project, "role": role}
    result = backend.call("route", {"route": name, "request": request, "caller": caller})["result"]
    return {"status": result["status"], "body": json.loads(base64.b64decode(result["body"]))}


def tool(backend: Backend, name: str, arguments: dict | None = None, project: str = "northwind") -> dict:
    caller = {"login": "agent-lumen", "project": project}
    return backend.call("tool", {"tool": name, "arguments": arguments or {}, "caller": caller})["result"]


class BackendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.gateway = Gateway()

    @classmethod
    def tearDownClass(cls):
        cls.gateway.close()

    def setUp(self):
        FakeGateway.seen.clear()
        self.backend = Backend()
        self.addCleanup(self.close)

    def close(self):
        stderr = self.backend.close()
        for secret in (KEY, USAGE_KEY):
            self.assertNotIn(secret, stderr)
            self.assertNotIn(secret, "".join(self.backend.lines))

    def connect(self, key: str = KEY, **project):
        instance = {"base_url": self.gateway.url + "/", "api_key": key}
        return initialize(self.backend, instance=instance, projects={"northwind": project} if project else {})

    def test_initialize_names_plugin_1_and_the_manifest_tools(self):
        result = initialize(self.backend)["result"]
        self.assertEqual(result["protocol"], "plugin/1")
        self.assertEqual(result["plugin"], {"id": MANIFEST["id"], "version": MANIFEST["version"]})
        self.assertEqual([item["name"] for item in result["tools"]], ["models", "ask", "usage"])

    def test_a_project_without_a_gateway_is_told_how_to_set_one(self):
        initialize(self.backend, instance={"base_url": "https://omniroute.example.com"})
        status = route(self.backend, "status")
        self.assertEqual(status["status"], 200)
        self.assertFalse(status["body"]["configured"])
        self.assertEqual(route(self.backend, "models")["status"], 409)
        answer = tool(self.backend, "ask", {"prompt": "hi"})
        self.assertTrue(answer["is_error"])
        self.assertIn("plugin config omniroute --project northwind", answer["content"][0]["text"])
        self.assertEqual(FakeGateway.seen, [])

    def test_status_reads_the_gateway_the_key_and_its_usage(self):
        self.connect(USAGE_KEY)
        status = route(self.backend, "status")["body"]
        self.assertTrue(status["configured"] and status["reachable"] and status["authorized"])
        self.assertEqual((status["models"], status["default_model"], status["base_url"]), (4, "auto", self.gateway.url))
        self.assertIsInstance(status["latency_ms"], int)
        self.assertTrue(status["usage"]["allowed"])
        self.assertEqual(status["usage"]["providers"][0]["provider"], "gemini")
        self.assertTrue(status["can_ask"])
        self.assertFalse(route(self.backend, "status", role="reader")["body"]["can_ask"])
        text = route(self.backend, "status", query={"render": "text"})["body"]["text"]
        self.assertIn("ok", text)
        self.assertIn("today $0.50 of $5.00", text)

    def test_status_says_when_the_key_may_not_read_its_usage(self):
        self.connect(KEY)
        usage = route(self.backend, "status")["body"]["usage"]
        self.assertEqual(usage["allowed"], False)
        self.assertIn("usage command", tool(self.backend, "usage")["content"][0]["text"])

    def test_a_wrong_key_is_a_refusal_not_a_crash(self):
        self.connect("sk-wrong")
        status = route(self.backend, "status")["body"]
        self.assertTrue(status["reachable"])
        self.assertFalse(status["authorized"])
        self.assertIn("Authentication required", status["error"])
        refused = route(self.backend, "models")
        self.assertEqual((refused["status"], refused["body"]["gateway_status"]), (502, 401))

    def test_models_filter_by_word_and_capability(self):
        self.connect()
        listed = route(self.backend, "models", query={"q": "gemini"})["body"]
        self.assertEqual([row["id"] for row in listed["models"]], ["gemini/gemini-flash"])
        self.assertEqual(listed["models"][0]["capabilities"], ["vision", "tool_calling", "reasoning"])
        tools = route(self.backend, "models", query={"capability": "tool_calling", "limit": "1"})["body"]
        self.assertEqual((tools["total"], len(tools["models"])), (2, 1))
        self.assertEqual(route(self.backend, "models", query={"capability": "flying"})["status"], 422)
        text = tool(self.backend, "models", {"query": "glm"})["content"][0]["text"]
        self.assertIn("glm/glm-lite", text)
        self.assertIn("128k", text)
        self.assertIn("1M", tool(self.backend, "models", {"query": "gemini"})["content"][0]["text"])

    def test_ask_uses_the_project_default_model_and_its_token_ceiling(self):
        self.connect(default_model="glm/glm-lite", max_tokens=200)
        asked = route(self.backend, "ask", "POST", {"prompt": "ping", "system": "be brief", "max_tokens": 5000})
        self.assertEqual(asked["status"], 200)
        self.assertEqual((asked["body"]["model"], asked["body"]["content"]), ("glm/glm-lite", "echo: ping"))
        sent = [item for item in FakeGateway.seen if item["method"] == "POST"][-1]["body"]
        self.assertEqual(sent["max_tokens"], 200)
        self.assertEqual(sent["messages"][0], {"role": "system", "content": "be brief"})
        self.assertFalse(sent["stream"])
        answer = tool(self.backend, "ask", {"prompt": "pong", "model": "auto"})
        self.assertFalse(answer["is_error"])
        self.assertIn("echo: pong", answer["content"][0]["text"])
        self.assertIn("[gemini-flash, 7 in, 3 out,", answer["content"][0]["text"])

    def test_ask_takes_the_role_the_project_set(self):
        self.connect()
        self.assertEqual(route(self.backend, "ask", "POST", {"prompt": "x"}, role="reader")["status"], 403)
        self.assertEqual(route(self.backend, "ask", "GET")["status"], 405)
        self.assertEqual(route(self.backend, "ask", "POST", {"prompt": "  "})["status"], 422)
        self.backend.close()
        self.backend = Backend()
        self.connect(ask_role="reader")
        self.assertEqual(route(self.backend, "ask", "POST", {"prompt": "x"}, role="reader")["status"], 200)

    def test_the_gateway_s_own_error_reaches_the_caller(self):
        self.connect()
        refused = route(self.backend, "ask", "POST", {"prompt": "x", "model": "missing/model"})
        self.assertEqual(refused["status"], 502)
        self.assertIn("no connection serves missing/model", refused["body"]["detail"])
        self.assertTrue(tool(self.backend, "ask", {"prompt": "x", "model": "missing/model"})["is_error"])

    def test_a_slow_question_does_not_hold_the_other_calls(self):
        self.connect()
        slow = self.backend.write({"method": "tool", "params": {
            "tool": "ask", "arguments": {"prompt": "x", "model": "slow/model"}, "caller": {"project": "northwind"}}})
        ping = self.backend.write({"method": "ping", "params": {}})
        first, second = self.backend.read(), self.backend.read()
        self.assertEqual((first["id"], second["id"]), (ping, slow))
        self.assertFalse(second["result"]["is_error"])

    def test_unknown_methods_and_bad_lines_keep_it_serving(self):
        initialize(self.backend)
        self.assertEqual(self.backend.call("nothing")["error"]["code"], -32601)
        self.assertEqual(self.backend.send("not json")["error"]["code"], -32700)
        self.assertEqual(self.backend.call("route", {"route": "nope", "request": {"method": "GET", "path": "/"},
                                                     "caller": {"project": "northwind"}})["error"]["code"], -32602)
        self.assertEqual(self.backend.call("ping")["result"], {})


class AddressTest(unittest.TestCase):
    """Without the test switch, the backend reaches only https on public addresses."""

    def setUp(self):
        self.backend = Backend(allow_local=False)
        self.addCleanup(self.backend.close)

    def status_of(self, url: str) -> dict:
        initialize(self.backend, instance={"base_url": url, "api_key": KEY})
        return route(self.backend, "status")["body"]

    def test_plain_http_is_refused(self):
        self.assertIn("must start with https://", self.status_of("http://omniroute.example.com")["error"])

    def test_a_private_or_loopback_address_is_refused(self):
        for url in ("https://127.0.0.1:9", "https://10.0.0.8", "https://[::1]:9", "https://169.254.169.254"):
            with self.subTest(url=url):
                self.assertIn("not a public address", self.status_of(url)["error"])

    def test_a_url_with_a_login_is_refused(self):
        self.assertIn("carries a login", self.status_of("https://ada:secret@omniroute.example.com")["error"])


class FilesTest(unittest.TestCase):
    def test_every_page_entry_and_its_words_exist(self):
        for page in MANIFEST["contributes"]["pages"]:
            entry = ROOT / page["entry"]
            self.assertTrue(entry.is_file(), entry)
            html = entry.read_text(encoding="utf-8")
            self.assertIn("/v1/plugin-ui/kit/1/evo-ui.js", html)
            script = (entry.parent / "gateway.js").read_text(encoding="utf-8")
            keys = set(re.findall(r'data-i18n="([a-z_]+)"', html)) | set(re.findall(r'evo\.t\("([a-z_]+)"', script))
            for locale in ("en", "vi"):
                words = (entry.parent / "i18n" / f"{locale}.js").read_text(encoding="utf-8")
                self.assertTrue(words.startswith(f'evo.messages("{locale}"'), locale)
                for key in keys:
                    self.assertRegex(words, rf"\n  {key}: ", f"{locale} lacks {key}")

    def test_the_cli_commands_call_routes_the_manifest_declares(self):
        routes = {item["name"]: item.get("methods", ["GET"]) for item in MANIFEST["contributes"]["routes"]}
        for command in MANIFEST["contributes"]["cli"]:
            self.assertIn(command["http"]["method"], routes[command["http"]["path"]], command["name"])

    def test_the_market_entry_matches_the_manifest(self):
        self.assertEqual(ENTRY["id"], MANIFEST["id"])
        self.assertEqual(ENTRY["publisher"]["id"], MANIFEST["publisher"])
        self.assertEqual(ENTRY["tier"], MANIFEST["tier"])
        self.assertRegex(ENTRY["publisher"]["signer"], r"^sigstore:[^/]+/[^/]+/\.github/workflows/ci\.yml$")
        self.assertIn(f"sigstore:{ENTRY['repo']}/", ENTRY["publisher"]["signer"])
        self.assertLessEqual(len(ENTRY["description"]), 280)

    def test_the_key_field_is_secret(self):
        self.assertTrue(MANIFEST["config"]["properties"]["api_key"]["x-secret"])


if __name__ == "__main__":
    unittest.main()
