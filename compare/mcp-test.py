#!/usr/bin/env python3
"""
MCP conformance probe for both gateways.

Runs a real Streamable HTTP handshake (initialize -> initialized -> tools/list
-> tools/call) against each gateway and reports what came back. Handles both
JSON and SSE responses because the two gateways answer differently.

Usage: compare/mcp-test.py [--markdown]
"""
import json, sys, urllib.request, urllib.error

TARGETS = [
    ("Envoy AI Gateway", "http://localhost:8080/mcp"),
    ("agentgateway",     "http://localhost:8081/mcp"),
]


def parse(raw: str):
    """Accept a plain JSON body or an SSE frame carrying one."""
    raw = raw.strip()
    if not raw:
        return None
    if raw.startswith("{") or raw.startswith("["):
        return json.loads(raw)
    for line in raw.splitlines():                 # SSE: id:/event:/data:
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return None


class Client:
    def __init__(self, url):
        self.url, self.session = url, None

    def rpc(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if not notify:
            msg["id"] = 1
        if params is not None:
            msg["params"] = params
        req = urllib.request.Request(
            self.url, data=json.dumps(msg).encode(),
            headers={"content-type": "application/json",
                     "accept": "application/json, text/event-stream",
                     **({"mcp-session-id": self.session} if self.session else {})})
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                sid = r.headers.get("mcp-session-id")
                if sid:
                    self.session = sid
                return r.status, parse(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, parse(e.read().decode() or "")
        except Exception as e:
            return 0, {"error": {"message": str(e)}}


def probe(name, url):
    c = Client(url)
    out = {"name": name}

    code, res = c.rpc("initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "probe", "version": "1"}})
    out["init_code"] = code
    info = (res or {}).get("result", {}) if res else {}
    out["server_name"] = info.get("serverInfo", {}).get("name")
    out["protocol"] = info.get("protocolVersion")
    out["capabilities"] = sorted((info.get("capabilities") or {}).keys())
    out["session"] = bool(c.session)

    c.rpc("notifications/initialized", notify=True)

    code, res = c.rpc("tools/list")
    tools = ((res or {}).get("result", {}) or {}).get("tools", []) if res else []
    out["tools"] = [t["name"] for t in tools]

    # Call one tool from each upstream server to prove routing to the right one.
    out["calls"] = []
    for want in ("add", "ping_host"):
        target = next((t for t in out["tools"] if t.endswith(want)), None)
        if not target:
            out["calls"].append((want, "no matching tool")); continue
        args = {"a": 2, "b": 40} if want == "add" else {"host": "example.com"}
        code, res = c.rpc("tools/call", {"name": target, "arguments": args})
        r = (res or {}).get("result") if res else None
        text = (r or {}).get("content", [{}])[0].get("text") if r else \
               json.dumps((res or {}).get("error", {}))[:80]
        out["calls"].append((target, text))
    return out


results = [probe(n, u) for n, u in TARGETS]

if "--markdown" in sys.argv:
    print("| | " + " | ".join(r["name"] for r in results) + " |")
    print("|---|" + "---|" * len(results))
    def row(label, fn):
        print(f"| {label} | " + " | ".join(fn(r) for r in results) + " |")
    row("initialize", lambda r: f"http {r['init_code']}")
    row("advertised server name", lambda r: f"`{r['server_name']}`")
    row("protocol version", lambda r: f"`{r['protocol']}`")
    row("capabilities advertised", lambda r: ", ".join(f"`{c}`" for c in r["capabilities"]) or "—")
    row("session id issued", lambda r: "yes" if r["session"] else "no")
    row("tools after multiplexing", lambda r: str(len(r["tools"])))
    row("tool naming", lambda r: "<br>".join(f"`{t}`" for t in r["tools"]))
    for i in range(2):
        row(f"tools/call #{i+1}",
            lambda r, i=i: f"`{r['calls'][i][0]}` → {r['calls'][i][1]}"
            if i < len(r["calls"]) else "—")
else:
    for r in results:
        print(f"===== {r['name']} =====")
        for k in ("init_code", "server_name", "protocol", "capabilities", "session", "tools"):
            print(f"  {k:<22} {r[k]}")
        for t, out in r["calls"]:
            print(f"  call {t:<18} -> {out}")
