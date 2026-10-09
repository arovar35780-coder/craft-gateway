# App notes

## Looking at the result: use the render tools, not window screenshots
Window screenshots (`screenshot`, `ui_screenshot`) depend on what is on the screen (a covered or minimized window hangs or shows panels). Prefer the engine renders:

| app | tool | what it renders |
|---|---|---|
| designcraft | `render_page page=N scale=` | page from the document (checked with the window minimized) |
| vectorcraft | `screenshot` | the active artboard rendered to PNG (also `export`) |
| lightcraft | `render_photo` | photo with its develop settings (also `export`) |
| pdfcraft | `page_render doc= page= dpi=` | PDF page from the bridge's OWN session: `doc_open path=<pdf>` first (documents opened with `ui_open` are invisible to it). Verified with the window minimized (0.3 s) |
| filmcraft | `render_frame seconds= max_side=` | engine render of the program frame (`seq.render`); works with the window minimized; moves the playhead when `seconds` is given. Builds older than the `seq.render` commit fall back to a window screenshot that hangs when minimized |
| effectcraft | `render_frame comp=<id> time=` | comp frame; always pass `comp` (the default is the active comp, which may be another one) |
| photocraft | `doc_render_preview max_side=` | engine render of the flattened canvas (`doc.render`); works with the window minimized. Builds older than the `doc.render` commit fall back to a whole-window screenshot |

## Per-app traps and recipes live in the search index
Traps and recipes (DesignCraft `place_image`, silent font fallback, VectorCraft drawing, PhotoCraft export and gradients, EffectCraft scripting, shape operators and effects, PdfCraft sessions) are notes in `gateway\index\<slot>.notes.json`. They are found by `$C find <slot> "what you want to do"` next to the commands they concern, and `$C index doctor` checks that the ids they mention still exist. Add a new trap there (id, title, text, keys, see), then `$C index sync <slot>` (no Pi needed for notes); do not copy it into this file or the knowledge base unless it is a general lesson.
