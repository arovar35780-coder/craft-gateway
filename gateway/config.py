"""Config file loading and deterministic writing.

Read with tomllib. Writing is a small hand-written serializer: it preserves
whatever keys it parsed (minus comments) and emits stable TOML. Dev overrides
are honoured only from the file, never from HTTP.
"""

import os
import tomllib
from dataclasses import dataclass, field
from typing import Dict, List, Optional

DEFAULT_PORT = 7970
VALID_MODES = ("window", "headless")


def default_home() -> str:
    env = os.environ.get("CRAFT_GATEWAY_HOME")
    if env:
        return env
    appdata = os.environ.get("APPDATA") or os.path.expanduser("~")
    return os.path.join(appdata, "craft-gateway")


@dataclass
class AppConfig:
    path: str = ""
    mode: str = "window"


@dataclass
class DevConfig:
    app_cmd: Optional[List[str]] = None
    bridge_cmd: Optional[List[str]] = None


@dataclass
class Config:
    port: int = DEFAULT_PORT
    bind: str = "127.0.0.1"
    apps: Dict[str, AppConfig] = field(default_factory=dict)
    dev: Dict[str, DevConfig] = field(default_factory=dict)
    raw: dict = field(default_factory=dict)

    def app(self, slot: str) -> AppConfig:
        return self.apps.get(slot) or AppConfig()

    def dev_for(self, slot: str) -> Optional[DevConfig]:
        return self.dev.get(slot)


def config_path(home: str) -> str:
    return os.path.join(home, "config.toml")


def _parse_dev_cmd(value):
    if value is None:
        return None
    if isinstance(value, list) and all(isinstance(x, str) for x in value):
        return list(value)
    return None


def from_raw(raw: dict) -> Config:
    cfg = Config()
    cfg.raw = raw if isinstance(raw, dict) else {}
    try:
        cfg.port = int(cfg.raw.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        cfg.port = DEFAULT_PORT
    cfg.bind = str(cfg.raw.get("bind", "127.0.0.1"))
    apps = cfg.raw.get("apps", {}) or {}
    if isinstance(apps, dict):
        for slot, entry in apps.items():
            if not isinstance(entry, dict):
                continue
            cfg.apps[slot] = AppConfig(
                path=str(entry.get("path", "") or ""),
                mode=str(entry.get("mode", "window") or "window"),
            )
    dev = cfg.raw.get("dev", {}) or {}
    if isinstance(dev, dict):
        for slot, entry in dev.items():
            if not isinstance(entry, dict):
                continue
            cfg.dev[slot] = DevConfig(
                app_cmd=_parse_dev_cmd(entry.get("app_cmd")),
                bridge_cmd=_parse_dev_cmd(entry.get("bridge_cmd")),
            )
    return cfg


def load(home: str) -> Config:
    path = config_path(home)
    if not os.path.isfile(path):
        return Config()
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    return from_raw(raw)


def _fmt(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        return _quote(value)
    if isinstance(value, list):
        return "[" + ", ".join(_fmt(v) for v in value) + "]"
    if value is None:
        return '""'
    raise TypeError("cannot serialize %r" % (value,))


def _quote(s: str) -> str:
    out = (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    return '"' + out + '"'


def _write_table(lines, prefix, table):
    scalars = [(k, v) for k, v in table.items() if not isinstance(v, dict)]
    subs = [(k, v) for k, v in table.items() if isinstance(v, dict)]
    if prefix and scalars:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append("[%s]" % prefix)
        for k, v in scalars:
            lines.append("%s = %s" % (k, _fmt(v)))
    for k, v in subs:
        name = "%s.%s" % (prefix, k) if prefix else k
        _write_table(lines, name, v)


def dump(cfg: Config) -> str:
    raw = cfg.raw if isinstance(cfg.raw, dict) else {}
    lines = []
    for k, v in raw.items():
        if not isinstance(v, dict):
            lines.append("%s = %s" % (k, _fmt(v)))
    for k, v in raw.items():
        if isinstance(v, dict):
            _write_table(lines, k, v)
    return "\n".join(lines).rstrip() + "\n"


def save(home: str, cfg: Config) -> None:
    os.makedirs(home, exist_ok=True)
    path = config_path(home)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(dump(cfg))
    os.replace(tmp, path)


def set_path(cfg: Config, slot: str, folder: str, mode: Optional[str] = None) -> None:
    apps = cfg.raw.setdefault("apps", {})
    entry = apps.setdefault(slot, {})
    entry["path"] = folder
    if mode:
        entry["mode"] = mode
    entry.setdefault("mode", "window")
    _refresh(cfg)


def clear_path(cfg: Config, slot: str) -> None:
    apps = cfg.raw.get("apps", {})
    if isinstance(apps, dict):
        apps.pop(slot, None)
    _refresh(cfg)


def set_port(cfg: Config, port: int) -> None:
    cfg.raw["port"] = int(port)
    _refresh(cfg)


def _refresh(cfg: Config) -> None:
    """Rebuild the typed view after a raw edit."""
    fresh = from_raw(cfg.raw)
    cfg.port = fresh.port
    cfg.bind = fresh.bind
    cfg.apps = fresh.apps
    cfg.dev = fresh.dev
