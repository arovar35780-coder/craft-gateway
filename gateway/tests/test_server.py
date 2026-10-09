"""HTTP surface tests: lazy start, MCP forwarding, errors, guards."""

import json
import os
import threading
import time
import unittest

from tests import helpers
from tests.helpers import GatewayTestCase, http_json, http_request, mcp, read_bridge_log


class McpTests(GatewayTestCase):
    def test_non_ascii_text_survives_the_bridge_pipe(self):
        # The bridge prints raw UTF-8; decoding it with the Windows default (cp1252) gave "ÐŸÑ€Ð¸Ð²ÐµÑ‚".
        self.start_default()
        text = "Привет, мир — “quotes” … ✓"
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        status, _, data = mcp(
            self.port,
            "designcraft",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": text}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["content"][0]["text"], text)

    def test_end_to_end_lazy_start(self):
        self.start_default()
        session_header = None

        status, headers, data = mcp(self.port, "designcraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        self.assertEqual(data["id"], 1)
        self.assertEqual(data["result"]["serverInfo"]["name"], "fake-bridge")
        session_header = headers.get("Mcp-Session-Id")
        self.assertTrue(session_header)

        status, _, data = mcp(
            self.port,
            "designcraft",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["tools"][0]["name"], "echo")

        status, _, data = mcp(
            self.port,
            "designcraft",
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "echo", "arguments": {"text": "hello"}},
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["content"][0]["text"], "hello")

        status, _, status_obj = http_json(self.port, "GET", "/status")
        info = status_obj["apps"]["designcraft"]
        self.assertEqual(info["state"], "running")
        self.assertIsNotNone(info["app_pid"])
        self.assertIsNotNone(info["bridge_pid"])
        self.assertTrue(info["valid"])

    def test_cached_initialize(self):
        self.start_default()
        status, headers, first = mcp(self.port, "designcraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        first_sid = headers.get("Mcp-Session-Id")
        status, headers, second = mcp(self.port, "designcraft", helpers.initialize_message(9))
        self.assertEqual(status, 200)
        self.assertEqual(second["id"], 9)
        self.assertEqual(second["result"], first["result"])
        self.assertTrue(headers.get("Mcp-Session-Id"))
        self.assertEqual(first_sid, headers.get("Mcp-Session-Id"))
        inits = [m for m in read_bridge_log(self.home) if m.get("method") == "initialize"]
        self.assertEqual(len(inits), 1)

    def test_id_remapping_two_concurrent_clients(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))

        results = {}
        errors = []
        barrier = threading.Barrier(2)

        def call(name, text, delay):
            try:
                barrier.wait()
                _, _, data = mcp(
                    self.port,
                    "designcraft",
                    {
                        "jsonrpc": "2.0",
                        "id": 7,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": {"text": text, "delay": delay}},
                    },
                )
                results[text] = data
            except Exception as exc:  # pragma: no cover - surfaced by assertion
                errors.append(exc)

        t1 = threading.Thread(target=call, args=("echo", "slow", 0.6))
        t2 = threading.Thread(target=call, args=("echo", "fast", 0.05))
        start = time.time()
        t1.start()
        t2.start()
        t1.join(30)
        t2.join(30)
        elapsed = time.time() - start
        self.assertEqual(errors, [])
        self.assertLess(elapsed, 1.5)  # they overlapped rather than serialized
        self.assertEqual(results["slow"]["id"], 7)
        self.assertIn("slow", json.dumps(results["slow"]))
        self.assertIn("fast", json.dumps(results["fast"]))

    def test_notification_returns_202(self):
        self.start_default()
        status, headers, raw = http_request(
            self.port,
            "POST",
            "/designcraft",
            body={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        self.assertEqual(status, 202)
        self.assertEqual(raw, b"")

    def test_disabled_slot_error(self):
        # No config at all -> designcraft has no path and no dev override.
        self.start_gateway(window_slots=())
        status, _, data = mcp(self.port, "designcraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], -32000)
        self.assertIn("designcraft", data["error"]["message"])
        self.assertIn("no path", data["error"]["message"])

    def test_invalid_path_error(self):
        self.start_gateway(
            window_slots=(),
            apps={"designcraft": {"path": "/definitely/not/here", "mode": "window"}},
        )
        status, _, data = mcp(self.port, "designcraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], -32000)
        self.assertIn("folder not found", data["error"]["message"])

    def test_unsupported_slot_error(self):
        self.start_gateway(window_slots=())
        # Every slot is implemented now; an unconfigured one reports its path problem.
        status, _, data = mcp(self.port, "photocraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], -32000)
        self.assertIn("no path configured", data["error"]["message"])

    def test_delete_and_get(self):
        self.start_default()
        status, headers, raw = http_request(self.port, "DELETE", "/designcraft")
        self.assertEqual(status, 200)
        status, headers, raw = http_request(self.port, "GET", "/designcraft")
        self.assertEqual(status, 405)
        self.assertEqual(headers.get("Allow"), "POST, DELETE")

    def test_unknown_session_id_is_accepted(self):
        self.start_default()
        status, _, data = mcp(
            self.port, "designcraft", helpers.initialize_message(1),
            headers={"Mcp-Session-Id": "does-not-exist"},
        )
        self.assertEqual(status, 200)
        self.assertIn("result", data)

    def test_batch_requests(self):
        self.start_default()
        body = [
            helpers.initialize_message(1),
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
        status, _, data = http_json(self.port, "POST", "/designcraft", body=body)
        self.assertEqual(status, 200)
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["id"], 1)
        self.assertEqual(data[1]["result"]["tools"][0]["name"], "echo")

    def test_headless_vectorcraft_bridge_only(self):
        self.start_gateway(
            window_slots=(),
            apps={"vectorcraft": {"mode": "headless"}},
            dev={"vectorcraft": {"bridge_cmd": helpers.dev_bridge_cmd(self.home, "vectorcraft")}},
        )
        status, _, data = mcp(self.port, "vectorcraft", helpers.initialize_message(1))
        self.assertEqual(status, 200)
        self.assertIn("result", data)
        _, _, status_obj = http_json(self.port, "GET", "/status?dirty=1")
        info = status_obj["apps"]["vectorcraft"]
        self.assertEqual(info["mode"], "headless")
        self.assertIsNone(info["app_pid"])
        self.assertIsNotNone(info["bridge_pid"])
        self.assertIsNone(info["dirty_documents"])

    def test_admin_start_and_restart(self):
        self.start_default()
        status, _, data = http_json(
            self.port, "POST", "/admin/apps/designcraft/start", body={}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _, data = http_json(
            self.port, "POST", "/admin/apps/designcraft/restart", body={}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        _, _, status_obj = http_json(self.port, "GET", "/status")
        self.assertEqual(status_obj["apps"]["designcraft"]["state"], "running")


class GuardTests(GatewayTestCase):
    def test_bad_host_403(self):
        self.start_default()
        status, _, data = mcp(
            self.port, "designcraft", helpers.initialize_message(1),
            headers={"Host": "evil.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Host", data["error"])

    def test_bad_origin_403(self):
        self.start_default()
        status, _, data = mcp(
            self.port, "designcraft", helpers.initialize_message(1),
            headers={"Origin": "http://evil.example.com"},
        )
        self.assertEqual(status, 403)
        self.assertIn("Origin", data["error"])

    def test_good_origin_allowed(self):
        self.start_default()
        headers = {"Origin": "http://127.0.0.1:%d" % self.port}
        status, _, data = mcp(self.port, "designcraft", helpers.initialize_message(1), headers=headers)
        self.assertEqual(status, 200)
        self.assertIn("result", data)


class PageTests(GatewayTestCase):
    def test_start_page_content(self):
        self.start_default()
        status, headers, raw = http_request(self.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        body = raw.decode("utf-8")
        self.assertIn("No authentication", body)
        self.assertIn("claude mcp add --transport http designcraft", body)
        self.assertIn("http://127.0.0.1:%d/designcraft" % self.port, body)
        for slot in ("designcraft", "vectorcraft", "photocraft"):
            self.assertIn(slot, body)

    def test_status_shape(self):
        self.start_default()
        status, _, data = http_json(self.port, "GET", "/status")
        self.assertEqual(status, 200)
        self.assertIn("pid", data)
        self.assertEqual(data["port"], self.port)
        self.assertIn("uptime_s", data)
        self.assertIn("apps", data)
        self.assertEqual(set(data["apps"].keys()), {
            "designcraft", "vectorcraft", "photocraft", "lightcraft",
            "filmcraft", "pdfcraft", "effectcraft",
        })
        entry = data["apps"]["designcraft"]
        for key in ("supported", "path", "valid", "reason", "mode", "state",
                    "app_pid", "control_port", "bridge_pid", "started_at",
                    "last_call_at", "last_error", "build_info",
                    "dirty_documents"):
            self.assertIn(key, entry)
        self.assertTrue(data["apps"]["photocraft"]["supported"])

    def test_build_info_in_status(self):
        # Path in the config is only used for display/build-info here; the dev
        # override still launches the fakes.
        with open(os.path.join(self.home, "BUILD_INFO.txt"), "w", encoding="utf-8") as fh:
            fh.write("commit abc\nbuilt now\nextra\nignored\n")
        self.start_gateway(
            window_slots=("designcraft",),
            apps={"designcraft": {"path": self.home, "mode": "window"}},
        )
        _, _, data = http_json(self.port, "GET", "/status")
        self.assertEqual(
            data["apps"]["designcraft"]["build_info"],
            "commit abc\nbuilt now\nextra",
        )


if __name__ == "__main__":
    unittest.main()
