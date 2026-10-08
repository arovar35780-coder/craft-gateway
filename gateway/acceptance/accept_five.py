"""Acceptance of the five newer slots (photocraft, lightcraft, filmcraft, printcraft, effectcraft) through the
gateway, using the REAL builds in crafting-bin. Temporary CRAFT_GATEWAY_HOME and port 7969 (the user's own
gateway on 7970 is untouched). Real windows open for a few seconds. Starts and stops only its own processes.
"""
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# The folder that holds gateway/ and the app clones; override with CRAFT_ROOT.
ROOT = Path(os.environ.get("CRAFT_ROOT") or Path(__file__).resolve().parents[2])
GW = ROOT / "gateway"
PORT = 7969
HOME = tempfile.mkdtemp(prefix="gw_accept5_")
ENV = {**os.environ, "CRAFT_GATEWAY_HOME": HOME}
SLOTS = ["photocraft", "lightcraft", "filmcraft", "printcraft", "effectcraft"]
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail and not ok else ""), flush=True)


def ctl(*args):
    r = subprocess.run([sys.executable, str(GW / "gatewayctl.py"), *args], capture_output=True, text=True, env=ENV, timeout=180)
    return r.returncode, (r.stdout + r.stderr).strip()


def rpc(slot, method, params=None, id_=1):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=300)
    body = {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}
    c.request("POST", "/" + slot, json.dumps(body), {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, (json.loads(data) if data else None)


def status(dirty=False):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status" + ("?dirty=1" if dirty else ""), timeout=60) as r:
        return json.load(r)["apps"]


def alive(name):
    out = subprocess.run(["powershell", "-NoProfile", "-Command", f"Get-Process {name} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"], capture_output=True, text=True).stdout
    return [int(x) for x in out.split()]


def main():
    before = {s: alive(s) for s in SLOTS}
    for s in SLOTS:
        check(f"set-path {s}", ctl("config", "set-path", s, str(ROOT / "crafting-bin" / s))[0] == 0)
    ctl("config", "set-port", str(PORT))
    code, out = ctl("start")
    check("gateway start", code == 0, out)
    try:
        st = status()
        check("all five slots supported and valid", all(st[s]["supported"] and st[s]["valid"] for s in SLOTS), json.dumps({s: st[s]["reason"] for s in SLOTS}))
        for s in SLOTS:
            print(f"\n== {s}", flush=True)
            code_, init = rpc(s, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "accept", "version": "1"}})
            check(f"{s}: initialize through the gateway", code_ == 200 and init and "result" in init, json.dumps(init)[:300])
            code_, tl = rpc(s, "tools/list", id_=2)
            tools = [t["name"] for t in (tl or {}).get("result", {}).get("tools", [])]
            check(f"{s}: tools/list non-empty ({len(tools)})", len(tools) > 0, json.dumps(tl)[:300])
            info = status(dirty=True)[s]
            check(f"{s}: running on a gateway port", info["state"] == "running" and 7971 <= (info["control_port"] or 0) <= 7999, json.dumps(info)[:300])
            check(f"{s}: dirty check understood (empty list, not unknown)", info["dirty_documents"] == [], repr(info["dirty_documents"]))
            probe = {"photocraft": "session_list", "lightcraft": "inspect_ui", "printcraft": "ui_state"}.get(s)
            if probe and probe in tools:
                code_, r = rpc(s, "tools/call", {"name": probe, "arguments": {}}, id_=3)
                check(f"{s}: GUI tool {probe} reaches the app", code_ == 200 and r and "result" in r and not r["result"].get("isError"), json.dumps(r)[:300])
            code, out = ctl("app-stop", s)
            check(f"{s}: clean stop accepted", code == 0, out)
            time.sleep(2)
            info = status()[s]
            check(f"{s}: stopped, no bridge", info["state"] == "stopped" and not info["bridge_pid"], json.dumps(info)[:300])
            check(f"{s}: no process left", not [p for p in alive(s) if p not in before[s]], str(alive(s)))
    finally:
        code, out = ctl("stop", "--force")
        check("gateway stop", code == 0, out)
        time.sleep(3)
    left = {s: [p for p in alive(s) if p not in before[s]] for s in SLOTS}
    check("nothing left running", not any(left.values()), str(left))
    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    for n, _, d in bad:
        print("FAILED:", n, d[:300])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
