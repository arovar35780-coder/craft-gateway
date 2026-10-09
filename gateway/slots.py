"""Fixed slot table and install-folder validation.

Seven slots exist forever. Adding a slot here is the
only way to make it addressable, and executable paths never come from HTTP.
"""

import os

# Order matters for the start page and status output.
SLOTS = [
    "designcraft",
    "vectorcraft",
    "photocraft",
    "lightcraft",
    "filmcraft",
    "pdfcraft",
    "effectcraft",
]

SUPPORTED = {
    "designcraft": True,
    "vectorcraft": True,
    "photocraft": True,
    "lightcraft": True,
    "filmcraft": True,
    "pdfcraft": True,
    "effectcraft": True,
}

# Launch lines verified against the cli sources (see README / report):
#   designcraft-cli mcp [--connect PORT]   -> bare port accepted, control_addr() adds 127.0.0.1
#   vectorcraft-cli mcp [--connect ADDR | --headless] -> ADDR is host:port, default 127.0.0.1:7979
# "dirty" tells the gateway where each app reports unsaved work (default: ui.inspect.documents).
LAUNCH = {
    "designcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--connect", "{port}"],
        "headless": False,
    },
    "vectorcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--connect", "{addr}"],
        "headless": True,
        "headless_args": ["mcp", "--headless"],
    },
    # Sources unified on the local `gateway-unify` branches: --control PORT, no token.
    "photocraft": {
        # PhotoCraft opens and writes files only below its automation roots, with relative paths:
        # both are the exchange folder (the client copies files in and out of it).
        # --control-no-auth is the fork's explicit opt-out of the bearer token (loopback only).
        "app_args": [
            "--control", "{port}", "--control-no-auth",
            "--automation-read-root", "{exchange}",
            "--automation-write-root", "{exchange}",
        ],
        "bridge_args": ["mcp", "--bridge", "{addr}", "--control-no-auth"],
        "headless": False,
        "dirty": {"method": "ui.inspect", "docs": ["session", "documents"]},
    },
    "lightcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--connect", "{port}"],
        "headless": False,
        "dirty": {"method": "ui.inspect", "docs": ["documents"]},
    },
    "pdfcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--connect", "{port}"],
        "headless": False,
        "dirty": {"method": "ui.inspect", "docs": ["documents"]},
    },
    # FilmCraft/EffectCraft keep one project: dirty is a single flag in an engine query.
    "filmcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--bridge", "{addr}"],
        "headless": False,
        "dirty": {
            "method": "engine.execute",
            "params": {"command": "project.inspect"},
            "flag": ["dirty"],
            "title": "project",
        },
    },
    "effectcraft": {
        "app_args": ["--control", "{port}"],
        "bridge_args": ["mcp", "--bridge", "{port}"],
        "headless": False,
        "dirty": {
            "method": "engine.execute",
            "params": {"command": "project.summary"},
            "flag": ["dirty"],
            "title": "project",
        },
        # app.quit asks to save a modified project unless force is true.
        "quit_force": True,
    },
}


def is_supported(slot: str) -> bool:
    return bool(SUPPORTED.get(slot))


def app_exe(slot: str) -> str:
    return slot + ".exe"


def bridge_exe(slot: str) -> str:
    return slot + "-cli.exe"


def validate_path(slot: str, folder: str):
    """Check an install folder. Returns (valid, reason)."""
    if not folder:
        return False, "no path configured"
    if not os.path.isdir(folder):
        return False, "folder not found: %s" % folder
    missing = [
        name
        for name in (app_exe(slot), bridge_exe(slot))
        if not os.path.isfile(os.path.join(folder, name))
    ]
    if missing:
        return False, "missing executable(s): %s" % ", ".join(missing)
    return True, "ok"


def _dev_ready(slot: str, dev, mode: str):
    """A dev override is enough to run a slot without a real install folder."""
    if dev is None:
        return False
    spec = LAUNCH.get(slot, {})
    if mode == "headless" and spec.get("headless"):
        return bool(dev.bridge_cmd)
    return bool(dev.app_cmd) and bool(dev.bridge_cmd)


def slot_validity(slot: str, app_cfg, dev_cfg):
    """Combined validity used by /status and lazy start. Returns (valid, reason)."""
    if not is_supported(slot):
        return False, "not implemented yet"
    mode = "window"
    if app_cfg is not None and app_cfg.mode:
        mode = app_cfg.mode
    if mode == "headless" and not LAUNCH.get(slot, {}).get("headless"):
        return False, "headless mode is not supported by this slot"
    if _dev_ready(slot, dev_cfg, mode):
        return True, "dev override"
    if app_cfg is None or not app_cfg.path:
        return False, "no path configured"
    return validate_path(slot, app_cfg.path)


def build_info(folder: str, lines: int = 3):
    """First `lines` lines of BUILD_INFO.txt, or None."""
    if not folder:
        return None
    path = os.path.join(folder, "BUILD_INFO.txt")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    # Strip a UTF-8 BOM left over from PowerShell's Out-File.
    if text.startswith("\ufeff"):
        text = text[1:]
    out = [ln.rstrip("\r\n") for ln in text.splitlines()[:lines]]
    return "\n".join(out) if out else None
