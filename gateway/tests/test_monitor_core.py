"""MonitorCore against a real test gateway (fake apps/bridges, test ports)."""

import os
import shutil
import socket
import tempfile
import time
import unittest
from unittest import mock

from tests import helpers
from tests.helpers import GatewayTestCase, free_port, write_config

import config as config_mod
import monitor_core


def make_install(slot="designcraft", build_info=None):
    """A folder that passes slots.validate_path for `slot`."""
    folder = tempfile.mkdtemp(prefix="monitor-install-")
    for name in ("%s.exe" % slot, "%s-cli.exe" % slot):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as fh:
            fh.write("")
    if build_info:
        with open(os.path.join(folder, "BUILD_INFO.txt"), "w", encoding="utf-8") as fh:
            fh.write(build_info)
    return folder


class MonitorCoreTests(GatewayTestCase):
    def core(self):
        return monitor_core.MonitorCore(home=self.home, port=self.port)

    def rows(self, snap):
        return {row["slot"]: row for row in snap["rows"]}

    # -- down / config rendering ------------------------------------------- #

    def test_snapshot_when_gateway_down(self):
        folder = make_install("designcraft", "Build 2024.1\nsecond line\n")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        write_config(
            self.home,
            self.port,
            window_slots=(),
            apps={"designcraft": {"path": folder, "mode": "window"}},
        )
        core = self.core()
        snap = core.snapshot()
        self.assertEqual(snap["gateway"]["state"], "stopped")

        rows = self.rows(snap)
        design = rows["designcraft"]
        self.assertTrue(design["path_valid"])
        self.assertEqual(design["path_state"], "ok")
        self.assertEqual(design["build_info"].splitlines()[0], "Build 2024.1")
        self.assertEqual(design["state"], "stopped")
        self.assertFalse(design["actions"]["start"])  # gateway down

        self.assertEqual(rows["vectorcraft"]["path_state"], "empty")
        self.assertFalse(rows["vectorcraft"]["actions"]["start"])

        photo = rows["photocraft"]
        self.assertTrue(photo["supported"])
        self.assertEqual(photo["path_state"], "empty")
        self.assertFalse(photo["actions"]["start"])
        self.assertFalse(photo["actions"]["stop"])
        self.assertTrue(photo["actions"]["browse"])

    def test_invalid_path_is_marked(self):
        folder = tempfile.mkdtemp(prefix="monitor-empty-")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        write_config(
            self.home,
            self.port,
            window_slots=(),
            apps={"designcraft": {"path": folder, "mode": "window"}},
        )
        core = self.core()
        row = self.rows(core.snapshot())["designcraft"]
        self.assertEqual(row["path_state"], "invalid")
        self.assertIn("missing executable", row["path_reason"])
        self.assertEqual(row["state"], "disabled")

    def test_mtime_config_reload(self):
        self.start_gateway(window_slots=())
        core = self.core()
        core.poll()
        folder = make_install("designcraft")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)

        cfg = config_mod.load(self.home)
        config_mod.set_path(cfg, "designcraft", folder)
        config_mod.save(self.home, cfg)
        # Make the mtime definitely differ from the cached value.
        path = config_mod.config_path(self.home)
        stat = os.stat(path)
        os.utime(path, (stat.st_atime, stat.st_mtime + 5))

        row = self.rows(core.snapshot())["designcraft"]
        self.assertEqual(row["path"], folder)
        self.assertEqual(row["path_state"], "ok")

    # -- gateway lifecycle / states ---------------------------------------- #

    def test_start_gateway_action(self):
        write_config(self.home, self.port, window_slots=())
        core = self.core()
        self.assertEqual(core.poll()["gateway"]["state"], "stopped")
        result = core.start_gateway()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(core.poll()["gateway"]["state"], "running")
        result = core.stop_gateway()
        self.assertTrue(result.ok, result.message)
        self.assertEqual(core.poll()["gateway"]["state"], "stopped")

    def test_enable_rules_and_app_lifecycle(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()

        rows = self.rows(core.snapshot())
        design = rows["designcraft"]
        self.assertEqual(design["state"], "stopped")
        self.assertTrue(design["actions"]["start"])
        self.assertFalse(design["actions"]["stop"])
        self.assertTrue(design["actions"]["browse"])
        self.assertTrue(design["actions"]["copy_mcp"])

        result = core.start_app("designcraft")
        self.assertTrue(result.ok, result.message)
        design = self.rows(core.poll())["designcraft"]
        self.assertEqual(design["state"], "running")
        self.assertIsNotNone(design["control_port"])
        self.assertFalse(design["actions"]["start"])
        self.assertTrue(design["actions"]["stop"])

        result = core.stop_app("designcraft")
        self.assertTrue(result.ok, result.message)
        design = self.rows(core.poll())["designcraft"]
        self.assertEqual(design["state"], "stopped")

    # -- dirty guard -------------------------------------------------------- #

    def test_dirty_stop_confirm_then_force(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()
        self.assertTrue(core.start_app("designcraft").ok)
        helpers.set_state(self.home, [{"title": "Chapter 1", "dirty": True}])

        result = core.stop_app("designcraft")
        self.assertFalse(result.ok)
        self.assertIsNotNone(result.needs_confirm)
        self.assertEqual(result.needs_confirm["kind"], "dirty")
        self.assertEqual(result.needs_confirm["titles"], ["Chapter 1"])
        self.assertEqual(result.needs_confirm["slot"], "designcraft")

        result = core.stop_app("designcraft", force=True)
        self.assertTrue(result.ok, result.message)
        design = self.rows(core.poll())["designcraft"]
        self.assertEqual(design["state"], "stopped")

    def test_failed_dirty_check_confirm(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()
        self.assertTrue(core.start_app("designcraft").ok)
        helpers.set_inspect_error(self.home, True)

        result = core.stop_app("designcraft")
        self.assertFalse(result.ok)
        self.assertEqual(result.needs_confirm["kind"], "unknown")
        self.assertTrue(result.needs_confirm["unknown"])

    def test_gateway_shutdown_with_left_running_app(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()
        self.assertTrue(core.start_app("designcraft").ok)
        helpers.set_state(self.home, [{"title": "Unsaved", "dirty": True}])

        result = core.stop_gateway()
        self.assertFalse(result.ok)
        self.assertEqual(result.needs_confirm["kind"], "left_running")
        apps = result.needs_confirm["apps"]
        self.assertEqual(apps[0]["slot"], "designcraft")
        self.assertEqual(apps[0]["titles"], ["Unsaved"])
        # keepalive kept the gateway up so the force stop can still reach it.
        self.assertEqual(core.poll()["gateway"]["state"], "running")

        result = core.stop_gateway(force=True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(core.poll()["gateway"]["state"], "stopped")

    # -- config actions ----------------------------------------------------- #

    def test_set_path_invalid_nothing_saved_then_valid(self):
        self.start_gateway(window_slots=())
        core = self.core()
        core.poll()

        result = core.set_path("designcraft", r"C:\definitely\missing\folder")
        self.assertFalse(result.ok)
        self.assertIn("invalid path", result.message)
        self.assertEqual(config_mod.load(self.home).app("designcraft").path, "")

        folder = make_install("designcraft")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        result = core.set_path("designcraft", folder)
        self.assertTrue(result.ok, result.message)
        cfg = config_mod.load(self.home)
        self.assertEqual(cfg.app("designcraft").path, folder)
        row = self.rows(core.poll())["designcraft"]
        self.assertTrue(row["valid"])

    def test_clear_path(self):
        folder = make_install("designcraft")
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        write_config(
            self.home,
            self.port,
            window_slots=(),
            apps={"designcraft": {"path": folder}},
        )
        core = self.core()
        result = core.clear_path("designcraft")
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config_mod.load(self.home).app("designcraft").path, "")

    def test_headless_toggle_writes_mode(self):
        self.start_gateway(window_slots=())
        core = self.core()
        core.poll()

        result = core.set_mode("vectorcraft", True)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config_mod.load(self.home).app("vectorcraft").mode, "headless")
        row = self.rows(core.snapshot())["vectorcraft"]
        self.assertEqual(row["mode"], "headless")
        self.assertTrue(row["headless_supported"])

        self.assertTrue(core.set_mode("vectorcraft", False).ok)
        self.assertEqual(config_mod.load(self.home).app("vectorcraft").mode, "window")

        # designcraft cannot run headless.
        self.assertFalse(core.set_mode("designcraft", True).ok)

    def test_set_port_validation(self):
        self.start_gateway(window_slots=())
        core = self.core()
        core.poll()
        self.assertFalse(core.set_port(80).ok)
        self.assertFalse(core.set_port(70000).ok)
        new_port = free_port()
        result = core.set_port(new_port)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(config_mod.load(self.home).port, new_port)

    # -- unsupported / command / poll error --------------------------------- #

    def test_unsupported_slots_have_no_start_stop(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()
        rows = self.rows(core.snapshot())
        for slot in ("photocraft", "lightcraft", "filmcraft", "pdfcraft", "effectcraft"):
            row = rows[slot]
            self.assertTrue(row["supported"])
            self.assertEqual(row["path_state"], "empty")
            self.assertFalse(row["actions"]["start"])
            self.assertFalse(row["actions"]["stop"])

    def test_mcp_command(self):
        self.start_gateway(window_slots=())
        core = self.core()
        core.poll()
        self.assertEqual(
            core.mcp_command("designcraft"),
            "claude mcp add --transport http designcraft "
            "http://127.0.0.1:%d/designcraft" % self.port,
        )

    def test_poll_timeout_keeps_rows_with_error_marker(self):
        self.start_gateway(window_slots=("designcraft",))
        core = self.core()
        core.poll()
        self.assertEqual(core.snapshot()["gateway"]["state"], "running")

        with mock.patch.object(
            monitor_core.gatewayctl, "_http", side_effect=socket.timeout("timed out")
        ):
            snap = core.poll(timeout=0.1)

        self.assertEqual(snap["gateway"]["state"], "error")
        self.assertTrue(snap["gateway"]["error"])
        row = self.rows(snap)["designcraft"]
        self.assertEqual(row["state"], "stopped")
        self.assertEqual(len(snap["rows"]), 7)


class FormatHelperTests(unittest.TestCase):
    def test_uptime_uses_seconds_under_a_minute(self):
        self.assertEqual(monitor_core._fmt_uptime(5), "5 s")
        self.assertEqual(monitor_core._fmt_uptime(59), "59 s")
        self.assertEqual(monitor_core._fmt_uptime(61), "1 min")
        self.assertEqual(monitor_core._fmt_uptime(3 * 3600 + 120), "3 h 2 min")
        self.assertEqual(monitor_core._fmt_uptime(None), "0 s")


if __name__ == "__main__":
    unittest.main()
