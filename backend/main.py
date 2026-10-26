"""The backend of lumen-hello: plugin/1 on stdin and stdout, standard library only.

The hub starts this program and speaks JSON-RPC 2.0 to it, one JSON object per line: its requests on stdin, the
answers on stdout. Only answers go to stdout; logs go to stderr. The methods (docs/plugins.md of evo-agents, section
The plugin/1 protocol):

- ``initialize``: the plugin's config, of the hub (``instance``) and of each project that enabled it (``projects``);
  the answer names plugin/1 and lists the MCP tools of the manifest;
- ``route``: a call to /v1/x/lumen-hello/<project>/waves, GET or POST;
- ``tool``: a call to the MCP tool lumen-hello__greet;
- ``ping`` and ``shutdown``.

The waves live in memory, the last MAX_WAVES of each project, and are gone when the hub restarts the backend: a
plugin that must keep data stores it through the hub's API or a service of its own.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROTOCOL = "plugin/1"
MANIFEST = Path(__file__).resolve().parent.parent / "evo-plugin.json"
MAX_WAVES = 20
MAX_NAME = 80
DEFAULT_GREETING = "Hello"
INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR = -32602, -32601, -32700


class Refused(Exception):
    """A call the backend refuses, answered as a JSON-RPC error with ``code``."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def tools_of(manifest: dict) -> list[dict]:
    """The MCP tools the manifest contributes, as initialize lists them."""
    return [
        {
            "name": item["name"],
            "description": item.get("description", ""),
            "input_schema": item.get("input_schema", {"type": "object"}),
        }
        for item in manifest.get("contributes", {}).get("mcp_tools", [])
    ]


class Plugin:
    """The plugin's state: its config and each project's waves."""

    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.config: dict = {"instance": {}, "projects": {}}
        self.waves: dict[str, list[dict]] = {}

    def greeting(self, project: str) -> str:
        layer = self.config.get("projects", {}).get(project) or self.config.get("instance", {})
        return str(layer.get("greeting") or DEFAULT_GREETING)

    def initialize(self, params: dict) -> dict:
        self.config = params.get("config") or {"instance": {}, "projects": {}}
        plugin = params.get("plugin") or {}
        print(f"lumen-hello {plugin.get('version')}: serving", file=sys.stderr)
        return {
            "protocol": PROTOCOL,
            "plugin": {"id": self.manifest["id"], "version": self.manifest["version"]},
            "tools": tools_of(self.manifest),
        }

    def route(self, params: dict) -> dict:
        if params.get("route") != "waves":
            raise Refused(INVALID_PARAMS, f"no route {params.get('route')!r}")
        project = str((params.get("caller") or {}).get("project") or "")
        request = params.get("request") or {}
        method = str(request.get("method") or "GET").upper()
        if method == "GET":
            return self.answer(200, self.board(project))
        if method == "POST":
            try:
                body = json.loads(base64.b64decode(request.get("body") or "") or b"{}")
            except ValueError:
                return self.answer(422, {"detail": "the body is not JSON"})
            name = body.get("name") if isinstance(body, dict) else None
            if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME:
                return self.answer(422, {"detail": f"name is a text of 1 to {MAX_NAME} characters"})
            wave = {"name": name.strip(), "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            kept = self.waves.setdefault(project, [])
            kept.insert(0, wave)
            del kept[MAX_WAVES:]
            return self.answer(201, self.board(project))
        return self.answer(405, {"detail": f"{method} is not served here"})

    def board(self, project: str) -> dict:
        return {"project": project, "greeting": self.greeting(project), "waves": self.waves.get(project, [])}

    @staticmethod
    def answer(status: int, body: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(data).decode("ascii"),
        }

    def tool(self, params: dict) -> dict:
        if params.get("tool") != "greet":
            raise Refused(INVALID_PARAMS, f"no tool {params.get('tool')!r}")
        project = str((params.get("caller") or {}).get("project") or "")
        name = (params.get("arguments") or {}).get("name")
        if not isinstance(name, str) or not name.strip():
            return {"content": [{"type": "text", "text": "name is required"}], "is_error": True}
        count = len(self.waves.get(project, []))
        text = f"{self.greeting(project)}, {name.strip()}! The page holds {count} wave(s)."
        return {"content": [{"type": "text", "text": text}], "is_error": False}

    def handle(self, method: str, params: dict) -> dict:
        handlers = {
            "initialize": self.initialize,
            "ping": lambda _params: {},
            "shutdown": lambda _params: {},
            "route": self.route,
            "tool": self.tool,
        }
        if method not in handlers:
            raise Refused(METHOD_NOT_FOUND, f"no method {method!r}")
        return handlers[method](params)


def reply(message_id, result=None, error=None) -> None:
    message = {"jsonrpc": "2.0", "id": message_id}
    message.update({"error": error} if error is not None else {"result": result})
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def serve(plugin: Plugin, lines) -> None:
    for line in lines:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except ValueError:
            reply(None, error={"code": PARSE_ERROR, "message": "not JSON"})
            continue
        if not isinstance(message, dict):
            reply(None, error={"code": -32600, "message": "not a request"})
            continue
        method, message_id = message.get("method"), message.get("id")
        try:
            reply(message_id, plugin.handle(str(method), message.get("params") or {}))
        except Refused as exc:
            reply(message_id, error={"code": exc.code, "message": str(exc)})
        except Exception as exc:  # the backend's own failure; it keeps serving
            print(f"lumen-hello: {method} failed: {exc!r}", file=sys.stderr)
            reply(message_id, error={"code": -32603, "message": "internal error"})
        if method == "shutdown":
            return


def main() -> None:
    serve(Plugin(json.loads(MANIFEST.read_text(encoding="utf-8"))), sys.stdin)


if __name__ == "__main__":
    main()
