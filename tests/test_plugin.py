"""Tests of lumen-hello: the backend over plugin/1, as the hub drives it, and the files the hub reads.

Standard library only: `python -m unittest discover -s tests -v`. `evo-agents plugin test .` then runs the
conformance checks a hub relies on (docs/plugins.md of evo-agents, section Writing and checking a plugin).
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = json.loads((ROOT / "evo-plugin.json").read_text(encoding="utf-8"))
ENTRY = json.loads((ROOT / "market-entry.json").read_text(encoding="utf-8"))


class Backend:
    """The backend as a process, spoken to as the hub does: one JSON-RPC request per line."""

    def __init__(self):
        self.process = subprocess.Popen(
            [sys.executable, str(ROOT / MANIFEST["backend"]["entry"])],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=ROOT,
        )
        self.next = 0

    def send(self, line: str) -> dict:
        self.process.stdin.write(line + "\n")
        self.process.stdin.flush()
        return json.loads(self.process.stdout.readline())

    def call(self, method: str, params: dict | None = None) -> dict:
        self.next += 1
        return self.send(json.dumps({"jsonrpc": "2.0", "id": self.next, "method": method, "params": params or {}}))

    def close(self) -> None:
        if self.process.poll() is None:
            self.call("shutdown")
            self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


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


def route(backend: Backend, method: str = "GET", body: dict | None = None, project: str = "northwind") -> dict:
    request = {"method": method, "path": "waves"}
    if body is not None:
        request["body"] = base64.b64encode(json.dumps(body).encode()).decode()
    answer = backend.call("route", {"route": "waves", "request": request, "caller": {"login": "ada", "project": project}})
    result = answer["result"]
    return {"status": result["status"], "body": json.loads(base64.b64decode(result["body"]))}


class BackendTest(unittest.TestCase):
    def setUp(self):
        self.backend = Backend()
        self.addCleanup(self.backend.close)

    def test_initialize_names_plugin_1_and_the_manifest_tools(self):
        result = initialize(self.backend)["result"]
        self.assertEqual(result["protocol"], "plugin/1")
        self.assertEqual(result["plugin"], {"id": MANIFEST["id"], "version": MANIFEST["version"]})
        self.assertEqual([tool["name"] for tool in result["tools"]], ["greet"])

    def test_waves_are_kept_per_project_with_its_greeting(self):
        initialize(self.backend, instance={"greeting": "Hello"}, projects={"lumen": {"greeting": "Xin chào"}})
        self.assertEqual(route(self.backend)["body"], {"project": "northwind", "greeting": "Hello", "waves": []})
        posted = route(self.backend, "POST", {"name": " Ada "})
        self.assertEqual(posted["status"], 201)
        self.assertEqual([wave["name"] for wave in posted["body"]["waves"]], ["Ada"])
        other = route(self.backend, project="lumen")["body"]
        self.assertEqual((other["greeting"], other["waves"]), ("Xin chào", []))

    def test_only_the_last_twenty_waves_stay(self):
        initialize(self.backend)
        for number in range(25):
            route(self.backend, "POST", {"name": f"member {number}"})
        waves = route(self.backend)["body"]["waves"]
        self.assertEqual(len(waves), 20)
        self.assertEqual(waves[0]["name"], "member 24")

    def test_a_wave_without_a_name_is_refused(self):
        initialize(self.backend)
        for body in ({}, {"name": "  "}, {"name": "x" * 81}, {"name": 3}):
            self.assertEqual(route(self.backend, "POST", body)["status"], 422)

    def test_greet_uses_the_project_greeting(self):
        initialize(self.backend, projects={"northwind": {"greeting": "Hi"}})
        route(self.backend, "POST", {"name": "Ada"})
        answer = self.backend.call(
            "tool", {"tool": "greet", "arguments": {"name": "Grace"}, "caller": {"project": "northwind"}}
        )["result"]
        self.assertFalse(answer["is_error"])
        self.assertEqual(answer["content"][0]["text"], "Hi, Grace! The page holds 1 wave(s).")

    def test_unknown_methods_and_bad_lines_keep_it_serving(self):
        initialize(self.backend)
        self.assertEqual(self.backend.call("nothing")["error"]["code"], -32601)
        self.assertEqual(self.backend.send("not json")["error"]["code"], -32700)
        self.assertEqual(self.backend.call("ping")["result"], {})


class FilesTest(unittest.TestCase):
    def test_every_page_entry_and_its_words_exist(self):
        for page in MANIFEST["contributes"]["pages"]:
            entry = ROOT / page["entry"]
            self.assertTrue(entry.is_file(), entry)
            html = entry.read_text(encoding="utf-8")
            self.assertIn("/v1/plugin-ui/kit/1/evo-ui.js", html)
            keys = set(re.findall(r'data-i18n="([a-z_]+)"', html))
            for locale in ("en", "vi"):
                words = (entry.parent / "i18n" / f"{locale}.js").read_text(encoding="utf-8")
                self.assertTrue(words.startswith(f'evo.messages("{locale}"'), locale)
                for key in keys:
                    self.assertRegex(words, rf"\n  {key}: ", f"{locale} lacks {key}")

    def test_the_market_entry_matches_the_manifest(self):
        self.assertEqual(ENTRY["id"], MANIFEST["id"])
        self.assertEqual(ENTRY["publisher"]["id"], MANIFEST["publisher"])
        self.assertEqual(ENTRY["tier"], MANIFEST["tier"])
        self.assertRegex(ENTRY["publisher"]["signer"], r"^sigstore:[^/]+/[^/]+/\.github/workflows/ci\.yml$")
        self.assertIn(f"sigstore:{ENTRY['repo']}/", ENTRY["publisher"]["signer"])


if __name__ == "__main__":
    unittest.main()
