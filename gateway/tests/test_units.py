"""Unit tests: config round-trip, slot validation, bind refusal."""

import os
import shutil
import unittest
from unittest import mock

from tests import helpers
from tests.helpers import (
    TEST_PORT_HI,
    TEST_PORT_LO,
)

import config as config_mod
import gateway as gateway_mod
import slots as slots_mod


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = helpers.temp_home()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def test_set_path_round_trip(self):
        cfg = config_mod.load(self.home)
        config_mod.set_path(cfg, "designcraft", r"C:\Apps\DesignCraft", "window")
        config_mod.set_port(cfg, 17999)
        config_mod.save(self.home, cfg)
        again = config_mod.load(self.home)
        self.assertEqual(again.port, 17999)
        self.assertEqual(again.app("designcraft").path, r"C:\Apps\DesignCraft")
        self.assertEqual(again.app("designcraft").mode, "window")

    def test_dev_override_survives_round_trip(self):
        helpers.write_config(self.home, 17980, window_slots=("designcraft",))
        cfg = config_mod.load(self.home)
        dev = cfg.dev_for("designcraft")
        self.assertIsNotNone(dev)
        self.assertIn("{port}", dev.app_cmd)
        self.assertIn("{addr}", dev.bridge_cmd)

    def test_clear_path(self):
        cfg = config_mod.load(self.home)
        config_mod.set_path(cfg, "vectorcraft", "/tmp/vec")
        config_mod.clear_path(cfg, "vectorcraft")
        config_mod.save(self.home, cfg)
        self.assertEqual(config_mod.load(self.home).app("vectorcraft").path, "")


class SlotTests(unittest.TestCase):
    def test_disabled_slot(self):
        valid, reason = slots_mod.slot_validity("designcraft", None, None)
        self.assertFalse(valid)
        self.assertIn("no path", reason)

    def test_invalid_path(self):
        app = config_mod.AppConfig(path="/definitely/not/here", mode="window")
        valid, reason = slots_mod.slot_validity("designcraft", app, None)
        self.assertFalse(valid)
        self.assertIn("folder not found", reason)

    def test_unsupported_slot(self):
        app = config_mod.AppConfig(path="/tmp", mode="window")
        with mock.patch.dict(slots_mod.SUPPORTED, {"photocraft": False}):
            valid, reason = slots_mod.slot_validity("photocraft", app, None)
        self.assertFalse(valid)
        self.assertEqual(reason, "not implemented yet")

    def test_photocraft_gets_a_dedicated_exchange_write_root(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "ex")
            with mock.patch.dict(os.environ, {"CRAFT_EXCHANGE_DIR": target}):
                path = gateway_mod.exchange_dir()
            self.assertEqual(path, target)
            self.assertTrue(os.path.isdir(target))
        args = slots_mod.LAUNCH["photocraft"]["app_args"]
        self.assertIn("--automation-write-root", args)
        self.assertEqual(args[args.index("--automation-write-root") + 1], "{exchange}")
        # never the whole temp dir
        default = os.path.normpath(gateway_mod.exchange_dir())
        self.assertNotEqual(default, os.path.normpath(tempfile.gettempdir()))

    def test_all_slots_have_launch_spec(self):
        for slot in slots_mod.SLOTS:
            spec = slots_mod.LAUNCH[slot]
            self.assertIn("app_args", spec)
            self.assertIn("bridge_args", spec)

    def test_dev_override_valid(self):
        dev = config_mod.DevConfig(app_cmd=["x"], bridge_cmd=["y"])
        valid, reason = slots_mod.slot_validity("designcraft", None, dev)
        self.assertTrue(valid)
        self.assertEqual(reason, "dev override")

    def test_headless_not_supported(self):
        app = config_mod.AppConfig(path="/tmp", mode="headless")
        dev = config_mod.DevConfig(app_cmd=["x"], bridge_cmd=["y"])
        valid, reason = slots_mod.slot_validity("designcraft", app, dev)
        self.assertFalse(valid)
        self.assertIn("headless", reason)

    def test_build_info_missing(self):
        self.assertIsNone(slots_mod.build_info("/definitely/not/here"))

    def test_build_info_reads_three_lines(self):
        home = helpers.temp_home()
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        with open(os.path.join(home, "BUILD_INFO.txt"), "w", encoding="utf-8") as fh:
            fh.write("\ufeffone\ntwo\nthree\nfour\n")
        self.assertEqual(slots_mod.build_info(home), "one\ntwo\nthree")


class BindTests(unittest.TestCase):

    def test_loopback_allowed(self):
        gateway_mod.validate_bind_address("127.0.0.1")
        gateway_mod.validate_bind_address("localhost")

    def test_non_loopback_refused(self):
        os.environ.pop("CRAFT_GATEWAY_ALLOW_NON_LOOPBACK", None)
        with self.assertRaises(ValueError):
            gateway_mod.validate_bind_address("0.0.0.0")

    def test_non_loopback_allowed_with_override(self):
        os.environ["CRAFT_GATEWAY_ALLOW_NON_LOOPBACK"] = "1"
        try:
            gateway_mod.validate_bind_address("0.0.0.0")
        finally:
            os.environ.pop("CRAFT_GATEWAY_ALLOW_NON_LOOPBACK", None)

    def test_test_range(self):
        self.assertLess(TEST_PORT_LO, TEST_PORT_HI)


class ControlPortTests(unittest.TestCase):
    def test_production_range_and_reserved_ports(self):
        self.assertEqual(gateway_mod.DEFAULT_CONTROL_RANGE, (7971, 7999))
        self.assertEqual(gateway_mod.RESERVED_CONTROL_PORTS, {7979, 7981})

    def test_allocate_skips_excluded(self):
        port = gateway_mod.allocate_control_port(TEST_PORT_LO, TEST_PORT_HI, {TEST_PORT_LO})
        self.assertNotEqual(port, TEST_PORT_LO)
        self.assertGreaterEqual(port, TEST_PORT_LO)
        self.assertLessEqual(port, TEST_PORT_HI)


class PortReservationTests(unittest.TestCase):
    """Regression: every handed-out port used to be excluded for the life of the gateway (29 app starts used up 7971-7999)."""

    def _gateway(self, span):
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return gateway_mod.Gateway(home=tmp.name, port_override=1, control_range=(TEST_PORT_LO, TEST_PORT_LO + span - 1))

    def test_ports_come_back_after_the_reservation_expires(self):
        gw = self._gateway(3)
        old = gateway_mod.PORT_RESERVATION_SECONDS
        gateway_mod.PORT_RESERVATION_SECONDS = 0.0
        self.addCleanup(setattr, gateway_mod, "PORT_RESERVATION_SECONDS", old)
        for _ in range(12):                      # four times the size of the range
            gw._allocate_port()

    def test_two_starts_in_a_row_never_share_a_port(self):
        gw = self._gateway(5)
        ports = [gw._allocate_port() for _ in range(5)]
        self.assertEqual(len(set(ports)), 5)     # still reserved: nothing has bound them yet
        with self.assertRaises(gateway_mod.GatewayError):
            gw._allocate_port()                  # the range is genuinely exhausted within the reservation window


if __name__ == "__main__":
    unittest.main()
