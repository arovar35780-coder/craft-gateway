# craftmcp.py: every subcommand

`C="python $USERPROFILE/.claude/skills/craft-apps/scripts/craftmcp.py"`. `$C --help` lists them; `$C <command> --help` shows the options.

## Gateway and apps
```
$C status [--dirty]                  # gateway and app states (exit 2: gateway down); --dirty also lists unsaved documents
$C gateway-start | gateway-stop      # start/stop the gateway
$C ensure <slot>                     # start the app if needed, print its control port
$C port <slot>                       # control port of the running app (for scripts: DESIGNCRAFT_PORT=$($C port designcraft))
$C stop <slot> [--force]             # stop an app; refuses when documents are unsaved (never --force on the user's work)
```

## Calling tools and commands
```
$C tools <slot> [--filter x]         # list the app's MCP tools (names; do not guess them)
$C describe <slot> <tool>            # description and JSON schema of one tool
$C call <slot> <tool> key=value ...  # call an MCP tool (key=value, one JSON object, @file.json, or - for stdin)
$C commands <slot> [filter=x]        # the app's engine commands with parameter docs (large: prefer `find`)
$C run <slot> <command> '{"param":1}'    # run an engine command; hides execute / run_command / command_run / execute_command and `command` vs `id` (old name: exec)
$C run designcraft frame.create '{"rect":[72,72,500,200],"content":"text","text":"Hi"}'     # example
$C rpc <slot> <method> ...           # raw JSON-RPC to the app's bridge
```

## Common verbs (one call, mapped to each app's own tools; `$C verbs <slot>` shows the mapping)
```
$C new <slot> [key=value...]         # new document (designcraft, photocraft, pdfcraft; vectorcraft via file.new, effectcraft via comp.new; lightcraft and filmcraft have none)
$C inspect <slot> [key=value...]     # state of the document / project / UI
$C render <slot> [key=value...]      # an image of the work; extra arguments per app: page=, comp=, seconds=, doc= (see apps.md)
$C export <slot> path=<file>         # another format (png etc.); photocraft: an absolute path is exported via the gateway exchange folder and copied there
$C save <slot> path=<file>           # save (designcraft, vectorcraft, photocraft, pdfcraft, effectcraft); photocraft: an absolute path goes through the exchange folder like export
$C open photocraft path=<file>       # photocraft only: an absolute path is copied to <exchange>\in\ and opened from there (the app reads only below the exchange folder)
```
PhotoCraft works on the copy: `save photocraft` without `path=` writes the copy in the exchange folder, so save with an absolute `path=` to update the original.
Images are saved to `%TEMP%\craft-mcp\*.png`: Read the path the command prints.

## Finding tools and commands (semantic search, offline)
```
$C find <slot> "what you want to do"  # the 5 best MCP tools / engine commands with params and a ready run/call line; --kind tool|cmd, --top N, --shortlist N, --no-rerank, --json
$C notes <slot> [query]               # list the traps and recipes of an app, or search them (offline)
$C find <slot> "..." --no-tips          # tools, commands and effects only, without the notes
$C index status [slot]                # what is indexed (offline)
$C index doctor [slot ...]            # compare the index with the REAL apps (starts them if needed): new / removed / changed tools and commands, stale vectors; exit 1 = drift, 2 = an app could not be checked
$C index sync [slot ...]              # fix drift: drop removed, describe new and changed commands and effects with Pi, re-embed (--no-describe: skip Pi)
$C index eval [slot] [--misses]       # search quality regression over gateway/index/eval_queries.json (first / top-3 rates)
```
Items are `tool`, `cmd`, `effect` (EffectCraft's effect registry) and `note` (hand-written traps and recipes in `gateway/index/<slot>.notes.json`: id, title, text, keys, see; `index sync` embeds them, `doctor` checks that the ids in `see` exist). `find` ranks tools, commands and effects with one System One `choice` question and shows a note as a `tip` after the list when a separate yes/no question finds it useful for the task (a note is advice next to the answer, not a competing answer). `find --kind tool|cmd|effect|note` (`note` searches the notes themselves). `index eval` counts a query as found when an accepted answer is in the top 3 or shown as a tip.
Run `index doctor` after an app was rebuilt. Index files live in `gateway\index\` (override with `CRAFT_INDEX_DIR`); tests: `python -m unittest test_craftindex` in `scripts\`.

## Checks and cleanup
```
$C check designcraft                  # preflight + items off the page + text characters the font has NO glyph for (the app silently falls back); exit 1 on errors
$C check photocraft                   # canvas size, transparency, content bounds, centring offset of the open document
$C cleanup <slot>                     # dry run: lists open documents and what would be closed (designcraft, photocraft, vectorcraft)
$C cleanup <slot> --clean --yes       # close documents WITHOUT unsaved changes
$C cleanup <slot> --dirty-match '^probe$' --yes   # also discard UNSAVED documents whose title matches (only your own throwaways; unmatched unsaved documents are never closed)
```
