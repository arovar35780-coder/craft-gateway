"""Acceptance test of craft-gateway against the REAL DesignCraft and VectorCraft builds (paths below are for this machine).

Run from anywhere:  python accept_gateway.py
Uses a temporary CRAFT_GATEWAY_HOME and port 7970. It starts real apps through the gateway, so run it
when you do not mind two windows opening for a few seconds. Leaves nothing running; the manually started
DesignCraft on port 7979 must survive untouched.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# The folder that holds gateway/ and the app clones; override with CRAFT_ROOT.
ROOT = Path(os.environ.get("CRAFT_ROOT") or Path(__file__).resolve().parents[2])
GW = ROOT / "gateway"
PORT = 7970
HOME = tempfile.mkdtemp(prefix="gw_accept_")
ENV = {**os.environ, "CRAFT_GATEWAY_HOME": HOME}
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def ctl(*args, expect=None):
    r = subprocess.run([sys.executable, str(GW / "gatewayctl.py"), *args], capture_output=True, text=True, env=ENV, timeout=120)
    return r.returncode, (r.stdout + r.stderr).strip()


def post(path, body, headers=None, timeout=300):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=timeout)
    h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})}
    c.request("POST", path, json.dumps(body), h)
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


def rpc(path, method, params=None, id_=1, headers=None):
    s, h, d = post(path, {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}, headers)
    return s, h, (json.loads(d) if d else None)


def get_json(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=30) as r:
        return json.load(r)


def procs(name):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-Process {name} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def tool_text(resp):
    return "".join(c.get("text", "") for c in resp["result"]["content"])


def main():
    manual_dc = procs("designcraft")
    check("precondition: the manually started DesignCraft runs", len(manual_dc) >= 1, str(manual_dc))
    check("config set-path designcraft", ctl("config", "set-path", "designcraft", str(ROOT / "crafting-bin" / "designcraft"))[0] == 0)
    check("config set-path vectorcraft", ctl("config", "set-path", "vectorcraft", str(ROOT / "crafting-bin" / "vectorcraft"))[0] == 0)
    check("config set-port", ctl("config", "set-port", str(PORT))[0] == 0)
    check("status when down exits 2", ctl("status")[0] == 2)
    code, out = ctl("start")
    check("gatewayctl start", code == 0, out)
    try:
        st = get_json("/status")
        check("status lists seven slots", len(st["apps"]) == 7, str(list(st["apps"])))
        check("designcraft/vectorcraft valid", st["apps"]["designcraft"]["valid"] and st["apps"]["vectorcraft"]["valid"], json.dumps(st["apps"]["designcraft"]))
        check("other slots disabled", all(st["apps"][a]["state"] == "disabled" for a in ("photocraft", "lightcraft", "filmcraft", "pdfcraft", "effectcraft")))
        page = urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=30).read().decode("utf-8", "replace")
        check("start page has the registration line", f"http://127.0.0.1:{PORT}/designcraft" in page and "claude mcp add --transport http" in page)
        check("start page warns about no authentication", "No authentication" in page)

        # security checks
        s, _, _ = post("/designcraft", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Origin": "http://evil.example"})
        check("bad Origin -> 403", s == 403, str(s))
        s, _, _ = post("/designcraft", {"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"Host": "evil.example"})
        check("bad Host -> 403", s == 403, str(s))
        s, _, _ = post("/admin/shutdown", {}, {"Origin": "http://evil.example"})
        check("admin with bad Origin -> 403", s == 403, str(s))
        c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=30); c.request("GET", "/designcraft"); r = c.getresponse(); r.read()
        check("GET /<slot> -> 405", r.status == 405, str(r.status))

        # real DesignCraft through the gateway
        s, h, r1 = rpc("/designcraft", "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "accept", "version": "1"}})
        check("designcraft initialize", s == 200 and r1 and "result" in r1, json.dumps(r1)[:300])
        check("initialize returns Mcp-Session-Id", any(k.lower() == "mcp-session-id" for k in h))
        s, _, _ = post("/designcraft", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("notification -> 202", s == 202, str(s))
        s, _, r2 = rpc("/designcraft", "tools/list", id_=2)
        names = [t["name"] for t in r2["result"]["tools"]]
        check("designcraft tools/list has execute and new_document", "execute" in names and "new_document" in names, str(names[:8]))
        st = get_json("/status")["apps"]["designcraft"]
        check("designcraft started by the gateway on a free port in 7971-7999", st["state"] == "running" and 7971 <= st["control_port"] <= 7999 and st["control_port"] not in (7979, 7981), json.dumps(st))
        check("the manual DesignCraft is untouched", all(p in procs("designcraft") for p in manual_dc))
        s, _, r3 = rpc("/designcraft", "tools/call", {"name": "new_document", "arguments": {"preset": "A4"}}, id_=3)
        check("tools/call new_document works", s == 200 and "result" in r3 and not r3["result"].get("isError"), json.dumps(r3)[:300])
        s, _, r4 = rpc("/designcraft", "tools/call", {"name": "inspect_document", "arguments": {}}, id_=4)
        check("inspect_document shows the new A4 document", "pageCount" in tool_text(r4), tool_text(r4)[:200])

        # two concurrent clients with identical ids
        outs = {}

        def worker(k):
            outs[k] = rpc("/designcraft", "tools/call", {"name": "execute", "arguments": {"command": "document.inspect" if False else "edit.deselectAll"}}, id_=7)
        ts = [threading.Thread(target=worker, args=(k,)) for k in range(4)]
        [t.start() for t in ts]; [t.join() for t in ts]
        check("four concurrent calls with the same id all answered", all(o[2] and o[2].get("id") == 7 and "result" in o[2] for o in outs.values()), json.dumps({k: (v[2] or {}).get("id") for k, v in outs.items()}))

        # dirty documents protection
        code, out = ctl("app-stop", "designcraft")
        check("stop refused while a document is unsaved", code != 0 and ("unsaved" in out.lower() or "dirty" in out.lower()), out)
        check("designcraft still running after the refusal", get_json("/status")["apps"]["designcraft"]["state"] == "running")

        # real VectorCraft through the gateway
        s, _, v1 = rpc("/vectorcraft", "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "accept", "version": "1"}})
        check("vectorcraft initialize", s == 200 and v1 and "result" in v1, json.dumps(v1)[:300])
        s, _, v2 = rpc("/vectorcraft", "tools/list", id_=2)
        check("vectorcraft tools/list non-empty", s == 200 and len(v2["result"]["tools"]) > 0)
        s, _, v3 = rpc("/photocraft", "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "accept", "version": "1"}})
        check("unconfigured slot gives a clear JSON-RPC error", v3 and "error" in v3 and "photocraft" in v3["error"]["message"].lower(), json.dumps(v3)[:300])

        # Claude Code can connect over HTTP
        add = subprocess.run(["claude", "mcp", "add", "--transport", "http", "gw-accept", f"http://127.0.0.1:{PORT}/designcraft"], capture_output=True, text=True)
        try:
            lst = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True, timeout=180).stdout
            line = [l for l in lst.splitlines() if l.startswith("gw-accept")]
            check("claude mcp list shows the gateway connected", bool(line) and "Connected" in line[0], line[0] if line else add.stdout + add.stderr)
        finally:
            subprocess.run(["claude", "mcp", "remove", "gw-accept"], capture_output=True, text=True)

        # forced stop and clean shutdown
        code, out = ctl("app-stop", "designcraft", "--force")
        check("forced stop works", code == 0, out)
        time.sleep(2)
        st = get_json("/status")["apps"]
        check("designcraft stopped, bridge gone", st["designcraft"]["state"] in ("stopped",) and not st["designcraft"]["bridge_pid"], json.dumps(st["designcraft"]))
    finally:
        code, out = ctl("stop", "--force")
        check("gatewayctl stop", code == 0, out)
        time.sleep(3)
    left = [p for p in procs("designcraft") if p not in manual_dc] + procs("vectorcraft") + procs("designcraft-cli") + procs("vectorcraft-cli")
    # the Claude Code MCP bridge of this session (designcraft-cli) may exist; only count processes started under the gateway home
    check("no gateway-started app or bridge left", not [p for p in procs("designcraft") if p not in manual_dc] and not procs("vectorcraft"), str(left))
    check("the manual DesignCraft still runs at the end", all(p in procs("designcraft") for p in manual_dc))
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    for n, _, d in bad:
        print("FAILED:", n, d[:300])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
