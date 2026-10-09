---
name: craft-apps
description: Drive the Crafting Apps (DesignCraft, VectorCraft, PhotoCraft, LightCraft, FilmCraft, PdfCraft, EffectCraft) through the local craft-gateway with the `craftmcp.py` client, no MCP registration needed. Find tools and commands by plain-language search, create and edit layouts, books, flyers, vector art, photos, video and PDFs, render and check results. Not for Adobe apps (InDesign is driven through COM/ExtendScript).
---

# Crafting Apps through craft-gateway

One loopback gateway (default port 7970, no authentication; the `gateway/` folder of the craft-gateway repository this skill comes from) starts the seven apps (slots: designcraft, vectorcraft, photocraft, lightcraft, filmcraft, pdfcraft, effectcraft) and their MCP bridges on demand; `scripts/craftmcp.py` turns any tool of any app into one shell command. The user sets the install folders in the monitor (`gateway\start-monitor.cmd`). photocraft, lightcraft and pdfcraft need builds with the gateway automation patches. Restart the gateway after changing its code.

```
C="python $USERPROFILE/.claude/skills/craft-apps/scripts/craftmcp.py"
$C status                               # gateway and app states (exit 2: gateway down -> $C gateway-start)
$C find <slot> "what you want to do"    # START HERE: the 5 best tools / engine commands with params and a ready run/call line
$C run <slot> <command> '{"param":1}'   # run an engine command (same call for every app)
$C call <slot> <tool> key=value ...     # call an MCP tool (key=value, one JSON object, @file.json, or - for stdin)
$C new|inspect|render|export|save <slot> [key=value ...]   # common verbs, mapped per app (`$C verbs <slot>`)
$C check <designcraft|photocraft>       # before calling work finished
$C cleanup <slot>                       # dry run: your throwaway documents; add --clean --yes (see commands.md)
$C stop <slot>                          # refuses when documents are unsaved
```
Everything else (`tools`, `describe`, `commands`, `index doctor|sync`, `ensure`, `port`, ...): `$C --help` and [reference/commands.md](reference/commands.md).

## How to work
1. `$C status`. Gateway down -> `$C gateway-start`. A slot shown `disabled` needs a path: ask the user to set it in the monitor (never write paths through HTTP).
2. **Find, do not browse**: `$C find <slot> "task in plain words"` (apps have hundreds of commands; after an app was rebuilt run `$C index doctor`). Prefer commands over `click`/`drag`/`pointer`.
3. **Look at results with the app's render tool**, not a window screenshot (table in [reference/apps.md](reference/apps.md)). Before calling a layout finished: `$C check designcraft`, read it, render a page, look at the image. Images are saved to `%TEMP%\craft-mcp\*.png`: Read the path it prints.
4. **Long jobs** (more than about two minutes, hundreds of commands) belong in a script on the app's control channel, not in MCP calls: `DESIGNCRAFT_PORT=$($C port designcraft) python projects/book_build/build.py <manifest>` (7979 is the port of a DesignCraft started by hand). For many drawing calls write a short Python script that imports `craftmcp.py` (`session(slot)`, `rpc(slot, "tools/call", {...})`).

## Safety rules
- Never use `stop --force`, close an app, or kill a process the user started. The gateway only manages apps it started itself; an app the user started by hand (for example DesignCraft on port 7979) is a separate instance.
- `stop` refusing with "unsaved documents" or "could not check" is the safety net: report it and ask the user. Force only for throwaway documents you created yourself in this session, and say so. `cleanup` never closes unsaved documents unless `--dirty-match` names them.
- The gateway has no token: keep it on loopback, put no secrets in tool arguments, keep payloads out of its log (`gatewayctl logs` is already free of them).

## Reference (read when needed)
- [reference/commands.md](reference/commands.md): every subcommand with examples (calling, verbs, find/index, check, cleanup).
- [reference/apps.md](reference/apps.md): which render tool each app has (per-app traps and recipes are notes in the search index: `find` shows them).
- [reference/troubleshooting.md](reference/troubleshooting.md): errors and fixes, and where the apps, gateway, index and projects live.
