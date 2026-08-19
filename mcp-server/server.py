#!/usr/bin/env python3
"""
Minimal MCP server over the Streamable HTTP transport.

Two instances run with different tool sets ("clock" and "math") so the
comparison can test *multiplexing*: both gateways claim to aggregate several MCP
servers behind one endpoint, and the check is whether a single tools/list call
returns the union, and how each gateway disambiguates same-named tools.

Implements only what a gateway exercises: initialize, notifications/initialized,
tools/list, tools/call, ping. Responses are JSON or SSE depending on the
client's Accept header, since the transport permits either and the two gateways
do not ask the same way.

Env:
  SERVER_ID   which tool set to serve: clock | math   (default clock)
  PORT        listen port (default 8080)
  FORCE_SSE   "1" to always answer with text/event-stream
"""
import json, os, threading, time, uuid
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVER_ID = os.environ.get("SERVER_ID", "clock")
PORT = int(os.environ.get("PORT", "8080"))
FORCE_SSE = os.environ.get("FORCE_SSE", "") == "1"
DEFAULT_PROTOCOL = "2025-06-18"

RECENT = deque(maxlen=50)
SESSIONS = set()
LOCK = threading.Lock()

TOOLSETS = {
    "clock": [
        {"name": "get_time",
         "description": "Return the current server time.",
         "inputSchema": {"type": "object", "properties": {
             "timezone": {"type": "string", "description": "IANA tz name"}}}},
        {"name": "ping_host",
         "description": "Pretend to ping a host and report latency.",
         "inputSchema": {"type": "object",
                         "properties": {"host": {"type": "string"}},
                         "required": ["host"]}},
    ],
    "math": [
        {"name": "add",
         "description": "Add two numbers.",
         "inputSchema": {"type": "object",
                         "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                         "required": ["a", "b"]}},
        {"name": "multiply",
         "description": "Multiply two numbers.",
         "inputSchema": {"type": "object",
                         "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                         "required": ["a", "b"]}},
        # Deliberately shares a name with the clock server's tool so we can see
        # how each gateway disambiguates collisions when multiplexing.
        {"name": "get_time",
         "description": "Return the current time, math-server flavour.",
         "inputSchema": {"type": "object", "properties": {}}},
    ],
}
TOOLS = TOOLSETS.get(SERVER_ID, TOOLSETS["clock"])


def call_tool(name, args):
    if name == "get_time":
        return f"[{SERVER_ID}] time is {time.strftime('%H:%M:%S', time.gmtime())} UTC"
    if name == "ping_host":
        return f"[{SERVER_ID}] {args.get('host','?')} responded in 12ms"
    if name == "add":
        return f"[{SERVER_ID}] {args.get('a')} + {args.get('b')} = {args.get('a',0) + args.get('b',0)}"
    if name == "multiply":
        return f"[{SERVER_ID}] {args.get('a')} * {args.get('b')} = {args.get('a',0) * args.get('b',0)}"
    return None


def handle_rpc(msg, session_id):
    """Return a JSON-RPC response dict, or None for notifications."""
    method = msg.get("method")
    mid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        # Echo the client's protocol version when it sends one; gateways vary.
        ver = params.get("protocolVersion") or DEFAULT_PROTOCOL
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": ver,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": f"mock-mcp-{SERVER_ID}", "version": "1.0.0"}}}

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        name = params.get("name", "")
        # A multiplexing gateway may prefix the tool name with its target, e.g.
        # "math_add" or "math/add" -- accept either form.
        bare = name.split("/")[-1]
        for cand in (name, bare, bare.split("_", 1)[-1]):
            out = call_tool(cand, params.get("arguments") or {})
            if out is not None:
                return {"jsonrpc": "2.0", "id": mid, "result": {
                    "content": [{"type": "text", "text": out}], "isError": False}}
        return {"jsonrpc": "2.0", "id": mid, "error": {
            "code": -32602, "message": f"unknown tool: {name}"}}

    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)

    def _raw(self, code, body: bytes, ctype, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/__requests"):
            with LOCK:
                return self._raw(200, json.dumps(
                    {"server": SERVER_ID, "requests": list(RECENT)}).encode(),
                    "application/json")
        if self.path in ("/health", "/healthz", "/ready"):
            return self._raw(200, json.dumps({"status": "ok", "server": SERVER_ID}).encode(),
                             "application/json")
        # No server-initiated stream in this mock.
        return self._raw(405, b'{"error":"method not allowed"}', "application/json")

    def do_DELETE(self):
        sid = self.headers.get("mcp-session-id")
        with LOCK:
            SESSIONS.discard(sid)
        return self._raw(204, b"", "application/json")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._raw(400, b'{"jsonrpc":"2.0","error":{"code":-32700,'
                                  b'"message":"parse error"}}', "application/json")

        sid = self.headers.get("mcp-session-id") or uuid.uuid4().hex
        batch = payload if isinstance(payload, list) else [payload]
        with LOCK:
            RECENT.append({
                "methods": [m.get("method") for m in batch],
                "session": self.headers.get("mcp-session-id"),
                "authorization": self.headers.get("authorization"),
                "path": self.path,
                "at": time.time(),
            })
            SESSIONS.add(sid)

        responses = [r for r in (handle_rpc(m, sid) for m in batch) if r is not None]
        extra = {"Mcp-Session-Id": sid}

        if not responses:                      # notification-only batch
            return self._raw(202, b"", "application/json", extra)

        out = responses if isinstance(payload, list) else responses[0]
        accept = (self.headers.get("accept") or "")
        wants_sse = FORCE_SSE or ("text/event-stream" in accept
                                  and "application/json" not in accept)
        if wants_sse:
            body = ("event: message\ndata: " + json.dumps(out) + "\n\n").encode()
            return self._raw(200, body, "text/event-stream", extra)
        return self._raw(200, json.dumps(out).encode(), "application/json", extra)


if __name__ == "__main__":
    print(f"mock-mcp {SERVER_ID} listening :{PORT} tools="
          f"{[t['name'] for t in TOOLS]}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
