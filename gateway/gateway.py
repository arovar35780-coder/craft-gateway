"""craft-gateway: one loopback HTTP port fronting the apps' stdio MCP bridges.

Standard library only. The gateway owns the processes it starts (and those it
re-adopts from state.json); it never touches apps the user launched by hand.
Executable paths come only from config.toml, never from HTTP.
"""

import argparse
import html
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from urllib.parse import urlsplit

import config as config_mod
import slots as slots_mod

LOG = logging.getLogger("craft-gateway")

IS_WINDOWS = os.name == "nt"
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0) if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0
)

# Apps the gateway starts use the first free port here. 7979/7981 belong to the
# user's manually started apps, so they are never handed out.
DEFAULT_CONTROL_RANGE = (7971, 7999)
RESERVED_CONTROL_PORTS = {7979, 7981}
PORT_RESERVATION_SECONDS = 120.0   # how long a port just handed to a starting app is kept away from other starts

STATUS_STATES = ("disabled", "stopped", "starting", "running", "failed")


class GatewayError(Exception):
    """A failure with an actionable message for the HTTP/MCP surface."""


class BridgeError(GatewayError):
    pass


class DirtyError(GatewayError):
    def __init__(self, titles, unknown=False):
        if unknown:
            super().__init__("could not check the app for unsaved documents (it did not answer); use force to stop it anyway")
        else:
            super().__init__("app has unsaved documents: %s" % ", ".join(titles))
        self.titles = list(titles)
        self.unknown = unknown


# --------------------------------------------------------------------------- #
# small OS helpers
# --------------------------------------------------------------------------- #


def pid_alive(pid) -> bool:
    if not pid or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def process_image_path(pid):
    """Best-effort full image path of a running pid, or None."""
    if not pid:
        return None
    if IS_WINDOWS:
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return None
        try:
            size = wintypes.DWORD(4096)
            buf = ctypes.create_unicode_buffer(size.value)
            ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
            return buf.value if ok else None
        finally:
            kernel32.CloseHandle(handle)
    try:
        return os.readlink("/proc/%d/exe" % int(pid))
    except OSError:
        return None


def terminate_pid(pid, force=False):
    if not pid or not pid_alive(pid):
        return
    try:
        if IS_WINDOWS:
            os.kill(int(pid), signal.SIGTERM)
        else:
            os.kill(int(pid), signal.SIGKILL if force else signal.SIGTERM)
    except (OSError, ValueError):
        pass


def control_call(port, method, params=None, timeout=5.0):
    """One JSON-line request to a running app's control channel."""
    msg = {"id": 1, "method": method, "params": params or {}}
    with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout) as sock:
        sock.settimeout(timeout)
        sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(65536)
            if not chunk:
                break
            buf += chunk
    line = buf.split(b"\n", 1)[0]
    if not line:
        raise GatewayError("control channel closed without a reply")
    reply = json.loads(line.decode("utf-8", "replace"))
    if not reply.get("ok", True):
        raise GatewayError(str(reply.get("error", "control call failed")))
    return reply


def exchange_dir():
    """Scratch folder apps may write exported files to (their automation write root).

    A dedicated subfolder of the system temp dir, never the temp dir itself: the root bounds
    what an unauthenticated loopback client can make an app write.
    """
    path = os.environ.get("CRAFT_EXCHANGE_DIR") or os.path.join(tempfile.gettempdir(), "craft-exchange")
    os.makedirs(path, exist_ok=True)
    return path


def _dig(value, path):
    """Follow a key path through nested dicts; None when anything is missing."""
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def control_port_open(port, timeout=0.5) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def wait_control_port(port, timeout=60.0, poll=0.3) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if control_port_open(port):
            return True
        time.sleep(poll)
    return control_port_open(port)


def allocate_control_port(lo, hi, excluded):
    for port in range(lo, hi + 1):
        if port in excluded:
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    raise GatewayError("no free control port in %d..%d" % (lo, hi))


def parse_control_range() -> tuple:
    raw = os.environ.get("CRAFT_GATEWAY_CONTROL_RANGE", "").strip()
    if not raw:
        return DEFAULT_CONTROL_RANGE
    try:
        if "-" in raw:
            lo, hi = raw.split("-", 1)
            return (int(lo), int(hi))
    except ValueError:
        pass
    return DEFAULT_CONTROL_RANGE


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# logging
# --------------------------------------------------------------------------- #


def setup_logging(home, level=logging.INFO):
    os.makedirs(home, exist_ok=True)
    logger = logging.getLogger("craft-gateway")
    logger.setLevel(logging.DEBUG)
    logger.handlers[:] = []
    handler = RotatingFileHandler(
        os.path.join(home, "gateway.log"),
        maxBytes=1_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setLevel(level)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _summarize(message: dict) -> str:
    """Log method/id only; never tool args or results."""
    kind = "reply" if ("result" in message or "error" in message) else "message"
    method = message.get("method", "")
    mid = message.get("id", "")
    return "%s method=%s id=%s" % (kind, method, mid)


# --------------------------------------------------------------------------- #
# bridge
# --------------------------------------------------------------------------- #


class _Waiter:
    __slots__ = ("event", "msg", "orig")

    def __init__(self, orig):
        self.event = threading.Event()
        self.msg = None
        self.orig = orig


class Bridge:
    """A stdio MCP bridge process plus gateway<->client id remapping."""

    _INIT_PARAMS = {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "craft-gateway", "version": "1.0"},
    }

    def __init__(self, slot, proc, log):
        self.slot = slot
        self.proc = proc
        self.log = log
        self._write_lock = threading.Lock()
        self._pending = {}
        self._pending_lock = threading.Lock()
        self._counter = 0
        self._init_lock = threading.Lock()
        self.initialized = False
        self.init_result = None
        self.sent_initialized = False
        self.alive = True
        self.death_reason = None
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._pump = threading.Thread(
            target=_pump_stream, args=(self, proc.stderr, "stderr"), daemon=True
        )

    def start(self):
        # stderr pump plus the stdout reader loop.
        self._pump.start()
        self._reader.start()

    def _next_id(self):
        self._counter += 1
        return "gw-%d" % self._counter

    def _send(self, message):
        data = json.dumps(message, separators=(",", ":")) + "\n"
        with self._write_lock:
            if not self.alive or self.proc.stdin is None:
                raise BridgeError("bridge is not running")
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as exc:
                self._mark_dead("stdin write failed: %s" % exc)
                raise BridgeError("bridge died: %s" % exc)

    def _send_no_raise(self, message):
        try:
            self._send(message)
        except BridgeError:
            self.log.debug("bridge[%s] dropped notification after death", self.slot)

    def _call(self, message):
        gw_id = self._next_id()
        waiter = _Waiter(message.get("id"))
        outgoing = dict(message)
        outgoing["id"] = gw_id
        with self._pending_lock:
            self._pending[gw_id] = waiter
        try:
            self._send(outgoing)
        except BridgeError:
            with self._pending_lock:
                self._pending.pop(gw_id, None)
            raise
        waiter.event.wait()
        reply = waiter.msg or {
            "jsonrpc": "2.0",
            "id": waiter.orig,
            "error": {"code": -32000, "message": "bridge died"},
        }
        reply = dict(reply)
        reply["id"] = waiter.orig
        return reply

    def initialize(self, message):
        with self._init_lock:
            if self.initialized and self.init_result is not None:
                return {
                    "jsonrpc": "2.0",
                    "id": message.get("id"),
                    "result": self.init_result,
                }
            reply = self._call(message)
            if "result" in reply:
                self.init_result = reply["result"]
                self.initialized = True
            return reply

    def client_initialized(self):
        with self._init_lock:
            if self.sent_initialized:
                return  # swallow duplicates after the first
            self._send_no_raise(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )
            self.sent_initialized = True
            self.initialized = True

    def _ensure_initialized(self):
        with self._init_lock:
            if self.initialized:
                return
            msg = {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "initialize",
                "params": dict(self._INIT_PARAMS),
            }
            reply = self._call(msg)
            if "result" in reply:
                self.init_result = reply["result"]
            else:
                err = reply.get("error", {})
                raise BridgeError("initialize failed: %s" % err.get("message", err))
            self.initialized = True
            self.sent_initialized = True
            self._send_no_raise(
                {"jsonrpc": "2.0", "method": "notifications/initialized"}
            )

    def call(self, message):
        self._ensure_initialized()
        return self._call(message)

    def notify(self, message):
        self._send_no_raise(message)

    def _read_loop(self):
        stream = self.proc.stdout
        try:
            for line in stream:
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except ValueError:
                    self.log.debug("bridge[%s] non-json stdout", self.slot)
                    continue
                if not isinstance(message, dict):
                    continue
                self.log.debug("bridge[%s] <- %s", self.slot, _summarize(message))
                mid = message.get("id")
                if "method" not in message and mid is not None:
                    with self._pending_lock:
                        waiter = self._pending.pop(mid, None)
                    if waiter is not None:
                        waiter.msg = message
                        waiter.event.set()
                    else:
                        self.log.debug("bridge[%s] unmatched reply", self.slot)
                # server-initiated notifications/requests: only logged (debug)
        except (ValueError, OSError):
            pass
        finally:
            self._mark_dead("stdout closed")

    def _mark_dead(self, reason):
        if not self.alive:
            return
        self.alive = False
        self.death_reason = reason
        self.log.debug("bridge[%s] dead: %s", self.slot, reason)
        with self._pending_lock:
            waiters = list(self._pending.values())
            self._pending.clear()
        for waiter in waiters:
            waiter.msg = {
                "jsonrpc": "2.0",
                "id": waiter.orig,
                "error": {"code": -32000, "message": "bridge exited: %s" % reason},
            }
            waiter.event.set()

    def stop(self, timeout=5.0):
        self.alive = False
        try:
            if self.proc.stdin:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
        finally:
            self._mark_dead("stopped")
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=timeout)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    self.proc.kill()
                    self.proc.wait(timeout=timeout)
                except (subprocess.TimeoutExpired, OSError):
                    pass
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass


def _pump_stream(bridge, stream, label):
    if stream is None:
        return
    try:
        for line in stream:
            line = line.rstrip("\r\n")
            if not line:
                continue
            bridge.log.debug("bridge[%s] %s: %s", bridge.slot, label, line[:500])
    except (ValueError, OSError):
        pass


# --------------------------------------------------------------------------- #
# slot state
# --------------------------------------------------------------------------- #


class SlotState:
    def __init__(self, slot):
        self.slot = slot
        self.lock = threading.RLock()
        self.app_proc = None
        self.app_pid = None
        self.app_owned = False
        self.control_port = None
        self.app_started_at = None
        self.bridge = None
        self.started_path = None
        self.path_changed = False
        self.last_call_at = None
        self.last_error = None

    @property
    def bridge_pid(self):
        if self.bridge is not None and self.bridge.alive and self.bridge.proc.poll() is None:
            return self.bridge.proc.pid
        return None


# --------------------------------------------------------------------------- #
# gateway
# --------------------------------------------------------------------------- #


class Gateway:
    def __init__(self, home=None, port_override=None, bind_override=None,
                 control_range=None):
        self.home = home or config_mod.default_home()
        self.config = config_mod.load(self.home)
        self.port = int(port_override or self.config.port or config_mod.DEFAULT_PORT)
        self.bind = bind_override or self.config.bind or "127.0.0.1"
        self._config_mtime = self._mtime()
        self._control_range = control_range or parse_control_range()
        self._excluded = set(RESERVED_CONTROL_PORTS)   # never handed out
        self._reserved = {}                            # port -> time handed out (short-lived)
        self._port_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._stop = threading.Event()
        self._watcher = None
        self.server = None
        self.session_id = str(uuid.uuid4())
        self._started_at = now_iso()
        self.slots = {slot: SlotState(slot) for slot in slots_mod.SLOTS}
        self._adopt_state()

    # -- lifecycle ---------------------------------------------------------- #

    def start(self):
        validate_bind_address(self.bind)
        self.server = ThreadingHTTPServer((self.bind, self.port), Handler)
        self.server.daemon_threads = True
        self.server.gateway = self
        self._write_pid()
        self._write_state()
        self._watcher = threading.Thread(target=self._watch_config, daemon=True)
        self._watcher.start()
        LOG.info("gateway listening on http://%s:%d", self.bind, self.port)

    def serve_forever(self):
        if self.server is None:
            self.start()
        try:
            self.server.serve_forever(poll_interval=0.2)
        finally:
            self.stop_server()

    def start_in_thread(self):
        self.start()
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
        thread.start()
        return thread

    def stop_server(self):
        self._stop.set()
        if self.server is not None:
            try:
                self.server.shutdown()
            except Exception:
                pass
            try:
                self.server.server_close()
            except Exception:
                pass
            self.server = None
        try:
            if os.path.isfile(self._pid_path()):
                os.remove(self._pid_path())
        except OSError:
            pass

    # -- paths -------------------------------------------------------------- #

    def _pid_path(self):
        return os.path.join(self.home, "gateway.pid")

    def _state_path(self):
        return os.path.join(self.home, "state.json")

    def _mtime(self):
        try:
            return os.path.getmtime(config_mod.config_path(self.home))
        except OSError:
            return None

    def _write_pid(self):
        os.makedirs(self.home, exist_ok=True)
        with open(self._pid_path(), "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))

    # -- config / re-adoption ---------------------------------------------- #

    def reload_config(self):
        self.config = config_mod.load(self.home)
        self._config_mtime = self._mtime()
        changed = []
        for slot, state in self.slots.items():
            app = self.config.app(slot)
            if state.app_pid and state.app_owned:
                if (state.started_path or "") != (app.path or ""):
                    state.path_changed = True
                    changed.append(slot)
        LOG.info("config reloaded; path changed for running apps: %s",
                 ", ".join(changed) or "none")
        return {"ok": True, "path_changed_restart_needed": changed}

    def _watch_config(self):
        while not self._stop.wait(2.0):
            if self._mtime() != self._config_mtime:
                try:
                    self.reload_config()
                except Exception as exc:  # never kill the watcher
                    LOG.warning("config reload failed: %s", exc)
            try:
                self._reap_dead_apps()
            except Exception as exc:
                LOG.warning("app liveness check failed: %s", exc)

    def _reap_dead_apps(self):
        """An app the user closed by hand: log it and drop its bridge, which still points at the dead control port."""
        for slot, state in self.slots.items():
            if not state.lock.acquire(blocking=False):
                continue  # a start or stop is in progress for this slot
            try:
                if not state.app_pid or self._effective_mode(slot) == "headless" or pid_alive(state.app_pid):
                    continue
                code = state.app_proc.poll() if state.app_proc is not None else None
                LOG.info("%s app exited (pid=%s%s)", slot, state.app_pid, "" if code is None else " code=%s" % code)
                self._stop_bridge(state)
                self._forget_app(state)
                self._write_state()
            finally:
                state.lock.release()

    def _adopt_state(self):
        path = self._state_path()
        if not os.path.isfile(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as fh:
                recorded = json.load(fh)
        except (OSError, ValueError):
            return
        if not isinstance(recorded, dict):
            return
        for slot, entry in recorded.items():
            if slot not in self.slots or not isinstance(entry, dict):
                continue
            pid = entry.get("app_pid")
            port = entry.get("control_port")
            exe = entry.get("exe") or ""
            state = self.slots[slot]
            if not pid_alive(pid):
                continue
            image = process_image_path(pid)
            if image and exe and os.path.normcase(image) != os.path.normcase(exe):
                continue
            if not control_port_open(port):
                continue
            state.app_pid = pid
            state.control_port = port
            state.app_owned = True
            state.started_path = entry.get("path")
            state.app_started_at = entry.get("started_at")
            LOG.info("re-adopted %s app pid=%s control=%s", slot, pid, port)
        # Drop records we did not adopt.
        self._write_state()

    def _write_state(self):
        data = {}
        for slot, state in self.slots.items():
            if state.app_pid:
                app = self.config.app(slot)
                if state.app_proc is not None and state.app_proc.poll() is not None:
                    state.app_pid = None
                    state.app_proc = None
                    continue
                data[slot] = {
                    "app_pid": state.app_pid,
                    "control_port": state.control_port,
                    "exe": self._app_exe(slot),
                    "path": state.started_path if state.started_path is not None else (app.path or ""),
                    "started_at": state.app_started_at,
                }
        try:
            os.makedirs(self.home, exist_ok=True)
            with self._state_lock:
                tmp = self._state_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, indent=2)
                os.replace(tmp, self._state_path())
        except OSError as exc:
            LOG.warning("could not write state.json: %s", exc)

    # -- launch specs ------------------------------------------------------- #

    def _effective_mode(self, slot):
        app = self.config.app(slot)
        return app.mode or "window"

    def _subst(self, value, port):
        if "{exchange}" in value:
            value = value.replace("{exchange}", exchange_dir())
        return (
            value.replace("{port}", str(port))
            .replace("{addr}", "127.0.0.1:%d" % port)
            .replace("{home}", self.home)
        )

    def _app_exe(self, slot):
        dev = self.config.dev_for(slot)
        if dev and dev.app_cmd:
            return dev.app_cmd[0]
        app = self.config.app(slot)
        if not app.path:
            return ""
        return os.path.join(app.path, slots_mod.app_exe(slot))

    def _bridge_exe(self, slot):
        dev = self.config.dev_for(slot)
        if dev and dev.bridge_cmd:
            return dev.bridge_cmd[0]
        app = self.config.app(slot)
        if not app.path:
            return ""
        return os.path.join(app.path, slots_mod.bridge_exe(slot))

    def _allocate_port(self):
        with self._port_lock:
            now = time.monotonic()
            # A handed-out port is reserved only until the app has had time to bind it (after that the slot's
            # control_port or the listening socket protects it). Reserving it for good used up the whole range
            # after 29 app starts in one gateway lifetime ("no free control port").
            self._reserved = {p: t for p, t in self._reserved.items() if now - t < PORT_RESERVATION_SECONDS}
            used = {s.control_port for s in self.slots.values() if s.control_port}
            port = allocate_control_port(
                self._control_range[0],
                self._control_range[1],
                self._excluded | used | set(self._reserved),
            )
            self._reserved[port] = now
            return port

    # -- start / stop ------------------------------------------------------- #

    def ensure_started(self, slot):
        if slot not in self.slots:
            raise GatewayError("unknown slot '%s'" % slot)
        state = self.slots[slot]
        with state.lock:
            valid, reason = slots_mod.slot_validity(
                slot, self.config.app(slot), self.config.dev_for(slot)
            )
            if not valid:
                raise GatewayError("%s: %s" % (slot, reason))
            if self._effective_mode(slot) != "headless":
                self._ensure_app(state)
            self._ensure_bridge(state)
            state.last_error = None

    def _ensure_app(self, state):
        if state.app_pid and self._app_alive(state):
            return
        if state.app_pid:
            self._forget_app(state)
        self._stop_bridge(state)  # a bridge of the old app points at its old control port
        self._start_app(state)

    def _app_alive(self, state) -> bool:
        if not pid_alive(state.app_pid):
            return False
        return control_port_open(state.control_port)

    def _start_app(self, state):
        slot = state.slot
        dev = self.config.dev_for(slot)
        app = self.config.app(slot)
        port = self._allocate_port()
        if dev and dev.app_cmd:
            cmd = [self._subst(x, port) for x in dev.app_cmd]
            cwd = app.path or None
            flags = CREATE_NO_WINDOW
        else:
            exe = os.path.join(app.path, slots_mod.app_exe(slot))
            cmd = [exe] + [
                self._subst(x, port) for x in slots_mod.LAUNCH[slot]["app_args"]
            ]
            cwd = app.path
            # Several builds are console-subsystem exes: without this Windows opens a black console
            # next to the app window. Output is piped anyway and GUI windows are not affected.
            flags = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=flags,
            )
        except OSError as exc:
            state.last_error = "failed to start app: %s" % exc
            raise GatewayError("%s: failed to start app: %s" % (slot, exc))
        state.app_proc = proc
        state.app_pid = proc.pid
        state.app_owned = True
        state.control_port = port
        state.app_started_at = now_iso()
        state.started_path = app.path or ""
        state.path_changed = False
        threading.Thread(
            target=_pump_child, args=(LOG, proc.stdout, proc.stderr, slot, "app"),
            daemon=True,
        ).start()
        self._write_state()
        if not wait_control_port(port, timeout=60.0):
            state.last_error = "app did not open control port %d within 60s" % port
            self._hard_kill_app(state)
            self._write_state()
            raise GatewayError(
                "%s: app failed to start (no control port %d after 60s)"
                % (slot, port)
            )
        LOG.info("started %s app pid=%d control=%d", slot, proc.pid, port)

    def _forget_app(self, state):
        proc = state.app_proc
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass
        state.app_proc = None
        state.app_pid = None
        state.app_owned = False
        state.control_port = None

    def _hard_kill_app(self, state):
        proc = state.app_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    proc.kill()
                except OSError:
                    pass
        elif state.app_pid:
            terminate_pid(state.app_pid, force=True)
        self._forget_app(state)

    def _ensure_bridge(self, state):
        bridge = state.bridge
        if bridge is not None and bridge.alive and bridge.proc.poll() is None:
            return
        if bridge is not None:
            bridge.stop()
            state.bridge = None
        self._start_bridge(state)

    def _start_bridge(self, state):
        slot = state.slot
        dev = self.config.dev_for(slot)
        app = self.config.app(slot)
        mode = self._effective_mode(slot)
        if dev and dev.bridge_cmd:
            cmd = [self._subst(x, state.control_port or 0) for x in dev.bridge_cmd]
            cwd = app.path or None
        elif mode == "headless":
            exe = os.path.join(app.path, slots_mod.bridge_exe(slot))
            cmd = [exe] + list(slots_mod.LAUNCH[slot].get("headless_args", ["mcp"]))
            cwd = app.path
        else:
            exe = os.path.join(app.path, slots_mod.bridge_exe(slot))
            cmd = [exe] + [
                self._subst(x, state.control_port)
                for x in slots_mod.LAUNCH[slot]["bridge_args"]
            ]
            cwd = app.path
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                # The bridges speak UTF-8; the Windows locale default (cp1252) turned every
                # non-ASCII character in a tool result into mojibake.
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=CREATE_NO_WINDOW,
            )
        except OSError as exc:
            state.last_error = "failed to start bridge: %s" % exc
            raise GatewayError("%s: failed to start bridge: %s" % (slot, exc))
        bridge = Bridge(slot, proc, LOG)
        bridge.start()
        state.bridge = bridge
        state.last_error = None
        LOG.info("started %s bridge pid=%d", slot, proc.pid)
        return bridge

    def start_app(self, slot, force=False):
        self.ensure_started(slot)
        return {"ok": True, "slot": slot, "action": "start"}

    def stop_app(self, slot, force=False):
        if slot not in self.slots:
            raise GatewayError("unknown slot '%s'" % slot)
        state = self.slots[slot]
        with state.lock:
            self._stop_bridge(state)
            app = self.config.app(slot)
            if state.app_pid and pid_alive(state.app_pid):
                if self._effective_mode(slot) != "headless" and not force:
                    titles = self._query_dirty(state)
                    if titles is None:
                        raise DirtyError([], unknown=True)  # unknown is treated as unsafe
                    if titles:
                        raise DirtyError(titles)
                self._quit_app(state, force)
            self._write_state()
        return {
            "ok": True,
            "slot": slot,
            "action": "stop",
            "forced": bool(force),
        }

    def restart_app(self, slot, force=False):
        self.stop_app(slot, force=force)
        return self.start_app(slot)

    def _stop_bridge(self, state):
        if state.bridge is not None:
            state.bridge.stop()
        state.bridge = None

    def _query_dirty(self, state):
        if not state.control_port or not pid_alive(state.app_pid):
            return None
        spec = slots_mod.LAUNCH.get(state.slot, {}).get("dirty") or {
            "method": "ui.inspect",
            "docs": ["documents"],
        }
        try:
            reply = control_call(
                state.control_port,
                spec["method"],
                spec.get("params"),
                timeout=5.0,
            )
        except Exception:
            return None
        result = reply.get("result") if isinstance(reply, dict) else None
        if "flag" in spec:
            value = _dig(result, spec["flag"])
            if not isinstance(value, bool):
                return None
            return [spec.get("title", "project")] if value else []
        documents = _dig(result, spec["docs"])
        if not isinstance(documents, list):
            return None
        return [
            str(doc.get("title") or doc.get("name") or "untitled")
            for doc in documents
            if isinstance(doc, dict) and doc.get("dirty")
        ]

    def _quit_app(self, state, force):
        port = state.control_port
        if port:
            try:
                quit_params = (
                    {"force": True}
                    if force and slots_mod.LAUNCH.get(state.slot, {}).get("quit_force")
                    else None
                )
                control_call(port, "app.quit", quit_params, timeout=5.0)
            except Exception as exc:
                LOG.debug("app.quit failed for %s: %s", state.slot, exc)
        proc = state.app_proc
        if proc is not None:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        else:
            self._wait_pid(state.app_pid, 10.0)
        if state.app_pid and pid_alive(state.app_pid):
            self._hard_kill_app(state)
        else:
            self._forget_app(state)

    @staticmethod
    def _wait_pid(pid, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not pid_alive(pid):
                return
            time.sleep(0.2)

    # -- shutdown ----------------------------------------------------------- #

    def shutdown_report(self, force=False):
        stopped_bridges = []
        stopped_apps = []
        left_running = []
        for slot, state in self.slots.items():
            with state.lock:
                if state.bridge is not None:
                    self._stop_bridge(state)
                    stopped_bridges.append(slot)
                if not (state.app_pid and pid_alive(state.app_pid)):
                    if state.app_proc is not None:
                        self._forget_app(state)
                    continue
                mode = self._effective_mode(slot)
                titles = []
                if mode != "headless" and not force:
                    titles = self._query_dirty(state)
                    if titles is None:  # could not check: unknown is treated as unsafe
                        left_running.append({"slot": slot, "titles": [], "unknown": True})
                        LOG.warning("leaving %s running: could not check it for unsaved documents", slot)
                        continue
                if titles and not force:
                    left_running.append({"slot": slot, "titles": titles})
                    LOG.warning(
                        "leaving %s running with unsaved documents: %s",
                        slot, ", ".join(titles),
                    )
                    continue
                self._quit_app(state, force)
                stopped_apps.append(slot)
        self._write_state()
        return {
            "ok": True,
            "stopped_bridges": stopped_bridges,
            "stopped_apps": stopped_apps,
            "left_running": left_running,
            "forced": bool(force),
        }

    def request_shutdown(self, force=False, keepalive=False):
        report = self.shutdown_report(force=force)
        # keepalive: the monitor asks for confirmation before tearing down the
        # server when apps were left running; the plain CLI never sets it.
        if not (keepalive and report.get("left_running") and not force):
            threading.Thread(target=self.stop_server, daemon=True).start()
        return report

    # -- MCP ---------------------------------------------------------------- #

    def mcp_call(self, slot, messages):
        try:
            self.ensure_started(slot)
        except GatewayError as exc:
            return self._error_replies(messages, str(exc))
        state = self.slots[slot]
        bridge = state.bridge
        replies = []
        saw_initialize = False
        for message in messages:
            if not isinstance(message, dict):
                replies.append(self._rpc_error(None, -32600, "invalid request"))
                continue
            method = message.get("method")
            try:
                if method == "initialize":
                    replies.append(bridge.initialize(message))
                    saw_initialize = True
                elif method == "notifications/initialized":
                    bridge.client_initialized()
                elif method is not None and "id" not in message:
                    bridge.notify(message)
                elif "id" in message:
                    replies.append(bridge.call(message))
                else:
                    bridge.notify(message)
            except BridgeError as exc:
                replies.append(self._rpc_error(message.get("id"), -32000, str(exc)))
        if replies:
            state.last_call_at = now_iso()
        if not replies:
            return 202, None, None
        body = replies[0] if len(messages) == 1 else replies
        session = self.session_id if saw_initialize else None
        return 200, body, session

    def _error_replies(self, messages, text):
        replies = [self._rpc_error(m.get("id"), -32000, text)
                   for m in messages if isinstance(m, dict) and "id" in m]
        if not replies:
            return 202, None, None
        body = replies[0] if len(messages) == 1 else replies
        return 200, body, None

    @staticmethod
    def _rpc_error(mid, code, message):
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": code, "message": message},
        }

    # -- status / page ------------------------------------------------------ #

    def slot_status(self, slot, include_dirty=False):
        app = self.config.app(slot)
        dev = self.config.dev_for(slot)
        valid, reason = slots_mod.slot_validity(slot, app, dev)
        state = self.slots[slot]
        mode = self._effective_mode(slot)
        running_app = bool(state.app_pid and pid_alive(state.app_pid))
        running_bridge = bool(
            state.bridge is not None
            and state.bridge.alive
            and state.bridge.proc.poll() is None
        )
        alive = running_bridge if mode == "headless" else running_app
        if alive:
            status = "running"
        elif not valid:
            status = "disabled"
        elif state.last_error:
            status = "failed"
        else:
            status = "stopped"
        info = {
            "supported": slots_mod.is_supported(slot),
            "path": app.path or "",
            "valid": valid,
            "reason": reason,
            "mode": mode,
            "state": status,
            "app_pid": state.app_pid if running_app else None,
            "control_port": state.control_port if running_app else None,
            "bridge_pid": state.bridge_pid if running_bridge else None,
            "started_at": state.app_started_at,
            "last_call_at": state.last_call_at,
            "last_error": state.last_error,
            "build_info": slots_mod.build_info(app.path),
            "dirty_documents": None,
            "path_changed_restart_needed": bool(
                state.path_changed and status == "running"
            ),
        }
        if include_dirty and status == "running" and mode != "headless":
            info["dirty_documents"] = self._query_dirty(state)
        return info

    def status(self, include_dirty=False):
        started = self._started_at
        try:
            start_dt = datetime.fromisoformat(started)
            uptime = int((datetime.now(timezone.utc).astimezone() - start_dt).total_seconds())
        except ValueError:
            uptime = 0
        return {
            "pid": os.getpid(),
            "port": self.port,
            "started_at": started,
            "uptime_s": max(uptime, 0),
            "apps": {
                slot: self.slot_status(slot, include_dirty=include_dirty)
                for slot in slots_mod.SLOTS
            },
        }

    def start_page(self):
        state = self.status(include_dirty=False)
        rows = []
        for slot in slots_mod.SLOTS:
            info = state["apps"][slot]
            reg = "claude mcp add --transport http %s http://127.0.0.1:%d/%s" % (
                slot, self.port, slot,
            )
            rows.append(
                "<tr><td><b>%s</b></td><td>%s</td><td>%s</td><td>%s</td>"
                "<td>%s</td><td>%s</td><td><code>%s</code></td></tr>"
                % (
                    html.escape(slot),
                    "yes" if info["supported"] else "no",
                    html.escape(info["state"]),
                    html.escape(info["mode"]),
                    "yes" if info["valid"] else "no",
                    html.escape(info["reason"]),
                    html.escape(reg),
                )
            )
        return (
            "<!doctype html><html><head><meta charset='utf-8'>"
            "<title>craft-gateway</title></head><body>"
            "<h1>craft-gateway</h1>"
            "<p style='color:#a00'><b>No authentication: any process on this "
            "machine can drive your apps.</b></p>"
            "<p>pid %d &middot; port %d &middot; started %s &middot; uptime %ss</p>"
            "<table border='1' cellpadding='6' cellspacing='0'>"
            "<tr><th>slot</th><th>supported</th><th>state</th><th>mode</th>"
            "<th>valid</th><th>reason</th><th>register</th></tr>%s</table>"
            "</body></html>"
            % (
                state["pid"], state["port"], html.escape(state["started_at"]),
                state["uptime_s"], "".join(rows),
            )
        )


def _pump_child(log, out, err, slot, label):
    threads = []

    def pump(stream, stream_label):
        if stream is None:
            return
        try:
            for line in stream:
                if isinstance(line, bytes):
                    line = line.decode("utf-8", "replace")
                line = line.rstrip("\r\n")
                if line:
                    log.debug("%s[%s] %s: %s", label, slot, stream_label, line[:500])
        except (ValueError, OSError):
            pass

    for stream, name in ((out, "stdout"), (err, "stderr")):
        thread = threading.Thread(target=pump, args=(stream, name), daemon=True)
        thread.start()
        threads.append(thread)


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def validate_bind_address(address):
    """127.0.0.1 is required unless the escape hatch is set."""
    if os.environ.get("CRAFT_GATEWAY_ALLOW_NON_LOOPBACK") == "1":
        return
    host = address
    if address.startswith("["):
        host = address[1 : address.find("]")]
    host = host.lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError(
            "refusing to bind %s: only loopback is allowed "
            "(set CRAFT_GATEWAY_ALLOW_NON_LOOPBACK=1 to override)" % address
        )


def _valid_host(header):
    if not header:
        return False
    header = header.strip().lower()
    if header.startswith("["):
        end = header.find("]")
        return end > 0 and header[1:end] in ("::1",)
    if header.count(":") == 1:
        header = header.split(":", 1)[0]
    return header in ("127.0.0.1", "localhost")


def _valid_origin(origin):
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "craft-gateway/1.0"

    # -- plumbing ----------------------------------------------------------- #

    def log_message(self, fmt, *args):  # route to the logger, not stderr
        LOG.debug("http %s", fmt % args)

    @property
    def gateway(self):
        return self.server.gateway

    def _read_body(self):
        if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
            return self._read_chunked()
        length = self.headers.get("Content-Length")
        if length:
            try:
                return self.rfile.read(int(length))
            except (ValueError, OSError):
                return b""
        return b""

    def _read_chunked(self):
        chunks = []
        while True:
            line = self.rfile.readline(65536).strip()
            if not line:
                break
            try:
                size = int(line.split(b";", 1)[0], 16)
            except ValueError:
                break
            if size == 0:
                self.rfile.readline(65536)
                break
            chunks.append(self.rfile.read(size))
            self.rfile.readline(65536)
        return b"".join(chunks)

    def _send(self, code, body=b"", content_type="application/json", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.send_header("Connection", "close")
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
        self.close_connection = True

    def _json(self, code, obj, extra=None):
        self._send(code, json.dumps(obj, separators=(",", ":")), "application/json", extra)

    def _guard(self):
        if not _valid_host(self.headers.get("Host", "")):
            LOG.info("403 bad host %r", self.headers.get("Host", ""))
            self._json(403, {"error": "forbidden: bad Host header"})
            return False
        origin = self.headers.get("Origin")
        if origin and not _valid_origin(origin):
            LOG.info("403 bad origin %r", origin)
            self._json(403, {"error": "forbidden: bad Origin header"})
            return False
        return True

    # -- verbs -------------------------------------------------------------- #

    def do_GET(self):
        self._body = self._read_body()
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, self.gateway.start_page(), "text/html; charset=utf-8")
            return
        if path == "/status":
            query = urlsplit(self.path).query
            dirty = "dirty=1" in query
            self._json(200, self.gateway.status(include_dirty=dirty))
            return
        if self._slot_from_path(path):
            self._send(405, b"", extra={"Allow": "POST, DELETE"})
            return
        self._json(404, {"error": "not found"})

    def do_DELETE(self):
        self._body = self._read_body()
        if not self._guard():
            return
        path = urlsplit(self.path).path
        slot = self._slot_from_path(path)
        if slot:
            self._send(200, b"", "application/json")
            return
        self._json(404, {"error": "not found"})

    def do_POST(self):
        self._body = self._read_body()
        if not self._guard():
            return
        path = urlsplit(self.path).path
        if path == "/admin/shutdown":
            self._handle_shutdown()
            return
        if path == "/admin/reload":
            result = self.gateway.reload_config()
            self._json(200, result)
            return
        admin = self._admin_path(path)
        if admin:
            slot, action = admin
            self._handle_admin(slot, action)
            return
        slot = self._slot_from_path(path)
        if slot:
            self._handle_mcp(slot)
            return
        self._json(404, {"error": "not found"})

    # -- route helpers ------------------------------------------------------ #

    @staticmethod
    def _slot_from_path(path):
        if path.startswith("/") and path.count("/") == 1:
            candidate = path[1:]
            if candidate in slots_mod.SLOTS:
                return candidate
        return None

    @staticmethod
    def _admin_path(path):
        parts = [p for p in path.split("/") if p]
        if len(parts) == 4 and parts[0] == "admin" and parts[1] == "apps":
            if parts[2] in slots_mod.SLOTS and parts[3] in (
                "start", "stop", "restart",
            ):
                return parts[2], parts[3]
        return None

    # -- handlers ----------------------------------------------------------- #

    def _handle_shutdown(self):
        query = urlsplit(self.path).query
        force = "force=1" in query
        keepalive = "keepalive=1" in query
        try:
            report = self.gateway.request_shutdown(force=force, keepalive=keepalive)
        except Exception as exc:
            self._json(500, {"ok": False, "error": str(exc)})
            return
        LOG.info("admin shutdown force=%s left_running=%d", force, len(report["left_running"]))
        self._json(200, report)

    def _handle_admin(self, slot, action):
        query = urlsplit(self.path).query
        force = "force=1" in query
        try:
            if action == "start":
                result = self.gateway.start_app(slot)
            elif action == "stop":
                result = self.gateway.stop_app(slot, force=force)
            else:
                result = self.gateway.restart_app(slot, force=force)
        except DirtyError as exc:
            self._json(200, {
                "ok": False,
                "slot": slot,
                "error": str(exc),
                "dirty_documents": exc.titles,
                "dirty_check_failed": exc.unknown,
            })
            return
        except GatewayError as exc:
            self._json(200, {"ok": False, "slot": slot, "error": str(exc)})
            return
        LOG.info("admin slot=%s action=%s ok", slot, action)
        self._json(200, result)

    def _handle_mcp(self, slot):
        body = getattr(self, "_body", b"")
        try:
            parsed = json.loads(body.decode("utf-8")) if body else None
        except ValueError:
            self._json(400, self.gateway._rpc_error(None, -32700, "parse error"))
            return
        if parsed is None:
            self._json(400, self.gateway._rpc_error(None, -32600, "invalid request"))
            return
        messages = parsed if isinstance(parsed, list) else [parsed]
        start = time.time()
        status, payload, session = self.gateway.mcp_call(slot, messages)
        duration = (time.time() - start) * 1000.0
        methods = ",".join(
            str(m.get("method", "")) for m in messages if isinstance(m, dict)
        )
        ids = ",".join(
            str(m.get("id")) for m in messages
            if isinstance(m, dict) and "id" in m
        )
        LOG.info(
            "slot=%s methods=%s ids=%s status=%s %.1fms",
            slot, methods, ids, status, duration,
        )
        if status == 202:
            self._send(202, b"")
            return
        extra = {"Mcp-Session-Id": session} if session else None
        self._json(status, payload, extra)


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #


def build_parser():
    parser = argparse.ArgumentParser(prog="gateway.py")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the gateway")
    serve.add_argument("--home", default=None)
    serve.add_argument("--port", type=int, default=None)
    serve.add_argument("--bind", default=None)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command not in (None, "serve"):
        parser.error("unknown command %s" % args.command)
    home = args.home or config_mod.default_home()
    setup_logging(home)
    gateway = Gateway(home, port_override=args.port, bind_override=args.bind)
    try:
        gateway.start()
    except ValueError as exc:
        LOG.error("%s", exc)
        print(str(exc), file=sys.stderr)
        return 2

    def _signal(signum, frame):
        LOG.info("signal %s received, shutting down", signum)
        gateway.request_shutdown(force=False)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _signal)
        except (ValueError, OSError):
            pass
    try:
        gateway.serve_forever()
    except KeyboardInterrupt:
        gateway.request_shutdown(force=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
