# craft-gateway

One loopback HTTP port that fronts the stdio MCP bridges of the desktop apps.
The gateway starts/stops the apps and their bridges for you and speaks
[Streamable HTTP MCP](https://modelcontextprotocol.io) on `POST /<slot>`, so an
MCP client only needs a URL instead of a stdio command line.

Python 3.12, **standard library only**. No network beyond `127.0.0.1`.

## Trust model (read this)

- **No authentication.** Anyone or anything that can reach `127.0.0.1:<port>`
  can drive the apps that are configured. The start page prints a banner saying
  so; see it at `GET /`.
- The port binds to `127.0.0.1` only. A non-loopback bind is refused unless
  `CRAFT_GATEWAY_ALLOW_NON_LOOPBACK=1` is set.
- `Host` must be `127.0.0.1[:port]` or `localhost[:port]`, and a present
  `Origin` must be an `http(s)://127.0.0.1[:port]` / `...localhost...` origin.
  Anything else gets `403` (DNS-rebinding / web-page protection).
- Executable paths come **only** from `config.toml`, never from HTTP. The HTTP
  surface can only start/stop/restart the already-configured apps and shut the
  gateway down.
- The gateway only manages processes **it started** (or that a previous gateway
  instance started and recorded in `state.json`). It never touches apps you
  launched by hand.
- It refuses to stop an app with unsaved documents (`ui.inspect` -> any
  `dirty` document) unless you pass `force`.

## Slots

Seven fixed slots exist and all are implemented. PhotoCraft, LightCraft and
PrintCraft need builds with small automation patches that are not upstream yet
(`--control PORT` without a token, `documents` in `ui.inspect`, `app.quit`);
stock upstream builds of those three will not work with the gateway.

| slot | app | bridge | unsaved-work check |
|---|---|---|---|
| designcraft | `--control PORT` | `mcp --connect PORT` | `ui.inspect.documents[].dirty` |
| vectorcraft | `--control PORT` | `mcp --connect 127.0.0.1:PORT` (also `--headless`) | `ui.inspect.documents[].dirty` |
| photocraft | `--control PORT` | `mcp --bridge PORT` | `ui.inspect.session.documents[].dirty` |
| lightcraft | `--control PORT` | `mcp --connect PORT` | `ui.inspect.documents[].dirty` |
| printcraft | `--control PORT` | `mcp --connect PORT` | `ui.inspect.documents[].dirty` |
| filmcraft | `--control PORT` | `mcp --bridge 127.0.0.1:PORT` | `engine.execute project.inspect` -> `dirty` |
| effectcraft | `--control PORT` | `mcp --bridge PORT` | `engine.execute project.summary` -> `dirty`; `app.quit {force}` on forced stop |

Per-slot differences live in the `LAUNCH` table of `slots.py`. PhotoCraft is started with `--automation-write-root` set to a dedicated exchange folder (`%TEMP%\craft-exchange`, override with `CRAFT_EXCHANGE_DIR`; never the temp dir itself), so its file export works; paths are relative to that folder.

Verified in the sources:

- `designcraft/apps/designcraft-cli/src/main.rs`:
  `designcraft-cli mcp [--connect PORT] [--sample]`, and
  `"--connect" => connect = Some(it.next().cloned().ok_or("--connect needs a port or host:port")?)`.
  `control_addr()` turns `"7979"` into `"127.0.0.1:7979"`.
- `vectorcraft/apps/vectorcraft-cli/src/main.rs`:
  `vectorcraft-cli mcp [--connect ADDR | --headless]` and
  `"--connect" => connect = Some(it.next().cloned().ok_or("--connect needs an address")?)`,
  with `pub const DEFAULT_ADDR: &str = "127.0.0.1:7979";`. So an explicit
  `127.0.0.1:PORT` is always passed.

## Home directory

`%APPDATA%\craft-gateway` by default, or `CRAFT_GATEWAY_HOME`.

```
config.toml      configuration (see below)
gateway.log      rotating log, 1 MB x 3
state.json       {slot: {app_pid, control_port, exe, path, started_at}}
gateway.pid      pid of the running gateway
```

### config.toml

```toml
port = 7970

[apps.designcraft]
path = "C:\\Users\\you\\crafting-bin\\designcraft"   # folder with designcraft.exe and designcraft-cli.exe
mode = "window"                             # "window" | "headless"

[apps.vectorcraft]
path = "C:\\Users\\you\\crafting-bin\\vectorcraft"
mode = "window"                             # headless: bridge only, no app window

# Optional test/dev override, honoured only from the file. Placeholders:
# {port} control port, {addr} 127.0.0.1:{port}, {home} gateway home dir.
[dev.designcraft]
app_cmd = ["python", "fake_app.py", "--control", "{port}"]
bridge_cmd = ["python", "fake_bridge.py", "--connect", "{port}"]
```

A slot is disabled when it has no `path` and no dev override. A path is valid
when the folder exists and holds both `<slot>.exe` and `<slot>-cli.exe`. A dev
override is enough to run a slot without a real install.

`headless` is only allowed where the bridge supports it (vectorcraft): the app
is not started, only the bridge with `--headless`, and dirty state is unknown.

## gatewayctl

```
python gatewayctl.py start|stop|restart|status [--json] [--dirty]
python gatewayctl.py logs [-n 50] [-f]
python gatewayctl.py apps
python gatewayctl.py app-start <slot>
python gatewayctl.py app-stop <slot> [--force]
python gatewayctl.py app-restart <slot>
python gatewayctl.py config show
python gatewayctl.py config set-path <slot> <folder> [--mode window|headless]
python gatewayctl.py config clear-path <slot>
python gatewayctl.py config set-port <port>
```

`start` spawns `python gateway.py serve` detached (new process group, no
console window) with stdout/stderr appended to `gateway.log`. `stop` posts to
`/admin/shutdown` and waits for the process to exit. Exit codes: `0` ok, `1`
error, `2` not running (for `status`).

`config` commands edit `config.toml` directly and work while the gateway is
down; if a gateway is up they also call `/admin/reload`.

## Craft Gateway Monitor

A small MAMP-style desktop window (tkinter, standard library only) over the
same config and HTTP surface:

```sh
python monitor.py                 # or: start-monitor.cmd (no console window)
```

`start-monitor.cmd` resolves `pythonw.exe` next to the first `python` on PATH
(falling back to `pythonw.exe` on PATH) so the window runs on the same
interpreter as `python gatewayctl.py`, without a console.

- One resizable window, one row per fixed slot. It shows the gateway state
  (grey stopped / green running / red error), the seven apps' states, control
  ports, `BUILD_INFO.txt` build lines, the configured install folder with a
  green/red/grey validation colour and reason, and per-row actions.
- The install folder of each slot is chosen with a folder picker and written to
  `config.toml` (as `gatewayctl config set-path` does), then `/admin/reload` is
  called when the gateway is running. HTTP never carries paths. A folder is
  only saved after `slots.validate_path` accepts it.
- Unsupported slots still have a path field but are greyed as `not implemented
  yet` and have no Start/Stop. `vectorcraft` also has a `Headless` checkbox
  bound to `mode` in the config.
- Starting/stopping the gateway and apps uses the same code paths as
  `gatewayctl` (`start` spawns `gateway.py serve`; the rest is the admin HTTP
  API). Actions run in worker threads; the UI never blocks on the network and a
  2 s `/status` poll (1.5 s timeout) keeps the rows fresh. When the gateway is
  down the rows are read straight from `config.toml` (re-read on mtime change).
- Closing the window does **not** stop the gateway. A second launch exits
  immediately (Windows named mutex).
- Stop is guarded: if the admin reply lists `dirty_documents` or a failed
  check, the dialog names the documents and only a confirmed `force=1` call
  stops the app. Stopping the gateway first asks the server to stay up
  (`/admin/shutdown?keepalive=1`) when apps were left running, so `Force stop`
  can repeat with `force=1` instead of orphaning the confirmation.

`monitor_core.py` holds all state and actions and has no tkinter import
(importable headless); `monitor.py` is the UI only.

## HTTP surface

- `POST /<slot>` — one JSON-RPC message (or a batch) forwarded to that slot's
  bridge over stdio. Requests get `application/json`; notifications/responses
  only get `202`. The first request lazily starts the app and bridge. Errors
  are returned as HTTP `200` JSON-RPC error `{code: -32000, message: ...}`.
- `initialize` replies carry `Mcp-Session-Id: <uuid>`; missing/unknown session
  ids are accepted.
- `DELETE /<slot>` — `200`; `GET /<slot>` — `405`.
- `GET /` — HTML start page (no JS) with state and a copy-ready registration
  line per slot.
- `GET /status[?dirty=1]` — JSON state. `dirty=1` queries `ui.inspect` for
  running GUI apps (it costs a call).
- `POST /admin/apps/<slot>/start|stop|restart[?force=1]`
- `POST /admin/shutdown[?force=1]`, `POST /admin/reload`

### Register with Claude Code

```sh
claude mcp add --transport http designcraft http://127.0.0.1:7970/designcraft
claude mcp add --transport http vectorcraft http://127.0.0.1:7970/vectorcraft
```

## Shutdown behaviour

Bridges are always stopped. For each gateway-owned app: if it has no dirty
documents it is asked to quit (`app.quit`), then terminated and finally killed
after 10 s. Apps with unsaved documents are **left running** and listed in the
shutdown reply and the log. `force=1` quits them anyway.

If the gateway exits unexpectedly, `state.json` lets the next gateway instance
re-adopt an app (pid alive + image path matches + control port accepts).

## Troubleshooting

- *`gateway is not running` (exit 2)* — check `gateway.pid` and
  `gateway.log`; `python gatewayctl.py start`.
- *Slot shows `valid=false`* — the reason is in `/status` (`reason`). Set the
  folder with `gatewayctl config set-path`.
- *`not implemented yet`* — only if a slot is switched off in `slots.SUPPORTED`.
- *App failed to start* — look for `app did not open control port ... within
  60s` in `gateway.log`. Ports `7979` and `7981` are never handed out.
- *`403`* — the `Host`/`Origin` header is not loopback.
- Two gateways: `gatewayctl start` refuses when one already answers; delete a
  stale `gateway.pid` if needed (it is cleaned automatically).

## Tests

```sh
python -m unittest discover -s tests -v
```

The tests use fake apps/bridges written in Python and keep every socket in
`17970..17999`. The production control-port range is `7971..7999` (never
`7979`/`7981`); tests override it with `CRAFT_GATEWAY_CONTROL_RANGE`.

## Acceptance test against the real apps
`python acceptance/accept_gateway.py` starts a temporary gateway (own `CRAFT_GATEWAY_HOME`, port 7970) and drives the real
DesignCraft and VectorCraft builds through it: HTTP MCP handshake, tool calls, concurrent clients, 403 on bad Host/Origin,
the unsaved-documents guard, `claude mcp add --transport http` + `claude mcp list`, clean stop. It opens two app windows for a
few seconds and expects a manually started DesignCraft to keep running. The apps are looked up in `crafting-bin/` of the
folder that holds `gateway/` (set `CRAFT_ROOT` to use another one).
