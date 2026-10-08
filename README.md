# craft-gateway

One local MCP endpoint for the seven [Crafting Apps](https://getartcraft.com/apps) (DesignCraft, VectorCraft,
PhotoCraft, LightCraft, FilmCraft, PrintCraft, EffectCraft), so an AI agent can work across all of them through
one place, plus the tools we built on top of it: a book builder, a print-PDF probe and an example flyer.

Each Crafting App ships its own MCP bridge, but they start differently, take different flags and ports, and some
need a token. Without a gateway an agent has to know all of that, start the apps in the right order and keep
track of which port belongs to which app. The gateway hides it: every app gets a fixed URL
(`http://127.0.0.1:7970/<app>`), it is started on the first request, and the gateway refuses to close an app
that has unsaved documents.

This project is not affiliated with the authors of the Crafting Apps.

## What is in here

| Folder | What it is |
|---|---|
| [`gateway/`](gateway/README.md) | The gateway: Streamable HTTP MCP on loopback, lazy app start, unsaved-work guard, `gatewayctl` CLI, a small desktop monitor, tests. Python 3.12, standard library only. |
| `gateway/index/` | A semantic search index over the apps' commands (descriptions, notes, an evaluation set) for clients that look commands up by meaning instead of loading hundreds of tool definitions. |
| [`crafting-bin/`](crafting-bin/README.md) | Where the built apps go, with launchers and the DesignCraft rebuild script. |
| `build_crafting_apps.ps1` | Builds the apps from source clones and copies the binaries into `crafting-bin/`. |
| [`projects/book_build/`](projects/book_build/README.md) | Builds a book (title page, contents, chapters, running headers) in DesignCraft from Markdown files and a template, then checks it page by page. |
| `projects/templates/` | The 6x9 in book template used by the builder. |
| `projects/print_check/` | Builds a DesignCraft test document (CMYK, spot, RGB, transparency, drop shadow), exports PDF/X-4 and prints what is really inside the PDF. |
| [`projects/slow_coffee_fair/`](projects/slow_coffee_fair/README.md) | An example: a flyer whose image is made in PhotoCraft and laid out in DesignCraft, all through the gateway. |

## Layout on disk

The scripts expect the app clones next to this repository's folders:

```
<root>/
  gateway/  crafting-bin/  projects/  build_crafting_apps.ps1     (this repository)
  designcraft/  vectorcraft/  photocraft/  lightcraft/  filmcraft/  printcraft/  effectcraft/   (app clones)
```

## Quick start (Windows)

1. Clone the apps into `<root>` and build them: `powershell -ExecutionPolicy Bypass -File build_crafting_apps.ps1`
   (needs Rust with the MSVC Build Tools and CMake), and `crafting-bin\designcraft\rebuild.ps1` for DesignCraft.
2. Start the monitor: `gateway\start-monitor.cmd`. Pick the install folder of each app (`crafting-bin\<app>`) and
   press Start gateway. Or use the CLI: `python gateway\gatewayctl.py config set-path designcraft <folder>` and
   `python gateway\gatewayctl.py start`.
3. Register the apps with your MCP client, for example Claude Code:
   `claude mcp add --transport http designcraft http://127.0.0.1:7970/designcraft` (the start page at
   `http://127.0.0.1:7970/` lists the line for every app).

## Security

The gateway has **no authentication**: any process on the machine that can reach `127.0.0.1:7970` can drive the
configured apps. It binds to loopback only, rejects non-loopback `Host` and `Origin` headers, takes executable
paths only from its config file, and never stops an app with unsaved documents unless asked to force it. Details
are in [gateway/README.md](gateway/README.md#trust-model-read-this). Do not expose the port to a network.

## Status

- Developed and tested on Windows 11 with Python 3.12. Other platforms are untested.
- PhotoCraft, LightCraft and PrintCraft need small automation patches that are not upstream yet; stock builds of
  those three do not work with the gateway (see [gateway/README.md](gateway/README.md#slots)).
- The example flyer and the search index are written for the `craftmcp.py` client of our Claude skill, which is not
  part of this repository yet; set `CRAFTMCP` to point the example at a copy.

## License

Licensed under either of [Apache License 2.0](LICENSE-APACHE) or [MIT](LICENSE-MIT), at your option, like the
Crafting Apps themselves.
