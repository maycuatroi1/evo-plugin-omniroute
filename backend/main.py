"""The backend of omniroute: plugin/1 on stdin and stdout, standard library only.

The hub starts this program and speaks JSON-RPC 2.0 to it, one JSON object per line: its requests on stdin, the
answers on stdout. Only answers go to stdout; logs go to stderr, and never a key. The methods (docs/plugins.md of
evo-agents, section The plugin/1 protocol):

- ``initialize``: the plugin's config, of the hub (``instance``) and of each project that enabled it (``projects``);
  the answer names plugin/1 and lists the MCP tools of the manifest;
- ``route``: a call to /v1/x/omniroute/<project>/status, models (GET) or ask (POST);
- ``tool``: a call to the MCP tools omniroute__models, omniroute__ask and omniroute__usage;
- ``ping`` and ``shutdown``.

Every call goes to the project's OmniRoute gateway, the ``base_url`` of its config, with its ``api_key``. A project's
admin writes that config, so the backend treats the URL as untrusted: only https, and only a host whose every address
is public, checked at each call and pinned for the connection, so the plugin never reaches the hub's own network.

A question can take a model many seconds, and the hub gives one call 30, so each call runs in a thread of its own and
the answers go back in the order they are ready, which plugin/1 allows.
"""

from __future__ import annotations

import base64
import http.client
import ipaddress
import json
import os
import socket
import ssl
import sys
import threading
import time
import urllib.parse
from pathlib import Path

PROTOCOL = "plugin/1"
MANIFEST = Path(__file__).resolve().parent.parent / "evo-plugin.json"
GATEWAY_TIMEOUT = 25.0  # the hub waits 30 seconds for a call to a plugin
MAX_RESPONSE = 8 * 1024 * 1024
MAX_WORKERS = 8
ROLES = ("reader", "writer", "admin")
CAPABILITIES = ("vision", "tool_calling", "reasoning")
USER_AGENT = "evo-plugin-omniroute"
INVALID_PARAMS, METHOD_NOT_FOUND, PARSE_ERROR, INVALID_REQUEST, INTERNAL = -32602, -32601, -32700, -32600, -32603

# Tests only: lets the backend reach a gateway on plain http at a loopback address. The plugin host builds a backend's
# environment from nothing, so no hub sets it.
ALLOW_LOCAL = os.environ.get("EVO_OMNIROUTE_ALLOW_LOCAL") == "1"


class Refused(Exception):
    """A call the backend refuses, answered as a JSON-RPC error with ``code``."""

    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class GatewayError(Exception):
    """The gateway could not be reached or refused the call; ``status`` is its HTTP status, 0 when unreached."""

    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


class NotConfigured(Exception):
    """The project has no gateway URL or no key."""


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


def defaults_of(manifest: dict) -> dict:
    """The defaults of the config schema, the lowest layer of a project's settings."""
    properties = (manifest.get("config") or {}).get("properties") or {}
    return {name: field["default"] for name, field in properties.items() if "default" in field}


def public_addresses(host: str, port: int) -> list[str]:
    """The addresses of ``host``, refused unless every one of them is public."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise GatewayError(f"cannot resolve {host}: {exc.strerror or exc}") from None
    addresses = list(dict.fromkeys(info[4][0] for info in infos))
    if not addresses:
        raise GatewayError(f"{host} has no address")
    if not ALLOW_LOCAL:
        for address in addresses:
            if not ipaddress.ip_address(address.split("%")[0]).is_global:
                raise GatewayError(f"{host} resolves to {address}, which is not a public address")
    return addresses


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS to ``host`` at an address already checked, with the host in SNI and the certificate check."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self) -> None:
        sock = socket.create_connection((self.address, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedHTTPConnection(http.client.HTTPConnection):
    """Plain HTTP at an address already checked; only with ALLOW_LOCAL, for tests."""

    def __init__(self, host: str, port: int, address: str, timeout: float):
        super().__init__(host, port, timeout=timeout)
        self.address = address

    def connect(self) -> None:
        self.sock = socket.create_connection((self.address, self.port), self.timeout)


def error_message(payload, fallback: str) -> str:
    """The words of an OmniRoute error answer: {"error": {"message"}}, {"error": "..."} or {"detail": "..."}."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:500]
        if isinstance(error, str) and error:
            return error[:500]
        if payload.get("detail"):
            return str(payload["detail"])[:500]
    return fallback


class Gateway:
    """One project's OmniRoute: its root URL and its key."""

    def __init__(self, base_url: str, api_key: str):
        parsed = urllib.parse.urlsplit(base_url.strip())
        if parsed.scheme != "https" and not (ALLOW_LOCAL and parsed.scheme == "http"):
            raise GatewayError("the gateway URL must start with https://")
        if not parsed.hostname or parsed.username or parsed.password:
            raise GatewayError("the gateway URL names no host, or carries a login")
        self.scheme = parsed.scheme
        self.host = parsed.hostname
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.root = parsed.path.rstrip("/")
        self.base_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, self.root, "", ""))
        self.api_key = api_key

    def call(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        """``method`` ``path`` on the gateway; answers its JSON, or raises GatewayError."""
        address = public_addresses(self.host, self.port)[0]
        kind = PinnedHTTPSConnection if self.scheme == "https" else PinnedHTTPConnection
        connection = kind(self.host, self.port, address, GATEWAY_TIMEOUT)
        target = self.root + path
        if query:
            target += "?" + urllib.parse.urlencode({k: v for k, v in query.items() if v not in (None, "")})
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, target, body=data, headers=headers)
            response = connection.getresponse()
            raw = response.read(MAX_RESPONSE + 1)
        except (OSError, http.client.HTTPException) as exc:
            reason = "timed out" if isinstance(exc, (socket.timeout, TimeoutError)) else str(exc) or type(exc).__name__
            raise GatewayError(f"cannot reach {self.host}: {reason}") from None
        finally:
            connection.close()
        if len(raw) > MAX_RESPONSE:
            raise GatewayError("the gateway answered more than 8 MiB", response.status)
        try:
            payload = json.loads(raw) if raw else None
        except ValueError:
            payload = None
        if response.status >= 400:
            raise GatewayError(error_message(payload, f"the gateway answered {response.status}"), response.status)
        if payload is None:
            raise GatewayError(f"the gateway answered {response.status} without JSON", response.status)
        return payload


def model_row(item: dict) -> dict:
    capabilities = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
    return {
        "id": str(item.get("id", "")),
        "name": item.get("name"),
        "owned_by": item.get("owned_by"),
        "context_length": item.get("context_length"),
        "capabilities": [name for name in CAPABILITIES if capabilities.get(name) is True],
    }


def message_text(message: dict) -> str:
    """The text of a chat completion's message, whose content is a string or a list of parts."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


def compact(number) -> str:
    if not isinstance(number, (int, float)):
        return "-"
    for size, unit in ((1_000_000, "M"), (1000, "k")):
        if number >= size:
            return f"{number / size:.1f}".removesuffix(".0") + unit
    return str(number)


def money(value) -> str:
    return f"${value:.2f}" if isinstance(value, (int, float)) else "-"


class Plugin:
    """The plugin's state: its config, read at initialize."""

    def __init__(self, manifest: dict):
        self.manifest = manifest
        self.defaults = defaults_of(manifest)
        self.config: dict = {"instance": {}, "projects": {}}

    def initialize(self, params: dict) -> dict:
        self.config = params.get("config") or {"instance": {}, "projects": {}}
        plugin = params.get("plugin") or {}
        print(f"omniroute {plugin.get('version')}: serving {len(self.config.get('projects') or {})} project(s)",
              file=sys.stderr)
        return {
            "protocol": PROTOCOL,
            "plugin": {"id": self.manifest["id"], "version": self.manifest["version"]},
            "tools": tools_of(self.manifest),
        }

    def settings(self, project: str) -> dict:
        """The project's settings: the schema's defaults, then the hub's layer, then the project's, field by field."""
        merged = dict(self.defaults)
        for layer in (self.config.get("instance") or {}, (self.config.get("projects") or {}).get(project) or {}):
            merged.update({key: value for key, value in layer.items() if value not in (None, "")})
        return merged

    def gateway(self, project: str) -> Gateway:
        settings = self.settings(project)
        if not settings.get("base_url") or not settings.get("api_key"):
            raise NotConfigured(
                "OmniRoute is not set up for this project: an admin sets base_url and api_key "
                f"(evo-agents hub plugin config omniroute --project {project} ...)"
            )
        return Gateway(str(settings["base_url"]), str(settings["api_key"]))

    # What the gateway answers, shaped for a route, a tool and the CLI.

    def status(self, project: str) -> dict:
        settings = self.settings(project)
        answer = {
            "project": project,
            "configured": bool(settings.get("base_url") and settings.get("api_key")),
            "base_url": settings.get("base_url"),
            "default_model": settings.get("default_model"),
            "reachable": False,
            "authorized": False,
            "latency_ms": None,
            "models": None,
            "usage": None,
            "error": None,
        }
        if not answer["configured"]:
            answer["error"] = "base_url and api_key are not both set"
            return answer
        try:
            gateway = self.gateway(project)
            answer["base_url"] = gateway.base_url
            started = time.monotonic()
            listed = gateway.call("GET", "/v1/models")
            answer["latency_ms"] = round((time.monotonic() - started) * 1000)
            answer["reachable"] = answer["authorized"] = True
            answer["models"] = len(listed.get("data") or []) if isinstance(listed, dict) else 0
        except GatewayError as exc:
            answer["reachable"] = exc.status != 0
            answer["error"] = str(exc)
            return answer
        try:
            answer["usage"] = self.usage(project)
        except GatewayError as exc:
            answer["usage"] = {"allowed": False, "error": str(exc)}
        return answer

    def models(self, project: str, query: str = "", capability: str = "", limit: int = 100) -> dict:
        if capability and capability not in CAPABILITIES:
            raise ValueError(f"capability is one of {', '.join(CAPABILITIES)}")
        limit = max(1, min(int(limit or 100), 500))
        listed = self.gateway(project).call(
            "GET", "/v1/models", {"q": query or None, "capability": capability or None}
        )
        rows = [model_row(item) for item in (listed.get("data") or []) if isinstance(item, dict)]
        word = (query or "").strip().lower()
        if word:  # a gateway that ignores q still answers only what was asked for
            rows = [row for row in rows if word in row["id"].lower() or word in str(row["name"] or "").lower()]
        if capability:
            rows = [row for row in rows if capability in row["capabilities"]]
        return {"project": project, "total": len(rows), "models": rows[:limit]}

    def ask(self, project: str, prompt: str, model: str = "", system: str = "", max_tokens=None) -> dict:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt is a text of at least one character")
        settings = self.settings(project)
        gateway = self.gateway(project)
        chosen = (model or "").strip() or str(settings.get("default_model") or "auto")
        ceiling = int(settings.get("max_tokens") or 1024)
        tokens = ceiling if max_tokens in (None, "") else max(16, min(int(max_tokens), ceiling))
        messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
        started = time.monotonic()
        answer = gateway.call(
            "POST", "/v1/chat/completions",
            body={"model": chosen, "messages": messages, "max_tokens": tokens, "stream": False},
        )
        latency = round((time.monotonic() - started) * 1000)
        choice = ((answer.get("choices") or [{}])[0]) if isinstance(answer, dict) else {}
        return {
            "project": project,
            "model": answer.get("model") or chosen,
            "asked_model": chosen,
            "content": message_text(choice.get("message") or {}),
            "finish_reason": choice.get("finish_reason"),
            "usage": answer.get("usage"),
            "latency_ms": latency,
        }

    def usage(self, project: str) -> dict:
        """The key's own usage, from /api/usage/om-usage; {"allowed": false, "error"} when the key may not read it."""
        try:
            answer = self.gateway(project).call("GET", "/api/usage/om-usage", {"format": "json"})
        except GatewayError as exc:
            if exc.status in (401, 403):
                return {"allowed": False, "error": str(exc)}
            raise
        if not isinstance(answer, dict):
            return {"allowed": False, "error": "the gateway answered no usage"}
        providers = []
        for item in answer.get("providers") or []:
            if isinstance(item, dict):
                providers.append({key: item.get(key) for key in ("provider", "connectionId", "plan", "quotas")})
        return {"allowed": bool(answer.get("allowed")), "personal": answer.get("personal"), "providers": providers}

    # Words, for render=text and the MCP tools.

    @staticmethod
    def status_text(status: dict) -> str:
        if not status["configured"]:
            return f"OmniRoute is not set up for {status['project']}: an admin sets base_url and api_key."
        lines = [f"OmniRoute {status['base_url']} for {status['project']}"]
        if status["error"]:
            lines.append(f"  state:   {'refused' if status['reachable'] else 'unreachable'}: {status['error']}")
            return "\n".join(lines)
        lines.append(f"  state:   ok, {status['latency_ms']} ms, {status['models']} models")
        lines.append(f"  default: {status['default_model']}")
        lines.append("  usage:   " + Plugin.usage_line(status.get("usage") or {}))
        return "\n".join(lines)

    @staticmethod
    def usage_line(usage: dict) -> str:
        if not usage.get("allowed"):
            return "not readable with this key (turn on its usage command in OmniRoute)"
        personal = usage.get("personal")
        parts = []
        if isinstance(personal, dict):
            parts.append(f"today {money(personal.get('dailySpentUsd'))} of {money(personal.get('dailyLimitUsd'))}")
            parts.append(f"this week {money(personal.get('weeklySpentUsd'))} of {money(personal.get('weeklyLimitUsd'))}")
        else:
            parts.append("no spending limit on this key")
        connected = [str(item.get("provider")) for item in usage.get("providers") or [] if item.get("provider")]
        parts.append(f"{len(connected)} provider quota(s)" + (f": {', '.join(connected)}" if connected else ""))
        return ", ".join(parts)

    @staticmethod
    def models_text(listed: dict) -> str:
        if not listed["models"]:
            return "No model matches."
        width = max(len(row["id"]) for row in listed["models"])
        lines = [
            f"{row['id']:<{width}}  {compact(row['context_length']):>6}  {','.join(row['capabilities']) or '-'}"
            for row in listed["models"]
        ]
        shown = len(listed["models"])
        if listed["total"] > shown:
            lines.append(f"... {listed['total'] - shown} more; narrow it with a word or raise the limit")
        return "\n".join(lines)

    @staticmethod
    def ask_text(answer: dict) -> str:
        usage = answer.get("usage") or {}
        footer = (
            f"[{answer['model']}, {usage.get('prompt_tokens', '-')} in, {usage.get('completion_tokens', '-')} out, "
            f"{answer['latency_ms']} ms]"
        )
        content = answer["content"].strip()
        if not content:
            content = f"(the model answered no text, finish reason {answer.get('finish_reason') or 'unknown'})"
        return f"{content}\n\n{footer}"

    # plugin/1 methods.

    def route(self, params: dict) -> dict:
        name = params.get("route")
        caller = params.get("caller") or {}
        project = str(caller.get("project") or "")
        request = params.get("request") or {}
        method = str(request.get("method") or "GET").upper()
        query = request.get("query") or {}
        text = query.get("render") == "text"
        if name not in ("status", "models", "ask"):
            raise Refused(INVALID_PARAMS, f"no route {name!r}")
        try:
            if name == "status":
                status = self.status(project)
                status["can_ask"] = self.may_ask(project, caller.get("role"))
                return self.answer(200, {"text": self.status_text(status)} if text else status)
            if name == "models":
                listed = self.models(project, query.get("q", ""), query.get("capability", ""), query.get("limit") or 100)
                return self.answer(200, {"text": self.models_text(listed)} if text else listed)
            if method != "POST":
                return self.answer(405, {"detail": "ask takes POST"})
            if not self.may_ask(project, caller.get("role")):
                needed = self.settings(project).get("ask_role")
                return self.answer(403, {"detail": f"asking a model takes the role {needed} in {project}"})
            try:
                body = json.loads(base64.b64decode(request.get("body") or "") or b"{}")
            except ValueError:
                return self.answer(422, {"detail": "the body is not JSON"})
            if not isinstance(body, dict):
                return self.answer(422, {"detail": "the body is a JSON object"})
            answer = self.ask(project, body.get("prompt"), body.get("model") or "", body.get("system") or "",
                              body.get("max_tokens"))
            return self.answer(200, {"text": self.ask_text(answer)} if text else answer)
        except NotConfigured as exc:
            return self.answer(409, {"detail": str(exc)})
        except (ValueError, TypeError) as exc:
            return self.answer(422, {"detail": str(exc)})
        except GatewayError as exc:
            return self.answer(502, {"detail": f"OmniRoute: {exc}", "gateway_status": exc.status or None})

    def may_ask(self, project: str, role) -> bool:
        """Whether a member of ``role`` may ask a model from the page and the CLI, by the project's ask_role."""
        needed = str(self.settings(project).get("ask_role") or "writer")
        have = str(role or "reader")
        return ROLES.index(have if have in ROLES else "reader") >= ROLES.index(needed if needed in ROLES else "writer")

    @staticmethod
    def answer(status: int, body: dict) -> dict:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        return {
            "status": status,
            "headers": {"content-type": "application/json"},
            "body": base64.b64encode(data).decode("ascii"),
        }

    def tool(self, params: dict) -> dict:
        name = params.get("tool")
        project = str((params.get("caller") or {}).get("project") or "")
        arguments = params.get("arguments") or {}
        if name not in ("models", "ask", "usage"):
            raise Refused(INVALID_PARAMS, f"no tool {name!r}")
        try:
            if name == "models":
                listed = self.models(project, arguments.get("query", ""), arguments.get("capability", ""),
                                     arguments.get("limit") or 50)
                return self.said(self.models_text(listed))
            if name == "ask":
                answer = self.ask(project, arguments.get("prompt"), arguments.get("model") or "",
                                  arguments.get("system") or "", arguments.get("max_tokens"))
                return self.said(self.ask_text(answer))
            return self.said("OmniRoute key usage: " + self.usage_line(self.usage(project)))
        except NotConfigured as exc:
            return self.said(str(exc), error=True)
        except (ValueError, TypeError) as exc:
            return self.said(str(exc), error=True)
        except GatewayError as exc:
            return self.said(f"OmniRoute: {exc}", error=True)

    @staticmethod
    def said(text: str, error: bool = False) -> dict:
        return {"content": [{"type": "text", "text": text}], "is_error": error}

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


class Server:
    """Reads requests, runs the slow ones in threads, writes each answer as one line."""

    INLINE = ("initialize", "ping", "shutdown")

    def __init__(self, plugin: Plugin, out=None):
        self.plugin = plugin
        self.out = out or sys.stdout
        self.lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_WORKERS)

    def reply(self, message_id, result=None, error=None) -> None:
        message = {"jsonrpc": "2.0", "id": message_id}
        message.update({"error": error} if error is not None else {"result": result})
        with self.lock:
            self.out.write(json.dumps(message) + "\n")
            self.out.flush()

    def run(self, method: str, message_id, params: dict) -> None:
        try:
            self.reply(message_id, self.plugin.handle(method, params))
        except Refused as exc:
            self.reply(message_id, error={"code": exc.code, "message": str(exc)})
        except Exception as exc:  # the backend's own failure; it keeps serving
            print(f"omniroute: {method} failed: {type(exc).__name__}", file=sys.stderr)
            self.reply(message_id, error={"code": INTERNAL, "message": "internal error"})

    def threaded(self, method: str, message_id, params: dict) -> None:
        try:
            self.run(method, message_id, params)
        finally:
            self.slots.release()

    def serve(self, lines) -> None:
        for line in lines:
            if not line.strip():
                continue
            try:
                message = json.loads(line)
            except ValueError:
                self.reply(None, error={"code": PARSE_ERROR, "message": "not JSON"})
                continue
            if not isinstance(message, dict):
                self.reply(None, error={"code": INVALID_REQUEST, "message": "not a request"})
                continue
            method, message_id = str(message.get("method")), message.get("id")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method in self.INLINE or method not in ("route", "tool"):
                self.run(method, message_id, params)
                if method == "shutdown":
                    return
                continue
            self.slots.acquire()
            threading.Thread(target=self.threaded, args=(method, message_id, params), daemon=True).start()


def main() -> None:
    Server(Plugin(json.loads(MANIFEST.read_text(encoding="utf-8")))).serve(sys.stdin)


if __name__ == "__main__":
    main()
