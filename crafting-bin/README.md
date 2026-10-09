# Crafting Apps (built from source)

`build_crafting_apps.ps1` builds the apps from clones that sit next to this folder (`../photocraft`, `../vectorcraft`,
`../filmcraft`, `../lightcraft`, `../pdfcraft`, `../effectcraft`, `../designcraft`) and copies the binaries here.
Each app folder then holds `<app>.exe`, `<app>-cli.exe` (the MCP bridge) and `BUILD_INFO.txt` (the commit it was
built from). These folders are what the gateway's slots point at.

| App | Start by hand | Control port | What it is for |
|---|---|---|---|
| DesignCraft | `start-designcraft.cmd` | 7979 | page layout |
| VectorCraft | `start-vectorcraft.cmd` | 7981 | vector illustration |
| LightCraft | `start-lightcraft.cmd` | 7980 | photo library and raw development |
| FilmCraft | `start-filmcraft.cmd` | 9876 | video, color and sound |
| EffectCraft | `start-effectcraft.cmd` | 9877 | motion graphics and effects |
| PhotoCraft | `start-photocraft.cmd` | 9878 (no token, loopback) | image editing |
| PdfCraft (formerly PrintCraft) | `start-pdfcraft.cmd` | none | reading and organizing PDFs |

The launchers are for using an app by hand or from a script. When an agent works through the gateway, the gateway
starts the apps itself on free ports (7971-7999) and you do not need the launchers.

## Rebuild
`powershell -ExecutionPolicy Bypass -File ..\build_crafting_apps.ps1 [app ...]` builds the listed apps (default: all
six except DesignCraft) with `cargo build --release --locked -p <app> -p <app>-cli` into the shared target dir
`C:\crafting_target` (about 7 GB for all six) and copies the binaries here. A first build of all six takes about
40 minutes. Update a clone first with `git -C ..\<app> pull`. Needs the MSVC Build Tools, CMake (PdfCraft) and
network access on the first build. A running app cannot be overwritten: close it before rebuilding it.

DesignCraft has its own script, `designcraft\rebuild.ps1` (target dir `C:\dc_target`). Run it from a PowerShell
window without redirecting its output: with `*>` Windows PowerShell 5.1 turns cargo's progress lines on stderr into
errors and the script stops.

Release builds of large Rust workspaces take a lot of disk space; debug builds and full test runs take far more
(10-40 GB per app). Check free space before building several apps.
