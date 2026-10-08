# Troubleshooting and where things live

## Troubleshooting
- `error: gateway is not reachable` -> `$C gateway-start`; check `gatewayctl.py logs -n 40` in the gateway folder. The gateway must be restarted after its code changes (a plain `/admin/shutdown` leaves running apps alone).
- `slot ...: path ... not set / invalid` -> the user's install folder is wrong or missing `<app>.exe` and `<app>-cli.exe` (set it in the monitor: `gateway\start-monitor.cmd`).
- App window closed by hand -> just call again; status shows `stopped` meanwhile (the gateway notices within about 2 s and relaunches on the next call with a new port and bridge).
- Two apps on one default port (VectorCraft and DesignCraft both default to 7979) are avoided by the gateway; manual launches use `crafting-bin\start-<app>.cmd` with distinct ports.
- Non-ASCII text arriving as `ÐŸÑ€Ð¸...` -> an old gateway still decoding bridge output as cp1252; restart it.
- A black console next to an app window -> an old gateway; the current one starts apps with `CREATE_NO_WINDOW`.
- `no free control port in 7971..7999` while nothing is listening -> a gateway started before the port-leak fix (it never released handed-out ports after 29 app starts); restart it.
- `find` says no index -> `$C index sync <slot>`; results look stale or wrong -> `$C index doctor`.

## Where things live (paths relative to the craft-gateway repository)
- Apps: `crafting-bin\<app>\`, built from the clones next to the repository folders with `build_crafting_apps.ps1` and `crafting-bin\designcraft\rebuild.ps1`; manual launchers `crafting-bin\start-<app>.cmd`.
- Gateway and search index: `gateway\` (index in `gateway\index\`, monitor `gateway\start-monitor.cmd`, config `%APPDATA%\craft-gateway\config.toml`).
- This skill: `skill\craft-apps\`, installed into the skills folder as a link.
- Projects: `projects\<name>\` (snake_case, own scripts and results), for example `slow_coffee_fair` (flyer: `scripts\`, `assets\`, results).
- Book pipeline: `projects\book_build\` (`build.py`, `new_book.py`, `checks.py`, `example.toml`); template `projects\templates\book-6x9.designcraft`; a new book: `python projects\book_build\new_book.py <name> ...` creates `projects\<name>\`.
- Search backend: the embedder is the knowledge-base skill when installed, else fastembed (`pip install fastembed`); System One re-ranking needs `CRAFT_SYSTEM_ONE_URL` and `CRAFT_SYSTEM_ONE_MODEL` (or the knowledge-base configuration); without it `find` keeps the vector order.
