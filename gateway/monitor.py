"""Craft Gateway Monitor: a small desktop window over monitor_core.

tkinter only. The window is a thin layer: closing it never stops the gateway.
All network work happens in worker threads and lands in a queue that the Tk
thread drains, so the UI never blocks.
"""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import monitor_core
from monitor_core import ActionResult

FONT = ("Segoe UI", 9)
FONT_SMALL = ("Segoe UI", 8)
FONT_LOG = ("Consolas", 9)

COLOR_GREY = "#9e9e9e"
COLOR_GREEN = "#2e7d32"
COLOR_RED = "#c62828"
COLOR_AMBER = "#ef6c00"

PATH_COLORS = {"ok": "#dff0d8", "invalid": "#f2dede", "empty": "#ececec"}

STATE_COLORS = {
    "running": COLOR_GREEN,
    "stopped": COLOR_GREY,
    "disabled": COLOR_GREY,
    "failed": COLOR_RED,
    "starting": COLOR_AMBER,
    "not implemented yet": COLOR_GREY,
}

# Column indexes in the apps table.
C_LIGHT, C_NAME, C_STATE, C_PORT, C_BUILD = 0, 1, 2, 3, 4
C_PATH, C_BROWSE, C_CLEAR, C_START, C_STOP, C_MCP, C_HEADLESS = 5, 6, 7, 8, 9, 10, 11


def _enable_dpi_awareness():
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


_instance_handle = None


def _acquire_single_instance(name="CraftGatewayMonitor"):
    """Windows named mutex so a second launch does not open a second window."""
    global _instance_handle
    if os.name != "nt":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle:
            return True
        if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            return False
        _instance_handle = handle
        return True
    except Exception:
        return True


def _warn_already_running():
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None,
            "Craft Gateway Monitor is already running.",
            "Craft Gateway Monitor",
            0x40,
        )
    except Exception:
        print("Craft Gateway Monitor is already running.")


def _set_enabled(widget, enabled):
    widget.config(state="normal" if enabled else "disabled")


def _read_log_tail(path, limit=300):
    if not path or not os.path.isfile(path):
        return ["(no log file at %s)" % path]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return ["(could not read log: %s)" % exc]
    return [ln.rstrip("\r\n") for ln in lines[-limit:]]


def _short_build(build_info, limit=34):
    """First line of BUILD_INFO as '<hash> <subject>', shortened so it cannot squeeze the path column."""
    if not build_info:
        return "-"
    line = build_info.splitlines()[0].strip()
    prefix = "Built from commit:"
    if line.startswith(prefix):
        line = line[len(prefix):].strip()
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


class RowWidgets:
    """Handles for one slot's widgets so render() can update them cheaply."""

    def __init__(self):
        self.light = None
        self.name = None
        self.state = None
        self.port = None
        self.build = None
        self.path_frame = None
        self.path_entry = None
        self.path_reason = None
        self.btn_browse = None
        self.btn_clear = None
        self.btn_start = None
        self.btn_stop = None
        self.btn_mcp = None
        self.headless_var = None
        self.headless_check = None


class MonitorApp:
    def __init__(self, root, core=None):
        self.root = root
        self.core = core or monitor_core.MonitorCore()
        self.queue = queue.Queue()
        self.rows = {}
        self._busy = False
        self._status_hold_until = 0.0
        self._stop = threading.Event()
        self.log_window = None

        self._build_style()
        self._build_widgets()
        # Show config data immediately even when the gateway is down.
        self.render(self.core.snapshot())

    # -- construction ------------------------------------------------------- #

    def _build_style(self):
        self.root.title("Craft Gateway Monitor")
        self.root.minsize(980, 430)
        style = ttk.Style(self.root)
        for theme in ("vista", "clam", "default"):
            if theme in style.theme_names():
                try:
                    style.theme_use(theme)
                    break
                except tk.TclError:
                    continue
        style.configure(".", font=FONT)
        style.configure("TLabelframe.Label", font=FONT)
        style.configure("Warn.TLabel", foreground=COLOR_RED)
        style.configure("Small.TLabel", font=FONT_SMALL)

    def _build_widgets(self):
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(2, weight=1)

        top = ttk.LabelFrame(self.root, text="Gateway", padding=6)
        top.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        top.columnconfigure(1, weight=1)

        self.gw_light = tk.Label(top, text="\u25cf", font=("Segoe UI", 12), fg=COLOR_GREY, width=2)
        self.gw_light.grid(row=0, column=0, rowspan=2, sticky="w")
        self.gw_text = ttk.Label(top, text="Gateway: stopped")
        self.gw_text.grid(row=0, column=1, sticky="w")

        controls = ttk.Frame(top)
        controls.grid(row=0, column=2, sticky="e")
        self.btn_start_gateway = ttk.Button(controls, text="Start gateway", command=self.on_start_gateway)
        self.btn_start_gateway.pack(side="left", padx=2)
        self.btn_stop_gateway = ttk.Button(controls, text="Stop gateway", command=self.on_stop_gateway)
        self.btn_stop_gateway.pack(side="left", padx=2)
        ttk.Button(controls, text="Open start page", command=self.on_open_page).pack(side="left", padx=2)
        ttk.Button(controls, text="Log...", command=self.on_open_log).pack(side="left", padx=2)
        ttk.Button(controls, text="Settings...", command=self.on_settings).pack(side="left", padx=2)

        table = ttk.LabelFrame(self.root, text="Apps", padding=6)
        table.grid(row=2, column=0, sticky="nsew", padx=8, pady=4)
        table.columnconfigure(C_PATH, weight=1)

        headers = [
            (C_LIGHT, ""),
            (C_NAME, "App"),
            (C_STATE, "State"),
            (C_PORT, "Control port"),
            (C_BUILD, "Build"),
            (C_PATH, "Install folder"),
            (C_BROWSE, ""),
            (C_CLEAR, ""),
            (C_START, ""),
            (C_STOP, ""),
            (C_MCP, ""),
            (C_HEADLESS, "Headless"),
        ]
        for col, text in headers:
            ttk.Label(table, text=text).grid(row=0, column=col, sticky="w", padx=3, pady=(0, 2))

        for index, slot in enumerate(_slots(), start=1):
            self._build_row(table, index, slot)

        bottom = ttk.Frame(self.root)
        bottom.grid(row=3, column=0, sticky="ew", padx=8, pady=(4, 8))
        bottom.columnconfigure(2, weight=1)
        self.btn_start_all = ttk.Button(bottom, text="Start all", command=self.on_start_all)
        self.btn_start_all.grid(row=0, column=0, padx=2)
        self.btn_stop_all = ttk.Button(bottom, text="Stop all", command=self.on_stop_all)
        self.btn_stop_all.grid(row=0, column=1, padx=2)
        self.status_var = tk.StringVar(value="Ready")
        self.status_label = ttk.Label(bottom, textvariable=self.status_var)
        self.status_label.grid(row=0, column=2, sticky="w", padx=12)

    def _build_row(self, table, grid_row, slot):
        w = RowWidgets()
        self.rows[slot] = w

        w.light = tk.Label(table, text="\u25cf", font=("Segoe UI", 10), fg=COLOR_GREY, width=2)
        w.light.grid(row=grid_row, column=C_LIGHT, sticky="w", padx=3, pady=2)
        w.name = ttk.Label(table, text=slot)
        w.name.grid(row=grid_row, column=C_NAME, sticky="w", padx=3)
        w.state = ttk.Label(table, text="-")
        w.state.grid(row=grid_row, column=C_STATE, sticky="w", padx=3)
        w.port = ttk.Label(table, text="-")
        w.port.grid(row=grid_row, column=C_PORT, sticky="w", padx=3)
        w.build = ttk.Label(table, text="-")
        w.build.grid(row=grid_row, column=C_BUILD, sticky="w", padx=3)

        w.path_frame = ttk.Frame(table)
        w.path_frame.grid(row=grid_row, column=C_PATH, sticky="ew", padx=3)
        w.path_frame.columnconfigure(0, weight=1)
        w.path_entry = tk.Entry(
            w.path_frame, font=FONT, state="readonly", readonlybackground=PATH_COLORS["empty"], width=48
        )
        w.path_entry.grid(row=0, column=0, sticky="ew")
        w.path_reason = ttk.Label(w.path_frame, text="", font=FONT_SMALL, foreground=COLOR_RED)
        w.path_reason.grid(row=1, column=0, sticky="w")
        w.path_reason.grid_remove()

        w.btn_browse = ttk.Button(table, text="Browse...", width=9,
                                  command=lambda s=slot: self.on_browse(s))
        w.btn_browse.grid(row=grid_row, column=C_BROWSE, padx=2)
        w.btn_clear = ttk.Button(table, text="Clear", width=6,
                                 command=lambda s=slot: self.on_clear(s))
        w.btn_clear.grid(row=grid_row, column=C_CLEAR, padx=2)
        w.btn_start = ttk.Button(table, text="Start", width=6,
                                 command=lambda s=slot: self.on_start_app(s))
        w.btn_start.grid(row=grid_row, column=C_START, padx=2)
        w.btn_stop = ttk.Button(table, text="Stop", width=6,
                                command=lambda s=slot: self.on_stop_app(s))
        w.btn_stop.grid(row=grid_row, column=C_STOP, padx=2)
        w.btn_mcp = ttk.Button(table, text="Copy MCP command",
                               command=lambda s=slot: self.on_copy_mcp(s))
        w.btn_mcp.grid(row=grid_row, column=C_MCP, padx=2)
        w.headless_var = tk.BooleanVar(value=False)
        w.headless_check = ttk.Checkbutton(
            table, variable=w.headless_var, command=lambda s=slot: self.on_headless(s)
        )
        w.headless_check.grid(row=grid_row, column=C_HEADLESS, padx=3)

    # -- rendering ---------------------------------------------------------- #

    def render(self, snapshot):
        gateway = snapshot.get("gateway", {})
        state = gateway.get("state", "stopped")
        self.gw_light.config(
            fg={"running": COLOR_GREEN, "error": COLOR_RED}.get(state, COLOR_GREY)
        )
        self.gw_text.config(text=gateway.get("text", "Gateway: stopped"))
        _set_enabled(self.btn_start_gateway, state == "stopped")
        _set_enabled(self.btn_stop_gateway, state == "running")

        any_start = any_stop = False
        for row in snapshot.get("rows", []):
            self._render_row(row)
            any_start = any_start or row["actions"]["start"]
            any_stop = any_stop or row["actions"]["stop"]
        _set_enabled(self.btn_start_all, any_start)
        _set_enabled(self.btn_stop_all, any_stop)
        if self._busy:
            self._set_busy(True)

        error = gateway.get("error")
        if error and _now() > self._status_hold_until:
            self.set_status(error, error=True, hold=True)

    def _render_row(self, row):
        w = self.rows.get(row["slot"])
        if w is None:
            return
        busy = row["state"] in ("running", "starting")
        w.light.config(fg=STATE_COLORS.get(row["state"], COLOR_GREY))
        w.name.config(foreground="" if row["supported"] else COLOR_GREY)
        w.state.config(text=row["state"])
        w.state.config(
            foreground=COLOR_RED if row["state"] == "failed" else ""
        )
        w.port.config(text=str(row["control_port"]) if row["control_port"] else "-")
        build = row.get("build_info") or ""
        w.build.config(text=_short_build(build))

        w.path_entry.config(state="normal")
        w.path_entry.delete(0, "end")
        w.path_entry.insert(0, row["path"])
        w.path_entry.xview_moveto(1.0)  # a long path shows its end: the folder name is what tells forks apart
        w.path_entry.config(
            state="readonly",
            readonlybackground=PATH_COLORS.get(row["path_state"], PATH_COLORS["empty"]),
        )

        reason = ""
        color = COLOR_RED
        if row["path_state"] == "invalid":
            reason = row["path_reason"]
        elif row.get("path_changed_restart_needed"):
            reason = "path changed - restart needed"
            color = COLOR_AMBER
        w.path_reason.config(text=reason, foreground=color)
        if reason:
            w.path_reason.grid()
        else:
            w.path_reason.grid_remove()

        actions = row["actions"]
        _set_enabled(w.btn_browse, actions["browse"])
        _set_enabled(w.btn_clear, actions["clear"])
        _set_enabled(w.btn_start, actions["start"])
        _set_enabled(w.btn_stop, actions["stop"])
        _set_enabled(w.btn_mcp, actions["copy_mcp"])
        _set_enabled(w.headless_check, actions["headless"])
        w.headless_var.set(row["mode"] == "headless")

    def _set_busy(self, busy):
        """Grey out every action button while a worker is in flight."""
        for w in self.rows.values():
            _set_enabled(w.btn_browse, not busy)
            _set_enabled(w.btn_clear, not busy)
            _set_enabled(w.btn_start, not busy)
            _set_enabled(w.btn_stop, not busy)
            _set_enabled(w.btn_mcp, not busy)
            _set_enabled(w.headless_check, not busy)
        _set_enabled(self.btn_start_gateway, not busy)
        _set_enabled(self.btn_stop_gateway, not busy)
        _set_enabled(self.btn_start_all, not busy)
        _set_enabled(self.btn_stop_all, not busy)

    def set_status(self, message, error=False, hold=False):
        self.status_var.set(message)
        self.status_label.config(foreground=COLOR_RED if error else "")
        if error or hold:
            self._status_hold_until = _now() + 10.0
        else:
            self._status_hold_until = 0.0

    # -- polling loop ------------------------------------------------------- #

    def start_polling(self):
        def loop():
            while not self._stop.is_set():
                try:
                    snapshot = self.core.poll()
                except Exception as exc:  # never let the poll thread die
                    self.queue.put(("poll_error", str(exc)))
                    snapshot = None
                if snapshot is not None:
                    self.queue.put(("snapshot", snapshot))
                self._stop.wait(2.0)

        threading.Thread(target=loop, daemon=True).start()
        self.root.after(150, self._drain)

    def _drain(self):
        try:
            while True:
                item = self.queue.get_nowait()
                kind = item[0]
                if kind == "snapshot":
                    self.render(item[1])
                elif kind == "action":
                    self._handle_result(item[1], item[2] if len(item) > 2 else None)
                elif kind == "poll_error":
                    self.set_status("poll error: %s" % item[1], error=True)
        except queue.Empty:
            pass
        self.root.after(150, self._drain)

    def _run_async(self, fn, busy_message, on_done=None):
        self._busy = True
        self._set_busy(True)
        self.set_status(busy_message, hold=True)

        def worker():
            try:
                result = fn()
            except Exception as exc:
                result = ActionResult(False, "error: %s" % exc)
            self.queue.put(("action", result, on_done))

        threading.Thread(target=worker, daemon=True).start()

    def _handle_result(self, result, on_done=None):
        self._busy = False
        self.render(self.core.snapshot())
        if result.needs_confirm:
            self._confirm(result)
            return
        self.set_status(result.message, error=not result.ok)
        if on_done is not None:
            on_done(result)

    def _confirm(self, result):
        info = result.needs_confirm or {}
        kind = info.get("kind")
        if kind == "left_running":
            lines = []
            for app in info.get("apps", []):
                slot = app.get("slot", "?")
                if app.get("unknown"):
                    lines.append("%s: could not check for unsaved documents" % slot)
                else:
                    titles = ", ".join(app.get("titles") or []) or "untitled"
                    lines.append("%s: unsaved documents: %s" % (slot, titles))
            message = (
                "Some apps were left running:\n\n"
                + "\n".join(lines)
                + "\n\nForce stop them?"
            )
            if messagebox.askyesno("Stop gateway", message, icon="warning"):
                self._run_async(
                    lambda: self.core.stop_gateway(force=True), "forcing gateway stop..."
                )
            else:
                self.set_status("gateway left running")
            return

        slot = info.get("slot", "app")
        if kind == "unknown" or info.get("unknown"):
            message = (
                "%s could not be checked for unsaved documents (it may be busy)."
                "\n\nStop anyway?" % slot
            )
        else:
            titles = ", ".join(info.get("titles") or []) or "untitled"
            message = "%s has unsaved documents:\n\n%s\n\nStop anyway?" % (slot, titles)
        if messagebox.askyesno("Unsaved documents", message, icon="warning"):
            self._run_async(
                lambda: self.core.stop_app(slot, force=True), "forcing stop of %s..." % slot
            )
        else:
            self.set_status("stop cancelled")

    # -- gateway actions ---------------------------------------------------- #

    def on_start_gateway(self):
        self._run_async(self.core.start_gateway, "starting gateway...")

    def on_stop_gateway(self):
        self._run_async(self.core.stop_gateway, "stopping gateway...")

    def on_open_page(self):
        url = self.core.start_page_url()
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception as exc:
            self.set_status("could not open browser: %s" % exc, error=True)
            return
        self.set_status("opened %s" % url)

    def on_start_all(self):
        self._run_async(self.core.start_all, "starting all apps...")

    def on_stop_all(self):
        self._run_async(self.core.stop_all, "stopping all apps...")

    # -- app actions -------------------------------------------------------- #

    def on_start_app(self, slot):
        self._run_async(lambda: self.core.start_app(slot), "starting %s..." % slot)

    def on_stop_app(self, slot):
        self._run_async(lambda: self.core.stop_app(slot), "stopping %s..." % slot)

    def on_browse(self, slot):
        current = ""
        try:
            current = self.core.config.app(slot).path or ""
        except Exception:
            current = ""
        folder = filedialog.askdirectory(
            title="Choose install folder for %s" % slot,
            initialdir=current or None,
        )
        if not folder:
            return

        def after(result):
            if not result.ok:
                messagebox.showerror("Invalid folder", result.message)
            self.render(self.core.snapshot())

        self._run_async(
            lambda: self.core.set_path(slot, folder),
            "validating %s folder..." % slot,
            on_done=after,
        )

    def on_clear(self, slot):
        self._run_async(
            lambda: self.core.clear_path(slot),
            "clearing %s folder..." % slot,
            on_done=lambda result: self.render(self.core.snapshot()),
        )

    def on_headless(self, slot):
        headless = bool(self.rows[slot].headless_var.get())
        self._run_async(
            lambda: self.core.set_mode(slot, headless),
            "updating %s mode..." % slot,
            on_done=lambda result: self.render(self.core.snapshot()),
        )

    def on_copy_mcp(self, slot):
        command = self.core.mcp_command(slot)
        self.root.clipboard_clear()
        self.root.clipboard_append(command)
        self.set_status("copied: %s" % command)

    # -- dialogs ------------------------------------------------------------ #

    def on_open_log(self):
        if self.log_window is not None and self.log_window.exists():
            self.log_window.lift()
            return
        self.log_window = LogWindow(self.root, self.core)

    def on_settings(self):
        SettingsDialog(self.root, self.core)


class LogWindow:
    def __init__(self, parent, core):
        self.core = core
        self.top = tk.Toplevel(parent)
        self.top.title("Gateway log")
        self.top.geometry("900x480")
        self.top.columnconfigure(0, weight=1)
        self.top.rowconfigure(0, weight=1)
        self.text = tk.Text(self.top, font=FONT_LOG, wrap="none")
        self.text.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.top, command=self.text.yview)
        scroll.grid(row=0, column=1, sticky="ns")
        self.text.config(yscrollcommand=scroll.set)
        bar = ttk.Frame(self.top)
        bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=6, pady=6)
        ttk.Button(bar, text="Open home folder", command=self.open_home).pack(side="left")
        ttk.Button(bar, text="Close", command=self.top.destroy).pack(side="right")
        self.refresh()

    def exists(self):
        try:
            return bool(self.top.winfo_exists())
        except tk.TclError:
            return False

    def lift(self):
        self.top.lift()

    def refresh(self):
        if not self.exists():
            return
        lines = _read_log_tail(self.core.log_path(), 300)
        self.text.delete("1.0", "end")
        self.text.insert("1.0", "\n".join(lines))
        self.text.see("end")
        self.top.after(2000, self.refresh)

    def open_home(self):
        home = self.core.home
        try:
            if os.name == "nt":
                os.startfile(home)  # noqa: S606 - user requested
            else:
                import subprocess

                subprocess.Popen(["xdg-open", home])
        except Exception as exc:
            messagebox.showerror("Open folder", str(exc))


class SettingsDialog:
    def __init__(self, parent, core):
        self.core = core
        self.top = tk.Toplevel(parent)
        self.top.title("Settings")
        self.top.transient(parent)
        self.top.resizable(False, False)
        frame = ttk.Frame(self.top, padding=10)
        frame.pack(fill="both", expand=True)

        ttk.Label(frame, text="Gateway port (1024-65535):").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar(value=str(core.config.port))
        entry = ttk.Entry(frame, textvariable=self.port_var, width=10)
        entry.grid(row=0, column=1, sticky="w", padx=6)
        ttk.Label(frame, text="restart the gateway to apply", style="Small.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(2, 8)
        )
        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.top.destroy).pack(side="right", padx=2)
        ttk.Button(buttons, text="OK", command=self.save).pack(side="right", padx=2)
        entry.focus_set()

    def save(self):
        try:
            port = int(self.port_var.get().strip())
        except ValueError:
            messagebox.showerror("Settings", "Port must be a number")
            return
        result = self.core.set_port(port)
        if not result.ok:
            messagebox.showerror("Settings", result.message)
            return
        self.top.destroy()


def _slots():
    import slots as slots_mod

    return list(slots_mod.SLOTS)


def _now():
    import time

    return time.time()


def main(argv=None):
    _enable_dpi_awareness()
    if not _acquire_single_instance():
        _warn_already_running()
        return 1
    root = tk.Tk()
    app = MonitorApp(root)
    app.start_polling()
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
