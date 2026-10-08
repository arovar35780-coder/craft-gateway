"""Fake stdio MCP bridge used by the gateway tests.

Minimal JSON-RPC 2.0 over newline-delimited stdin/stdout: initialize,
tools/list (one `echo` tool) and tools/call. `echo` accepts an optional `delay`
(seconds) and a `text`; a `crash` call exits without replying.
"""

import argparse
import io
import json
import os
import sys
import time

INIT_RESULT = {
    "protocolVersion": "2025-06-18",
    "capabilities": {"tools": {}},
    "serverInfo": {"name": "fake-bridge", "version": "1.0"},
}


def log_line(path, line):
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--connect", default=None)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--log", default=None)
    args = parser.parse_args()

    print(
        "fake_bridge up (connect=%s headless=%s)" % (args.connect, args.headless),
        file=sys.stderr,
        flush=True,
    )
    for raw in io.TextIOWrapper(sys.stdin.buffer, encoding="utf-8"):
        line = raw.strip()
        if not line:
            continue
        log_line(args.log, line)
        try:
            message = json.loads(line)
        except ValueError:
            continue
        method = message.get("method")
        mid = message.get("id")
        if method is None:
            continue  # a response; ignore
        if method == "initialize":
            reply = {"jsonrpc": "2.0", "id": mid, "result": INIT_RESULT}
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            reply = {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {
                    "tools": [
                        {
                            "name": "echo",
                            "description": "echo text",
                            "inputSchema": {"type": "object"},
                        }
                    ]
                },
            }
        elif method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name == "crash":
                print("fake_bridge crashing on request", file=sys.stderr, flush=True)
                os._exit(1)
            delay = arguments.get("delay")
            if delay:
                time.sleep(float(delay))
            text = arguments.get("text", name or "")
            reply = {
                "jsonrpc": "2.0",
                "id": mid,
                "result": {"content": [{"type": "text", "text": text}]},
            }
        else:
            reply = {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32601, "message": "method not found: %s" % method},
            }
        # Real bridges print raw UTF-8 (not \u escapes); the gateway must decode it as UTF-8.
        sys.stdout.buffer.write((json.dumps(reply, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8"))
        sys.stdout.flush()
    print("fake_bridge stdin closed", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
