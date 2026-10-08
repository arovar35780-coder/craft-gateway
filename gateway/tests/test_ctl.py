"""gatewayctl start/stop/status against a real detached gateway subprocess."""

import json
import os
import shutil
import subprocess
import sys
import time
import unittest

from tests.helpers import (
    GATEWAY_DIR,
    TEST_PORT_HI,
    TEST_PORT_LO,
    free_port,
    temp_home,
    write_config,
)

CTL = os.path.join(GATEWAY_DIR, "gatewayctl.py")


def ctl(home, *args, timeout=60):
    env = os.environ.copy()
    env["CRAFT_GATEWAY_HOME"] = home
    env["CRAFT_GATEWAY_CONTROL_RANGE"] = "%d-%d" % (TEST_PORT_LO, TEST_PORT_HI)
    return subprocess.run(
        [sys.executable, CTL, *args],
        cwd=GATEWAY_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class GatewayCtlTests(unittest.TestCase):
    def setUp(self):
        self.home = temp_home()
        self.port = free_port()
        write_config(self.home, self.port, window_slots=())
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        try:
            ctl(self.home, "stop", "--force", timeout=30)
        except Exception:
            pass
        pid_path = os.path.join(self.home, "gateway.pid")
        if os.path.isfile(pid_path):
            try:
                with open(pid_path, "r", encoding="utf-8") as fh:
                    pid = int(fh.read().strip())
                if pid_alive(pid):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                       capture_output=True)
                    else:
                        os.kill(pid, 9)
            except (OSError, ValueError):
                pass
        shutil.rmtree(self.home, ignore_errors=True)

    def test_status_when_down_exit_2(self):
        result = ctl(self.home, "status")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not running", result.stdout)

    def test_start_status_stop(self):
        result = ctl(self.home, "start")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("http://127.0.0.1:%d" % self.port, result.stdout)

        # A second start is refused.
        again = ctl(self.home, "start")
        self.assertEqual(again.returncode, 1)
        self.assertIn("already running", again.stdout)

        result = ctl(self.home, "status", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)
        self.assertEqual(data["port"], self.port)
        self.assertIn("apps", data)

        result = ctl(self.home, "status")
        self.assertEqual(result.returncode, 0)
        self.assertIn("gateway pid=", result.stdout)

        result = ctl(self.home, "stop")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = ctl(self.home, "status")
        self.assertEqual(result.returncode, 2)

    def test_config_commands_while_down(self):
        result = ctl(self.home, "config", "set-port", str(free_port()))
        self.assertEqual(result.returncode, 0)
        result = ctl(self.home, "config", "set-path", "designcraft",
                     r"C:\Apps\DesignCraft", "--mode", "window")
        self.assertEqual(result.returncode, 0)
        result = ctl(self.home, "config", "show")
        self.assertEqual(result.returncode, 0)
        self.assertIn("[apps.designcraft]", result.stdout)
        self.assertIn("DesignCraft", result.stdout)
        result = ctl(self.home, "config", "clear-path", "designcraft")
        self.assertEqual(result.returncode, 0)
        result = ctl(self.home, "config", "show")
        self.assertNotIn("[apps.designcraft]", result.stdout)

    def test_apps_when_down(self):
        result = ctl(self.home, "apps")
        self.assertEqual(result.returncode, 2)
        self.assertIn("not running", result.stdout)


if __name__ == "__main__":
    unittest.main()
