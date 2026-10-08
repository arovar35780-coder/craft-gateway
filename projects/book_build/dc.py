"""Tiny client for the DesignCraft control channel (JSON lines on 127.0.0.1:7979)."""
import json
import os
import socket


class DC:
    def __init__(self, port=None, timeout=300):
        # DESIGNCRAFT_PORT lets scripts use an app started by the gateway (its control port is dynamic).
        port = port or int(os.environ.get("DESIGNCRAFT_PORT", 7979))
        self.s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        self.f = self.s.makefile("rwb")
        self.n = 0

    def call(self, method, **params):
        self.n += 1
        self.f.write((json.dumps({"id": self.n, "method": method, "params": params}, ensure_ascii=False) + "\n").encode("utf-8"))
        self.f.flush()
        r = json.loads(self.f.readline().decode("utf-8"))
        if not r.get("ok", False):
            raise RuntimeError(f"{method} {params.get('command', '')}: {r.get('error', r)}")
        return r.get("result")

    def x(self, command, **params):
        """engine.execute shortcut."""
        return self.call("engine.execute", command=command, params=params)

    def inspect(self):
        return self.call("document.inspect")

    def close(self):
        self.f.close()
        self.s.close()
