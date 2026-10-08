"""gatewayctl: console tool for craft-gateway.

Talks to a running gateway over loopback HTTP. `start` spawns `gateway.py serve`
detached; everything else is a thin HTTP client. Config commands edit
config.toml directly (they work while the gateway is down) and ask a running
gateway to reload when one is up.
"""

import argparse
import http.client
import json
import os
import subprocess
import sys
import time

import config as config_mod

HERE = os.path.dirname(os.path.abspath(__file__))
GATEWAY_PY = os.path.join(HERE, "gateway.py")

IS_WINDOWS = os.name == "nt"
DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0) if IS_WINDOWS else 0
CREATE_NEW_PROCESS_GROUP = (
    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if IS_WINDOWS else 0
)
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if IS_WINDOWS else 0


def _home():
    return config_mod.default_home()


def _pid_path(home):
    return os.path.join(home, "gateway.pid")


def _log_path(home):
    return os.path.join(home, "gateway.log")


def _read_pid(home):
    try:
        with open(_pid_path(home), "r", encoding="utf-8") as fh:
            return int(fh.read().strip())
    except (OSError, ValueError):
        return None


def _pid_alive(pid):
    if not pid:
        return False
    if IS_WINDOWS:
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
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _http(method, path, port, body=None, timeout=30.0):
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=timeout)
    headers = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        return resp.status, raw
    finally:
        conn.close()


def _probe(port, timeout=1.0):
    try:
        status, raw = _http("GET", "/status", port, timeout=timeout)
    except OSError:
        return None
    if status != 200:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def _port(args, home):
    if getattr(args, "port", None):
        return int(args.port)
    return config_mod.load(home).port


def _print_json(obj):
    print(json.dumps(obj, indent=2))


def _state_summary(status_obj):
    lines = [
        "gateway pid=%s port=%s started=%s uptime=%ss"
        % (
            status_obj.get("pid"),
            status_obj.get("port"),
            status_obj.get("started_at"),
            status_obj.get("uptime_s"),
        )
    ]
    for slot, info in status_obj.get("apps", {}).items():
        lines.append(
            "  %-12s %-8s mode=%-8s valid=%-5s app=%-6s bridge=%-6s %s"
            % (
                slot,
                info.get("state"),
                info.get("mode"),
                info.get("valid"),
                info.get("app_pid") or "-",
                info.get("bridge_pid") or "-",
                "" if info.get("valid") else "(%s)" % info.get("reason"),
            )
        )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# importable operations (shared with monitor_core)
# --------------------------------------------------------------------------- #


def start_gateway(home, port=None, explicit_port=None):
    """Spawn `gateway.py serve` detached. Returns (ok, message)."""
    port = int(port if port is not None else config_mod.load(home).port)
    if _probe(port) is not None or _pid_alive(_read_pid(home)):
        return False, "gateway is already running on port %d" % port
    stale = _read_pid(home)
    if stale:
        try:
            os.remove(_pid_path(home))
        except OSError:
            pass
    os.makedirs(home, exist_ok=True)
    env = os.environ.copy()
    env["CRAFT_GATEWAY_HOME"] = home
    cmd = [sys.executable, GATEWAY_PY, "serve"]
    if explicit_port:
        cmd += ["--port", str(explicit_port)]
    logfh = open(_log_path(home), "a", encoding="utf-8")
    kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=logfh,
        stderr=logfh,
        cwd=HERE,
        env=env,
    )
    if IS_WINDOWS:
        kwargs["creationflags"] = (
            DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except OSError as exc:
        logfh.close()
        return False, "could not start gateway: %s" % exc
    logfh.close()
    with open(_pid_path(home), "w", encoding="utf-8") as fh:
        fh.write(str(proc.pid))
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if _probe(port) is not None:
            return True, "gateway started: http://127.0.0.1:%d" % port
        if proc.poll() is not None:
            break
        time.sleep(0.2)
    return False, "gateway failed to start; see %s" % _log_path(home)


def request_shutdown(home, port=None, force=False, keepalive=False, timeout=20.0):
    """POST /admin/shutdown. Returns (reached, report, message).

    keepalive asks the server to stay up when apps are left running (monitor
    confirmation flow); the plain CLI never uses it.
    """
    port = int(port if port is not None else config_mod.load(home).port)
    if force:
        query = "?force=1"
    elif keepalive:
        query = "?keepalive=1"
    else:
        query = ""
    try:
        status, raw = _http(
            "POST", "/admin/shutdown" + query, port, body={}, timeout=timeout
        )
    except OSError as exc:
        return False, None, "could not reach gateway: %s" % exc
    try:
        report = json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        report = {}
    return True, report, ""


def stop_gateway(home, port=None, force=False):
    """Shut the gateway down and wait for exit. Returns (ok, message)."""
    port = int(port if port is not None else config_mod.load(home).port)
    if _probe(port) is None:
        pid = _read_pid(home)
        if not _pid_alive(pid):
            return True, "gateway is not running"
    reached, _report, message = request_shutdown(home, port, force=force)
    if not reached:
        return False, message
    pid = _read_pid(home)
    deadline = time.time() + 15.0
    while time.time() < deadline:
        if not _pid_alive(pid) and _probe(port) is None:
            break
        time.sleep(0.25)
    try:
        if os.path.isfile(_pid_path(home)):
            os.remove(_pid_path(home))
    except OSError:
        pass
    return True, "gateway stopped"


def set_app_path(home, slot, folder, mode=None):
    """Write an install folder (and optional mode) and reload. (ok, message)."""
    if mode and mode not in config_mod.VALID_MODES:
        return False, "mode must be window or headless"
    cfg = config_mod.load(home)
    config_mod.set_path(cfg, slot, folder, mode)
    config_mod.save(home, cfg)
    _reload_if_running(cfg.port)
    return True, "config updated"


def clear_app_path(home, slot):
    cfg = config_mod.load(home)
    config_mod.clear_path(cfg, slot)
    config_mod.save(home, cfg)
    _reload_if_running(cfg.port)
    return True, "config updated"


def set_gateway_port(home, port):
    if not (1 <= int(port) <= 65535):
        return False, "port out of range"
    cfg = config_mod.load(home)
    config_mod.set_port(cfg, int(port))
    config_mod.save(home, cfg)
    _reload_if_running(int(port))
    return True, "config updated"


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #


def cmd_start(args, home):
    ok, message = start_gateway(home, _port(args, home), getattr(args, "port", None))
    print(message)
    return 0 if ok else 1


def cmd_stop(args, home):
    ok, message = stop_gateway(home, _port(args, home), force=getattr(args, "force", False))
    print(message)
    return 0 if ok else 1


def cmd_restart(args, home):
    port = _port(args, home)
    if _probe(port) is not None:
        force = bool(getattr(args, "force", False))
        path = "/admin/shutdown" + ("?force=1" if force else "")
        try:
            _http("POST", path, port, body={}, timeout=20.0)
        except OSError:
            pass
        pid = _read_pid(home)
        deadline = time.time() + 15.0
        while time.time() < deadline and _pid_alive(pid):
            time.sleep(0.25)
    return cmd_start(args, home)


def cmd_status(args, home):
    port = _port(args, home)
    status_obj = None
    if getattr(args, "dirty", False):
        try:
            code, raw = _http("GET", "/status?dirty=1", port, timeout=30.0)
            if code == 200:
                status_obj = json.loads(raw.decode("utf-8"))
        except OSError:
            status_obj = None
    else:
        status_obj = _probe(port)
    if status_obj is None:
        print("gateway is not running")
        return 2
    if getattr(args, "json", False):
        _print_json(status_obj)
    else:
        print(_state_summary(status_obj))
    return 0


def cmd_logs(args, home):
    path = _log_path(home)
    if not os.path.isfile(path):
        print("no log file at %s" % path)
        return 1
    count = max(int(getattr(args, "lines", 50)), 1)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.readlines()
    for line in lines[-count:]:
        sys.stdout.write(line if line.endswith("\n") else line + "\n")
    if getattr(args, "follow", False):
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            fh.seek(0, os.SEEK_END)
            try:
                while True:
                    line = fh.readline()
                    if not line:
                        time.sleep(0.5)
                        continue
                    sys.stdout.write(line)
                    sys.stdout.flush()
            except KeyboardInterrupt:
                pass
    return 0


def cmd_apps(args, home):
    port = _port(args, home)
    status_obj = _probe(port)
    if status_obj is None:
        print("gateway is not running")
        return 2
    print(_state_summary(status_obj))
    return 0


def _admin(args, home, slot, action, force=False):
    port = _port(args, home)
    if _probe(port) is None:
        print("gateway is not running")
        return 2
    query = "?force=1" if force else ""
    try:
        status, raw = _http(
            "POST", "/admin/apps/%s/%s%s" % (slot, action, query), port,
            body={}, timeout=120.0,
        )
    except OSError as exc:
        print("could not reach gateway: %s" % exc)
        return 1
    obj = json.loads(raw.decode("utf-8")) if raw else {}
    if status != 200 or not obj.get("ok"):
        print("error: %s" % obj.get("error", status))
        if obj.get("dirty_documents"):
            print("unsaved documents: %s" % ", ".join(obj["dirty_documents"]))
        return 1
    print("%s %s ok" % (slot, action))
    return 0


def cmd_app_start(args, home):
    return _admin(args, home, args.slot, "start")


def cmd_app_stop(args, home):
    return _admin(args, home, args.slot, "stop", force=getattr(args, "force", False))


def cmd_app_restart(args, home):
    return _admin(args, home, args.slot, "restart", force=getattr(args, "force", False))


def _reload_if_running(port):
    try:
        _http("POST", "/admin/reload", port, body={}, timeout=5.0)
    except OSError:
        pass


def cmd_config(args, home):
    action = args.config_action
    if action == "show":
        sys.stdout.write(config_mod.dump(config_mod.load(home)))
        return 0
    if action == "set-path":
        ok, message = set_app_path(home, args.slot, args.folder, args.mode)
    elif action == "clear-path":
        ok, message = clear_app_path(home, args.slot)
    elif action == "set-port":
        ok, message = set_gateway_port(home, args.port)
    else:
        print("unknown config action")
        return 1
    if not ok:
        print(message)
        return 1
    print("config updated")
    return 0


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #


def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--port", type=int, default=None, help="gateway port")
    parser = argparse.ArgumentParser(prog="gatewayctl.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("start", parents=[common])
    p.set_defaults(func=cmd_start)

    p = sub.add_parser("stop", parents=[common])
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_stop)

    p = sub.add_parser("restart", parents=[common])
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_restart)

    p = sub.add_parser("status", parents=[common])
    p.add_argument("--json", action="store_true")
    p.add_argument("--dirty", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("logs", parents=[common])
    p.add_argument("-n", "--lines", type=int, default=50)
    p.add_argument("-f", "--follow", action="store_true")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("apps", parents=[common])
    p.set_defaults(func=cmd_apps)

    p = sub.add_parser("app-start", parents=[common])
    p.add_argument("slot")
    p.set_defaults(func=cmd_app_start)

    p = sub.add_parser("app-stop", parents=[common])
    p.add_argument("slot")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_app_stop)

    p = sub.add_parser("app-restart", parents=[common])
    p.add_argument("slot")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_app_restart)

    p = sub.add_parser("config")
    csub = p.add_subparsers(dest="config_action", required=True)
    p = csub.add_parser("show")
    p.set_defaults(func=cmd_config)

    p = csub.add_parser("set-path")
    p.add_argument("slot")
    p.add_argument("folder")
    p.add_argument("--mode", choices=list(config_mod.VALID_MODES))
    p.set_defaults(func=cmd_config)

    p = csub.add_parser("clear-path")
    p.add_argument("slot")
    p.set_defaults(func=cmd_config)

    p = csub.add_parser("set-port")
    p.add_argument("port", type=int)
    p.set_defaults(func=cmd_config)
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args, _home())


if __name__ == "__main__":
    sys.exit(main())
