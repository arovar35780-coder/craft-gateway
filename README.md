# craft-gateway

One local MCP endpoint for the seven [Crafting Apps](https://getartcraft.com/apps) (DesignCraft, VectorCraft,
PhotoCraft, LightCraft, FilmCraft, PdfCraft, EffectCraft), so an AI agent can work across all of them through
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
| `gateway/index/` | A semantic search index over the apps' tools and commands (descriptions, vectors, hand-written notes, an evaluation set), so an agent looks a command up by meaning instead of loading hundreds of tool definitions. |
| [`skill/craft-apps/`](skill/craft-apps/SKILL.md) | A Claude skill and its command-line client `craftmcp.py`: every tool of every app as one shell command, `find` (semantic search over the index), common verbs (new, inspect, render, export, save), pre-delivery checks, index maintenance (`index doctor`, `index sync`, `index eval`). |
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
  designcraft/  vectorcraft/  photocraft/  lightcraft/  filmcraft/  pdfcraft/  effectcraft/   (app clones)
```

## Quick start (Windows)

1. Clone the apps into `<root>` and build them: `powershell -ExecutionPolicy Bypass -File build_crafting_apps.ps1`
   (needs Rust with the MSVC Build Tools and CMake), and `crafting-bin\designcraft\rebuild.ps1` for DesignCraft.
2. Start the monitor: `gateway\start-monitor.cmd`. Pick the install folder of each app (`crafting-bin\<app>`) and
   press Start gateway. Or use the CLI: `python gateway\gatewayctl.py config set-path designcraft <folder>` and
   `python gateway\gatewayctl.py start`.
3. Either install the skill for Claude Code as a link, so it stays in sync with the repository:
   `mklink /J "%USERPROFILE%\.claude\skills\craft-apps" "<root>\skill\craft-apps"`, and run the client with
   `python skill\craft-apps\scripts\craftmcp.py status`; or register the apps with any MCP client, for example
   `claude mcp add --transport http designcraft http://127.0.0.1:7970/designcraft` (the start page at
   `http://127.0.0.1:7970/` lists the line for every app).

## Command search

`craftmcp.py find <app> "what you want to do"` embeds the request with `BAAI/bge-small-en-v1.5` and compares it with
the vectors in `gateway/index/`. The embedder is [fastembed](https://github.com/qdrant/fastembed)
(`pip install numpy fastembed`; the model, about 130 MB, is downloaded on first use). Optionally the 20 nearest
candidates are re-ranked by a System One compatible judge server (`CRAFT_SYSTEM_ONE_URL`,
`CRAFT_SYSTEM_ONE_MODEL`). On the 82 queries of `index eval` the vector order alone puts an accepted answer first
for 50 and finds one (top 3 or a note shown as a tip) for 68; with re-ranking the numbers are 57 and 82.
After an app is rebuilt, `index doctor` reports new, changed and removed commands and `index sync` updates the
index (new commands get their descriptions from an LLM agent CLI, `pi`, when it is installed; otherwise they are
searchable by name and parameters).

## Security

The gateway has **no authentication**: any process on the machine that can reach `127.0.0.1:7970` can drive the
configured apps. It binds to loopback only, rejects non-loopback `Host` and `Origin` headers, takes executable
paths only from its config file, and never stops an app with unsaved documents unless asked to force it. Details
are in [gateway/README.md](gateway/README.md#trust-model-read-this). Do not expose the port to a network.

## Status

- Developed and tested on Windows 11 with Python 3.12. Other platforms are untested.
- PhotoCraft, LightCraft and PdfCraft need small automation patches that are not upstream yet; stock builds of
  those three do not work with the gateway (see [gateway/README.md](gateway/README.md#slots)).
- The skill's texts assume Claude Code; the client `craftmcp.py` itself is a plain Python 3.12 script and works
  from any shell.

## License

Licensed under either of [Apache License 2.0](LICENSE-APACHE) or [MIT](LICENSE-MIT), at your option, like the
Crafting Apps themselves.
