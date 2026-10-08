"""Shared helpers for the gateway tests. Keeps every socket in 17970-17999."""

import http.client
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_DIR = os.path.dirname(HERE)
FAKES = os.path.join(HERE, "fakes")
FAKE_APP = os.path.join(FAKES, "fake_app.py")
FAKE_BRIDGE = os.path.join(FAKES, "fake_bridge.py")

TEST_PORT_LO = 17970
TEST_PORT_HI = 17999

sys.path.insert(0, GATEWAY_DIR)

import config as config_mod  # noqa: E402
import gateway as gateway_mod  # noqa: E402
import slots as slots_mod  # noqa: E402


def free_port():
    for port in range(TEST_PORT_LO, TEST_PORT_HI + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise RuntimeError("no free test port")


def temp_home():
    return tempfile.mkdtemp(prefix="craft-gateway-test-")


def dev_app_cmd(home, slot="designcraft"):
    return [
        sys.executable,
        FAKE_APP,
        "--control",
        "{port}",
        "--state",
        os.path.join(home, "fake_state.json"),
    ]


def dev_bridge_cmd(home, slot="designcraft"):
    return [
        sys.executable,
        FAKE_BRIDGE,
        "--connect",
        "{addr}",
        "--log",
        os.path.join(home, "bridge_%s.log" % slot),
    ]


def write_config(home, port, window_slots=("designcraft",), apps=None, dev=None):
    raw = {"port": port, "apps": {}, "dev": {}}
    for slot in window_slots:
        raw["dev"][slot] = {
            "app_cmd": dev_app_cmd(home, slot),
            "bridge_cmd": dev_bridge_cmd(home, slot),
        }
    if apps:
        for slot, entry in apps.items():
            raw["apps"][slot] = dict(entry)
    if dev:
        for slot, entry in dev.items():
            raw["dev"][slot] = dict(entry)
    cfg = config_mod.from_raw(raw)
    config_mod.save(home, cfg)
    return cfg


def set_inspect_error(home, on=True):
    with open(os.path.join(home, "fake_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"documents": [], "inspect_error": bool(on)}, fh)


def set_state(home, documents):
    with open(os.path.join(home, "fake_state.json"), "w", encoding="utf-8") as fh:
        json.dump({"documents": documents}, fh)


# --------------------------------------------------------------------------- #
# HTTP client
# --------------------------------------------------------------------------- #


def http_request(port, method, path, body=None, headers=None, timeout=60.0):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    payload = None
    send_headers = dict(headers or {})
    if body is not None:
        if isinstance(body, (dict, list)):
            payload = json.dumps(body).encode("utf-8")
            send_headers.setdefault("Content-Type", "application/json")
        elif isinstance(body, str):
            payload = body.encode("utf-8")
        else:
            payload = body
    try:
        conn.request(method, path, body=payload, headers=send_headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, dict(resp.getheaders()), raw
    finally:
        conn.close()


def http_json(port, method, path, body=None, headers=None, timeout=60.0):
    status, resp_headers, raw = http_request(
        port, method, path, body=body, headers=headers, timeout=timeout
    )
    data = json.loads(raw.decode("utf-8")) if raw else None
    return status, resp_headers, data


def mcp(port, slot, message, headers=None, timeout=60.0):
    return http_json(
        port, "POST", "/" + slot, body=message, headers=headers, timeout=timeout
    )


def initialize_message(mid=1):
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }


def read_bridge_log(home, slot="designcraft"):
    path = os.path.join(home, "bridge_%s.log" % slot)
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


# --------------------------------------------------------------------------- #
# gateway fixture
# --------------------------------------------------------------------------- #


class GatewayFixture:
    def __init__(self, home, control_range=(TEST_PORT_LO, TEST_PORT_HI)):
        self.home = home
        self.control_range = control_range
        self.gateway = None

    def start(self):
        self.gateway = gateway_mod.Gateway(
            self.home, control_range=self.control_range
        )
        self.gateway.start()
        self.thread = threading.Thread(
            target=self.gateway.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self.thread.start()
        self.port = self.gateway.port
        deadline = time.time() + 5
        while time.time() < deadline:
            if gateway_mod.control_port_open(self.port, timeout=0.3):
                return self
            time.sleep(0.05)
        raise RuntimeError("gateway did not start")

    def cleanup(self):
        if self.gateway is not None:
            try:
                self.gateway.shutdown_report(force=True)
            except Exception:
                pass
            try:
                self.gateway.stop_server()
            except Exception:
                pass
        if getattr(self, "thread", None) is not None:
            self.thread.join(timeout=5)


class GatewayTestCase(unittest.TestCase):
    """Base class that gives each test a fresh temp home and gateway."""

    def setUp(self):
        self.home = temp_home()
        self.port = free_port()
        self.fixture = GatewayFixture(self.home)
        self.addCleanup(self._cleanup_home)

    def _cleanup_home(self):
        if getattr(self, "fixture", None) is not None:
            self.fixture.cleanup()
        shutil.rmtree(self.home, ignore_errors=True)

    def start_gateway(self, **kwargs):
        if "port" in kwargs:
            self.port = kwargs.pop("port")
        write_config(self.home, self.port, **kwargs)
        self.fixture.start()
        return self.fixture

    def start_default(self):
        return self.start_gateway(window_slots=("designcraft",))
