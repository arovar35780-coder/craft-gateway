"""Non-GUI core for the Craft Gateway Monitor.

This module has no tkinter import so it can be imported headless and unit
tested. It reuses config.py, slots.py and the HTTP/launch code paths of
gatewayctl.py: the UI only renders the snapshot and calls these actions.
"""

import json
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import config as config_mod
import gatewayctl
import slots as slots_mod

@dataclass
class ActionResult:
    ok: bool
    message: str = ""
    # needs_confirm: {"kind": "dirty"|"unknown"|"left_running", ...}
    needs_confirm: Optional[dict] = None


@dataclass
class Row:
    slot: str
    supported: bool
    state: str
    mode: str
    control_port: Optional[int]
    build_info: Optional[str]
    path: str
    path_valid: bool
    path_state: str  # "ok" | "invalid" | "empty"
    path_reason: str
    valid: bool
    reason: str
    path_changed_restart_needed: bool
    headless_supported: bool
    actions: Dict[str, bool] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "slot": self.slot,
            "supported": self.supported,
            "implemented": self.supported,
            "state": self.state,
            "mode": self.mode,
            "control_port": self.control_port,
            "build_info": self.build_info,
            "path": self.path,
            "path_valid": self.path_valid,
            "path_state": self.path_state,
            "path_reason": self.path_reason,
            "valid": self.valid,
            "reason": self.reason,
            "path_changed_restart_needed": self.path_changed_restart_needed,
            "headless_supported": self.headless_supported,
            "actions": dict(self.actions),
        }


class MonitorCore:
    """State + actions for the monitor, independent of the GUI toolkit."""

    def __init__(self, home: Optional[str] = None, port: Optional[int] = None):
        self.home = home or config_mod.default_home()
        self._port_override = int(port) if port else None
        self.config = config_mod.load(self.home)
        self._config_mtime = self._mtime()
        self._status: Optional[dict] = None
        self._gateway_state = "stopped"
        self._last_error: Optional[str] = None
        self._gateway_port = self._port_override or self.config.port

    # -- config ------------------------------------------------------------- #

    def _mtime(self):
        try:
            return os.path.getmtime(config_mod.config_path(self.home))
        except OSError:
            return None

    def _maybe_reload_config(self):
        mtime = self._mtime()
        if mtime != self._config_mtime:
            try:
                self.config = config_mod.load(self.home)
                self._config_mtime = mtime
            except Exception:
                pass

    def reload_config(self):
        """Force a re-read of config.toml (used after writes)."""
        try:
            self.config = config_mod.load(self.home)
            self._config_mtime = self._mtime()
        except Exception:
            pass

    # -- port / polling ----------------------------------------------------- #

    def _current_port(self) -> int:
        if self._port_override is not None:
            return self._port_override
        if self._gateway_state == "running" and self._status:
            return int(self._status.get("port") or self.config.port)
        return int(self.config.port)

    def _candidate_ports(self) -> List[int]:
        if self._port_override is not None:
            return [self._port_override]
        ports = []
        if self._gateway_state == "running" and self._status:
            ports.append(int(self._status.get("port") or self.config.port))
        ports.append(int(self.config.port))
        out = []
        for p in ports:
            if p not in out:
                out.append(p)
        return out

    def _gateway_process_alive(self) -> bool:
        """True when a gateway pid is recorded and still alive.

        Windows can report a closed loopback port as a timeout instead of a
        refusal, so the pid file is the reliable "was running" signal.
        """
        try:
            return bool(gatewayctl._pid_alive(gatewayctl._read_pid(self.home)))
        except Exception:
            return False

    def poll(self, timeout: float = 1.5) -> dict:
        """GET /status (never blocks longer than timeout) and return a snapshot."""
        self._maybe_reload_config()
        last_kind = "down"
        last_message = "gateway is not running"
        for port in self._candidate_ports():
            try:
                code, raw = gatewayctl._http(
                    "GET", "/status", port, timeout=timeout
                )
            except socket.timeout as exc:
                last_kind = "timeout"
                last_message = "poll timed out: %s" % (exc or "timed out")
                continue
            except OSError as exc:
                last_kind = "down"
                last_message = "gateway is not running (%s)" % (exc or "refused")
                continue
            if code != 200:
                last_kind = "error"
                last_message = "status returned HTTP %s" % code
                continue
            try:
                data = json.loads(raw.decode("utf-8"))
            except ValueError:
                last_kind = "error"
                last_message = "invalid status JSON"
                continue
            self._status = data
            self._gateway_state = "running"
            self._last_error = None
            self._gateway_port = int(data.get("port") or port)
            return self.snapshot()
        if last_kind == "timeout" and self._gateway_process_alive():
            # Keep the last known rows but flag the gateway as errored.
            self._gateway_state = "error"
            self._last_error = last_message
        else:
            self._gateway_state = "stopped"
            self._last_error = None
            self._status = None
        return self.snapshot()

    # -- snapshot ----------------------------------------------------------- #

    def _gateway_dict(self, status: Optional[dict]) -> dict:
        state = self._gateway_state
        status = status or {}
        port = int(status.get("port") or self._current_port())
        pid = status.get("pid")
        parts = ["Gateway: %s" % state]
        if state == "running":
            parts.append("port %d" % port)
            if pid is not None:
                parts.append("pid %s" % pid)
            parts.append("up %s" % _fmt_uptime(status.get("uptime_s")))
        elif state == "error":
            parts.append(self._last_error or "unreachable")
        return {
            "state": state,
            "port": port,
            "pid": pid,
            "uptime_s": status.get("uptime_s"),
            "text": " - ".join(parts),
            "error": self._last_error,
        }

    def snapshot(self) -> dict:
        self._maybe_reload_config()
        status = self._status if self._gateway_state in ("running", "error") else None
        apps = (status or {}).get("apps", {})
        rows = [
            self._row(slot, apps.get(slot) if status else None).to_dict()
            for slot in slots_mod.SLOTS
        ]
        return {"gateway": self._gateway_dict(status), "rows": rows}

    def _row(self, slot: str, status_app: Optional[dict]) -> Row:
        cfg_app = self.config.app(slot)
        dev = self.config.dev_for(slot)
        supported = slots_mod.is_supported(slot)
        path = cfg_app.path or ""
        mode = cfg_app.mode or "window"

        if path:
            path_valid, path_reason = slots_mod.validate_path(slot, path)
            path_state = "ok" if path_valid else "invalid"
        else:
            path_valid, path_reason = False, "no path configured"
            path_state = "empty"
        if dev is not None and not path:
            # A dev override runs without an install folder.
            path_reason = "dev override"

        valid, reason = slots_mod.slot_validity(slot, cfg_app, dev)
        if not supported:
            state = "not implemented yet"
        elif status_app is not None:
            state = status_app.get("state") or "stopped"
        elif valid:
            state = "stopped"
        else:
            state = "disabled"

        build = status_app.get("build_info") if status_app else None
        if not build:
            build = slots_mod.build_info(path)
        control_port = status_app.get("control_port") if status_app else None
        path_changed = bool(
            status_app.get("path_changed_restart_needed")
        ) if status_app else False

        headless_supported = bool(slots_mod.LAUNCH.get(slot, {}).get("headless"))
        gateway_running = self._gateway_state == "running"
        busy = state in ("running", "starting")
        actions = {
            "start": gateway_running and supported and valid and not busy,
            "stop": gateway_running and state == "running",
            "browse": True,
            "clear": True,
            "copy_mcp": supported,
            "headless": supported and headless_supported and not busy,
        }
        return Row(
            slot=slot,
            supported=supported,
            state=state,
            mode=mode,
            control_port=control_port,
            build_info=build,
            path=path,
            path_valid=path_valid,
            path_state=path_state,
            path_reason=path_reason,
            valid=valid,
            reason=reason,
            path_changed_restart_needed=path_changed,
            headless_supported=headless_supported,
            actions=actions,
        )

    # -- gateway actions ---------------------------------------------------- #

    def start_gateway(self) -> ActionResult:
        ok, message = gatewayctl.start_gateway(self.home, self._current_port())
        if ok:
            self.poll()
        return ActionResult(ok, message)

    def stop_gateway(self, force: bool = False) -> ActionResult:
        """Shut the gateway down, asking first when apps are left running."""
        port = self._current_port()
        reached, report, message = gatewayctl.request_shutdown(
            self.home, port, force=force, keepalive=not force
        )
        if not reached:
            return ActionResult(False, message)
        left = (report or {}).get("left_running") or []
        if left and not force:
            return ActionResult(
                False,
                "some apps were left running",
                {
                    "kind": "left_running",
                    "apps": left,
                    "unknown": any(bool(a.get("unknown")) for a in left),
                },
            )
        self._wait_down(port)
        self.poll()
        return ActionResult(True, "gateway stopped")

    def _wait_down(self, port: int, timeout: float = 10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if gatewayctl._probe(port, timeout=0.4) is None:
                return
            time.sleep(0.25)

    # -- app actions -------------------------------------------------------- #

    def _admin(self, slot: str, action: str, force: bool = False, timeout: float = 120.0):
        """POST an admin app action. Returns (reached, obj, message)."""
        if self._gateway_state != "running":
            self.poll()
        if self._gateway_state != "running":
            return False, {}, "gateway is not running"
        port = self._current_port()
        query = "?force=1" if force else ""
        try:
            status, raw = gatewayctl._http(
                "POST",
                "/admin/apps/%s/%s%s" % (slot, action, query),
                port,
                body={},
                timeout=timeout,
            )
        except OSError as exc:
            return False, {}, "could not reach gateway: %s" % exc
        try:
            obj = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            obj = {}
        return True, obj, ""

    def start_app(self, slot: str) -> ActionResult:
        reached, obj, message = self._admin(slot, "start")
        if not reached:
            return ActionResult(False, message)
        if obj.get("ok"):
            self.poll()
            return ActionResult(True, "%s started" % slot)
        return ActionResult(False, _error_text(obj, slot))

    def stop_app(self, slot: str, force: bool = False) -> ActionResult:
        reached, obj, message = self._admin(slot, "stop", force=force)
        if not reached:
            return ActionResult(False, message)
        if obj.get("ok"):
            self.poll()
            return ActionResult(True, "%s stopped" % slot)
        dirty = obj.get("dirty_documents") or []
        if obj.get("dirty_check_failed"):
            return ActionResult(
                False,
                _error_text(obj, slot),
                {"kind": "unknown", "slot": slot, "titles": [], "unknown": True},
            )
        if dirty:
            return ActionResult(
                False,
                _error_text(obj, slot),
                {"kind": "dirty", "slot": slot, "titles": list(dirty), "unknown": False},
            )
        return ActionResult(False, _error_text(obj, slot))

    def start_all(self) -> ActionResult:
        return self._run_all("start")

    def stop_all(self) -> ActionResult:
        return self._run_all("stop")

    def _run_all(self, action: str) -> ActionResult:
        snap = self.snapshot()
        done, failed = [], []
        for row in snap["rows"]:
            if not row["supported"] or not row["valid"]:
                continue
            if action == "start" and not row["actions"]["start"]:
                continue
            if action == "stop" and not row["actions"]["stop"]:
                continue
            result = self.start_app(row["slot"]) if action == "start" else self.stop_app(row["slot"])
            if result.ok:
                done.append(row["slot"])
            else:
                failed.append(row["slot"])
        message = "%s: %s" % (action, ", ".join(done) if done else "nothing to do")
        if failed:
            message += "; failed: %s" % ", ".join(failed)
        return ActionResult(not failed, message)

    # -- config actions ----------------------------------------------------- #

    def set_path(self, slot: str, folder: str) -> ActionResult:
        valid, reason = slots_mod.validate_path(slot, folder)
        if not valid:
            return ActionResult(False, "invalid path: %s" % reason)
        ok, message = gatewayctl.set_app_path(self.home, slot, folder)
        self.reload_config()
        self.poll()
        return ActionResult(ok, message)

    def clear_path(self, slot: str) -> ActionResult:
        ok, message = gatewayctl.clear_app_path(self.home, slot)
        self.reload_config()
        self.poll()
        return ActionResult(ok, message)

    def set_mode(self, slot: str, headless: bool) -> ActionResult:
        if not slots_mod.LAUNCH.get(slot, {}).get("headless"):
            return ActionResult(False, "headless mode is not supported by this slot")
        cfg = config_mod.load(self.home)
        path = cfg.app(slot).path or ""
        mode = "headless" if headless else "window"
        ok, message = gatewayctl.set_app_path(self.home, slot, path, mode)
        self.reload_config()
        self.poll()
        return ActionResult(ok, message if ok else message)

    def set_port(self, port: int) -> ActionResult:
        try:
            port = int(port)
        except (TypeError, ValueError):
            return ActionResult(False, "port must be a number")
        if not (1024 <= port <= 65535):
            return ActionResult(False, "port must be between 1024 and 65535")
        ok, message = gatewayctl.set_gateway_port(self.home, port)
        self.reload_config()
        return ActionResult(ok, message)

    # -- helpers ------------------------------------------------------------ #

    def mcp_command(self, slot: str) -> str:
        return "claude mcp add --transport http %s http://127.0.0.1:%d/%s" % (
            slot,
            self._current_port(),
            slot,
        )

    def start_page_url(self) -> str:
        return "http://127.0.0.1:%d/" % self._current_port()

    def log_path(self) -> str:
        return gatewayctl._log_path(self.home)

    def gateway_running(self) -> bool:
        return self._gateway_state == "running"


def _error_text(obj: dict, slot: str) -> str:
    error = obj.get("error")
    if error:
        return str(error)
    return "%s: action failed" % slot


def _fmt_uptime(seconds) -> str:
    try:
        total = max(int(seconds), 0)
    except (TypeError, ValueError):
        total = 0
    if total < 60:
        return "%d s" % total
    minutes = total // 60
    if minutes < 60:
        return "%d min" % minutes
    return "%d h %d min" % (minutes // 60, minutes % 60)
