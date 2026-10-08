"""Smoke test for the tkinter monitor: build it, feed snapshots, no mainloop."""

import os
import tkinter as tk
import unittest
from unittest import mock

import config as config_mod
import monitor
import monitor_core
from monitor_core import ActionResult

try:
    # One hidden root is created up front and reused as the class root:
    # creating ttk widgets after another root was destroyed makes Tcl print a
    # harmless "application has been destroyed" ThemeChanged error.
    _probe = tk.Tk()
    _probe.withdraw()
    TK_AVAILABLE = True
except tk.TclError:
    _probe = None
    TK_AVAILABLE = False


UNSUPPORTED = ("photocraft", "lightcraft", "filmcraft", "printcraft", "effectcraft")


def make_row(slot, supported, state, **kw):
    actions = {
        "start": False,
        "stop": False,
        "browse": True,
        "clear": bool(kw.get("path")),
        "copy_mcp": supported,
        "headless": False,
    }
    actions.update(kw.pop("actions", {}))
    return {
        "slot": slot,
        "supported": supported,
        "implemented": supported,
        "state": state,
        "mode": kw.get("mode", "window"),
        "control_port": kw.get("control_port"),
        "build_info": kw.get("build_info"),
        "path": kw.get("path", ""),
        "path_state": kw.get("path_state", "empty"),
        "path_reason": kw.get("path_reason", ""),
        "valid": kw.get("valid", supported),
        "reason": kw.get("reason", ""),
        "path_changed_restart_needed": kw.get("path_changed_restart_needed", False),
        "headless_supported": kw.get("headless_supported", False),
        "actions": actions,
    }


def down_snapshot():
    rows = [
        make_row("designcraft", True, "stopped"),
        make_row("vectorcraft", True, "disabled"),
    ]
    rows.extend(make_row(slot, False, "not implemented yet") for slot in UNSUPPORTED)
    return {
        "gateway": {"state": "stopped", "port": 7970, "pid": None,
                    "uptime_s": None, "text": "Gateway: stopped", "error": None},
        "rows": rows,
    }


def running_snapshot():
    rows = [
        make_row("designcraft", True, "running", control_port=7980,
                 build_info="Build 7", path=r"C:\apps\designcraft",
                 path_state="ok", actions={"stop": True}),
        make_row("vectorcraft", True, "stopped",
                 actions={"start": True}),
    ]
    rows.extend(make_row(slot, False, "not implemented yet") for slot in UNSUPPORTED)
    return {
        "gateway": {"state": "running", "port": 7970, "pid": 1234,
                    "uptime_s": 720,
                    "text": "Gateway: running - port 7970 - pid 1234 - up 12 min",
                    "error": None},
        "rows": rows,
    }


class FakeCore:
    """A MonitorCore stand-in; the UI only reads snapshots and config."""

    def __init__(self):
        self.home = os.getcwd()
        self.config = config_mod.Config()
        self.snap = down_snapshot()
        self.calls = []

    def snapshot(self):
        return self.snap

    def poll(self, timeout=1.5):
        return self.snap

    def start_page_url(self):
        return "http://127.0.0.1:7970/"

    def log_path(self):
        return ""

    def mcp_command(self, slot):
        return "cmd %s" % slot

    def stop_app(self, slot, force=False):
        self.calls.append(("stop_app", slot, force))
        return ActionResult(True, "stopped")

    def stop_gateway(self, force=False):
        self.calls.append(("stop_gateway", force))
        return ActionResult(True, "stopped")


@unittest.skipUnless(TK_AVAILABLE, "Tk cannot create a root")
class MonitorUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = _probe

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            cls.root.destroy()

    def setUp(self):
        for child in self.root.winfo_children():
            child.destroy()
        self.fake = FakeCore()
        self.app = monitor.MonitorApp(self.root, core=self.fake)
        self.addCleanup(self.app._stop.set)

    def test_gateway_down_widgets(self):
        self.app.render(down_snapshot())
        self.root.update()

        self.assertEqual(self.app.gw_light.cget("fg"), monitor.COLOR_GREY)
        self.assertIn("stopped", self.app.gw_text.cget("text"))
        self.assertEqual(str(self.app.btn_start_gateway["state"]), "normal")
        self.assertEqual(str(self.app.btn_stop_gateway["state"]), "disabled")

        for slot in monitor._slots():
            self.assertIn(slot, self.app.rows)

        rows = self.app.rows
        self.assertEqual(rows["designcraft"].state.cget("text"), "stopped")
        self.assertEqual(str(rows["designcraft"].btn_start["state"]), "disabled")
        self.assertEqual(rows["photocraft"].state.cget("text"), "not implemented yet")
        self.assertEqual(str(rows["photocraft"].btn_start["state"]), "disabled")
        self.assertEqual(str(rows["photocraft"].btn_stop["state"]), "disabled")

    def test_gateway_running_widgets(self):
        self.app.render(running_snapshot())
        self.root.update()

        self.assertEqual(self.app.gw_light.cget("fg"), monitor.COLOR_GREEN)
        self.assertIn("running", self.app.gw_text.cget("text"))
        self.assertEqual(str(self.app.btn_start_gateway["state"]), "disabled")
        self.assertEqual(str(self.app.btn_stop_gateway["state"]), "normal")

        design = self.app.rows["designcraft"]
        self.assertEqual(design.state.cget("text"), "running")
        self.assertEqual(design.port.cget("text"), "7980")
        self.assertEqual(design.build.cget("text"), "Build 7")
        self.assertEqual(str(design.btn_start["state"]), "disabled")
        self.assertEqual(str(design.btn_stop["state"]), "normal")
        self.assertEqual(str(self.app.rows["vectorcraft"].btn_start["state"]), "normal")
        self.assertEqual(str(self.app.rows["photocraft"].btn_stop["state"]), "disabled")

    def test_path_validation_colors(self):
        snap = running_snapshot()
        snap["rows"][0]["path_state"] = "invalid"
        snap["rows"][0]["path_reason"] = "folder not found"
        self.app.render(snap)
        self.root.update()
        design = self.app.rows["designcraft"]
        self.assertEqual(design.path_entry.cget("readonlybackground"),
                         monitor.PATH_COLORS["invalid"])
        self.assertEqual(design.path_reason.cget("text"), "folder not found")

    def test_dirty_confirm_asks_and_can_cancel(self):
        self.app.render(running_snapshot())
        self.root.update()
        result = ActionResult(
            False,
            "unsaved",
            {"kind": "dirty", "slot": "designcraft",
             "titles": ["Chapter 1"], "unknown": False},
        )
        with mock.patch.object(monitor.messagebox, "askyesno", return_value=False) as ask:
            self.app._confirm(result)
        self.assertTrue(ask.called)
        self.assertIn("Chapter 1", ask.call_args[0][1])
        self.assertEqual(self.fake.calls, [])
        self.assertIn("cancelled", self.app.status_var.get())

    def test_unknown_dirty_confirm_mentions_could_not_check(self):
        result = ActionResult(
            False,
            "unknown",
            {"kind": "unknown", "slot": "designcraft", "titles": [], "unknown": True},
        )
        with mock.patch.object(monitor.messagebox, "askyesno", return_value=False) as ask:
            self.app._confirm(result)
        self.assertIn("could not be checked", ask.call_args[0][1])


class ShortBuildTests(unittest.TestCase):
    def test_drops_the_prefix_and_shortens_long_subjects(self):
        self.assertEqual(monitor._short_build(None), "-")
        self.assertEqual(monitor._short_build("Built from commit: 465ce44 Fix"), "465ce44 Fix")
        long_line = "Built from commit: abc1234 " + "x" * 80
        out = monitor._short_build(long_line)
        self.assertLessEqual(len(out), 34)
        self.assertTrue(out.startswith("abc1234 xxx"))
        self.assertTrue(out.endswith(chr(0x2026)))


if __name__ == "__main__":
    unittest.main()
