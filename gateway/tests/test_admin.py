"""Admin behaviour: dirty guard, shutdown policy, re-adoption, restart, reload."""

import os
import shutil
import time
import unittest

from tests import helpers
from tests.helpers import (
    GatewayFixture,
    GatewayTestCase,
    free_port,
    http_json,
    mcp,
    read_bridge_log,
    temp_home,
    write_config,
)

import config as config_mod
import gateway as gateway_mod


class DirtyGuardTests(GatewayTestCase):
    def _running_app(self):
        self.start_default()
        status, _, data = mcp(self.port, "designcraft", helpers.initialize_message(1))
        self.assertIn("result", data)
        _, _, status_obj = http_json(self.port, "GET", "/status")
        return status_obj["apps"]["designcraft"]

    def test_stop_refuses_dirty_then_force(self):
        info = self._running_app()
        helpers.set_state(self.home, [{"title": "Chapter 1", "dirty": True}])

        status, _, data = http_json(
            self.port, "POST", "/admin/apps/designcraft/stop", body={}
        )
        self.assertEqual(status, 200)
        self.assertFalse(data["ok"])
        self.assertEqual(data["dirty_documents"], ["Chapter 1"])
        _, _, status_obj = http_json(self.port, "GET", "/status")
        self.assertEqual(status_obj["apps"]["designcraft"]["state"], "running")

        status, _, data = http_json(
            self.port, "POST", "/admin/apps/designcraft/stop?force=1", body={}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        _, _, status_obj = http_json(self.port, "GET", "/status")
        self.assertNotEqual(status_obj["apps"]["designcraft"]["state"], "running")

    def test_dirty_documents_in_status(self):
        self._running_app()
        helpers.set_state(self.home, [{"title": "Dirty", "dirty": True},
                                      {"title": "Clean", "dirty": False}])
        status, _, data = http_json(self.port, "GET", "/status?dirty=1")
        self.assertEqual(data["apps"]["designcraft"]["dirty_documents"], ["Dirty"])


class UnknownDirtyStateTests(GatewayTestCase):
    """If the app cannot be asked about unsaved documents, treat that as unsafe."""

    def test_stop_refuses_when_the_check_fails_then_force(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        helpers.set_inspect_error(self.home)
        _, _, status_obj = http_json(self.port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]

        status, _, data = http_json(self.port, "POST", "/admin/apps/designcraft/stop", body={})
        self.assertFalse(data["ok"])
        self.assertTrue(data["dirty_check_failed"])
        self.assertIn("could not check", data["error"])
        self.assertTrue(gateway_mod.pid_alive(app_pid))

        status, _, data = http_json(self.port, "POST", "/admin/apps/designcraft/stop?force=1", body={})
        self.assertTrue(data["ok"])

    def test_shutdown_leaves_the_app_running_when_the_check_fails(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        helpers.set_inspect_error(self.home)
        _, _, status_obj = http_json(self.port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]

        status, _, report = http_json(self.port, "POST", "/admin/shutdown", body={})
        self.assertEqual(status, 200)
        self.assertEqual(report["stopped_apps"], [])
        self.assertEqual(report["left_running"][0]["slot"], "designcraft")
        self.assertTrue(report["left_running"][0]["unknown"])
        self.assertTrue(gateway_mod.pid_alive(app_pid))
        self.addCleanup(gateway_mod.terminate_pid, app_pid, True)


class ShutdownTests(GatewayTestCase):
    def test_shutdown_leaves_dirty_app_running(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        helpers.set_state(self.home, [{"title": "Unsaved", "dirty": True}])
        _, _, status_obj = http_json(self.port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]
        self.assertTrue(gateway_mod.pid_alive(app_pid))

        status, _, report = http_json(self.port, "POST", "/admin/shutdown", body={})
        self.assertEqual(status, 200)
        self.assertEqual(len(report["left_running"]), 1)
        self.assertEqual(report["left_running"][0]["slot"], "designcraft")
        self.assertEqual(report["left_running"][0]["titles"], ["Unsaved"])
        self.assertTrue(gateway_mod.pid_alive(app_pid))
        self.addCleanup(gateway_mod.terminate_pid, app_pid, True)

    def test_shutdown_force_stops_dirty_app(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        helpers.set_state(self.home, [{"title": "Unsaved", "dirty": True}])
        _, _, status_obj = http_json(self.port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]

        status, _, report = http_json(
            self.port, "POST", "/admin/shutdown?force=1", body={}
        )
        self.assertEqual(status, 200)
        self.assertIn("designcraft", report["stopped_apps"])
        deadline = time.time() + 10
        while time.time() < deadline and gateway_mod.pid_alive(app_pid):
            time.sleep(0.1)
        self.assertFalse(gateway_mod.pid_alive(app_pid))


class ReAdoptionTests(unittest.TestCase):
    def test_readopt_running_app(self):
        home = temp_home()
        port = free_port()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        write_config(home, port, window_slots=("designcraft",))

        g1 = GatewayFixture(home)
        g1.start()
        self.addCleanup(g1.cleanup)
        mcp(port, "designcraft", helpers.initialize_message(1))
        _, _, status_obj = http_json(port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]
        control_port = status_obj["apps"]["designcraft"]["control_port"]
        self.assertTrue(gateway_mod.pid_alive(app_pid))

        # Abandon the gateway but leave the app running.
        g1.gateway.stop_server()
        g1.thread.join(timeout=5)
        self.assertTrue(gateway_mod.pid_alive(app_pid))
        self.assertTrue(os.path.isfile(os.path.join(home, "state.json")))

        g2 = GatewayFixture(home)
        g2.start()
        self.addCleanup(g2.cleanup)
        _, _, status_obj = http_json(port, "GET", "/status")
        info = status_obj["apps"]["designcraft"]
        self.assertEqual(info["state"], "running")
        self.assertEqual(info["app_pid"], app_pid)
        self.assertEqual(info["control_port"], control_port)

    def test_stale_record_dropped(self):
        home = temp_home()
        port = free_port()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        write_config(home, port, window_slots=("designcraft",))
        with open(os.path.join(home, "state.json"), "w", encoding="utf-8") as fh:
            fh.write('{"designcraft": {"app_pid": 999999, "control_port": 1, '
                     '"exe": "nope", "path": ""}}')
        g = GatewayFixture(home)
        g.start()
        self.addCleanup(g.cleanup)
        _, _, status_obj = http_json(port, "GET", "/status")
        self.assertNotEqual(status_obj["apps"]["designcraft"]["state"], "running")
        with open(os.path.join(home, "state.json"), "r", encoding="utf-8") as fh:
            self.assertNotIn("999999", fh.read())


class RestartTests(GatewayTestCase):
    def test_bridge_crash_is_transparent(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        _, _, status_obj = http_json(self.port, "GET", "/status")
        first_bridge = status_obj["apps"]["designcraft"]["bridge_pid"]

        status, _, data = mcp(
            self.port,
            "designcraft",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
             "params": {"name": "crash", "arguments": {}}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["error"]["code"], -32000)

        status, _, data = mcp(
            self.port,
            "designcraft",
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["tools"][0]["name"], "echo")
        _, _, status_obj = http_json(self.port, "GET", "/status")
        self.assertNotEqual(status_obj["apps"]["designcraft"]["bridge_pid"], first_bridge)


class AppClosedByUserTests(GatewayTestCase):
    """The user closes the app window: the gateway must notice and not keep a stale bridge."""

    def _wait_status(self, predicate, timeout=10.0):
        deadline = time.time() + timeout
        info = None
        while time.time() < deadline:
            _, _, status_obj = http_json(self.port, "GET", "/status")
            info = status_obj["apps"]["designcraft"]
            if predicate(info):
                return info
            time.sleep(0.3)
        self.fail("status did not change as expected: %r" % (info,))

    def test_closed_app_is_noticed(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        _, _, status_obj = http_json(self.port, "GET", "/status")
        app_pid = status_obj["apps"]["designcraft"]["app_pid"]
        self.assertEqual(status_obj["apps"]["designcraft"]["state"], "running")

        gateway_mod.terminate_pid(app_pid, True)
        # The state flips at once; the stale bridge is removed by the next liveness check (every 2 s).
        info = self._wait_status(lambda i: i["state"] != "running" and i["bridge_pid"] is None)
        self.assertEqual(info["state"], "stopped")
        self.assertIsNone(info["app_pid"])

    def test_request_after_close_starts_a_fresh_app_and_bridge(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))
        _, _, status_obj = http_json(self.port, "GET", "/status")
        old = status_obj["apps"]["designcraft"]

        gateway_mod.terminate_pid(old["app_pid"], True)
        status, _, data = mcp(
            self.port, "designcraft",
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["tools"][0]["name"], "echo")
        _, _, status_obj = http_json(self.port, "GET", "/status")
        new = status_obj["apps"]["designcraft"]
        self.assertEqual(new["state"], "running")
        self.assertNotEqual(new["app_pid"], old["app_pid"])
        self.assertNotEqual(new["bridge_pid"], old["bridge_pid"])
        self.assertNotEqual(new["control_port"], old["control_port"])


class ReloadTests(GatewayTestCase):
    def test_path_changed_restart_needed_on_reload(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))

        cfg = config_mod.load(self.home)
        config_mod.set_path(cfg, "designcraft", "/a/different/place", "window")
        config_mod.save(self.home, cfg)
        status, _, data = http_json(self.port, "POST", "/admin/reload", body={})
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])

        _, _, status_obj = http_json(self.port, "GET", "/status")
        info = status_obj["apps"]["designcraft"]
        self.assertTrue(info["path_changed_restart_needed"])
        self.assertEqual(info["state"], "running")

    def test_mtime_watcher_reloads(self):
        self.start_default()
        mcp(self.port, "designcraft", helpers.initialize_message(1))

        cfg = config_mod.load(self.home)
        config_mod.set_path(cfg, "designcraft", "/watched/change", "window")
        # Change the file directly; the 2 s watcher should pick it up.
        config_mod.save(self.home, cfg)
        deadline = time.time() + 8
        while time.time() < deadline:
            _, _, status_obj = http_json(self.port, "GET", "/status")
            if status_obj["apps"]["designcraft"]["path_changed_restart_needed"]:
                return
            time.sleep(0.4)
        self.fail("watcher did not reload within 8s")


if __name__ == "__main__":
    unittest.main()
