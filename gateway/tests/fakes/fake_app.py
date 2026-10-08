"""Fake desktop app used by the gateway tests.

Speaks the JSON-lines control protocol on 127.0.0.1:<port>. Documents come from
a JSON state file so tests can flip dirty documents at runtime.
"""

import argparse
import json
import os
import socket
import sys
import threading


def load_state(path, env_key):
    if path and os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("documents"), list):
                return data["documents"]
        except (OSError, ValueError):
            pass
    env = os.environ.get(env_key)
    if env:
        try:
            data = json.loads(env)
            if isinstance(data, dict) and isinstance(data.get("documents"), list):
                return data["documents"]
        except ValueError:
            pass
    return []


def inspect_fails(path):
    """True when the state file asks ui.inspect to fail (simulates a busy app)."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return bool(json.load(fh).get("inspect_error"))
    except (OSError, ValueError, AttributeError, TypeError):
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--control", type=int, required=True)
    parser.add_argument("--state", default=None)
    args = parser.parse_args()

    stop = threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.control))
    server.listen(16)
    server.settimeout(0.5)
    print("fake_app listening on %d" % args.control, file=sys.stderr, flush=True)

    def handle(conn):
        try:
            reader = conn.makefile("rb")
            for raw in reader:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                method = message.get("method")
                mid = message.get("id")
                if method == "app.quit":
                    conn.sendall(
                        (json.dumps({"id": mid, "ok": True, "result": {}}) + "\n").encode()
                    )
                    stop.set()
                    return
                if method == "ui.inspect" and inspect_fails(args.state):
                    conn.sendall(
                        (json.dumps({"id": mid, "ok": False, "error": "busy"}) + "\n").encode()
                    )
                    continue
                if method == "ui.inspect":
                    result = {
                        "documents": load_state(args.state, "FAKE_APP_DOCUMENTS")
                    }
                else:
                    result = {}
                conn.sendall(
                    (json.dumps({"id": mid, "ok": True, "result": result}) + "\n").encode()
                )
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    while not stop.is_set():
        try:
            conn, _ = server.accept()
        except socket.timeout:
            continue
        except OSError:
            break
        threading.Thread(target=handle, args=(conn,), daemon=True).start()

    try:
        server.close()
    except OSError:
        pass
    print("fake_app exiting", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
