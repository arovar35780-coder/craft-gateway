# Book builder for DesignCraft

Builds a book (title page, optional copyright page, contents, introduction, chapters, conclusion)
from Markdown files into a `.designcraft` file and a PDF, using a template with masters and styles.

## Quick start for a new book
```
python new_book.py my_book --title "My Book" --author "Jane Writer" --subtitle "..." --publisher "..." --chapters 12
# creates ../my_book/ with book.toml, text/ and out/; edit text/*.md (stubs), then:
python build.py ../my_book/book.toml
```
The build prints a check report, saves it as `out/<name>.report.txt` and exits with code 1 when there are errors.

## Files
- `build.py` — the builder (the generic tool). `python build.py ../my_book/book.toml`
- Books live in `../<name>/` (manifest, `text/` Markdown sources, `out/` results). `example.toml` is the reference manifest with every option; copy it for a new book or use `new_book.py`.
- `convert.py` — Markdown to sections through pandoc (needs `pandoc` on PATH).
- `dc.py` — client for the DesignCraft control channel (port 7979).
- `new_book.py` — scaffold for a new book (folder, manifest, stub Markdown files).
- `checks.py` — quality checks after a build (see below).
- `make_clean_template.py` — makes `../templates/book-6x9.designcraft` (one empty page, masters, styles, header variables) from a full template.
- `../templates/book-6x9.designcraft` — the template (6x9 in, one empty page, masters A/B/C, styles, running-header variables).

## Build a book
1. Start the app with the control channel: `crafting-bin/start-designcraft.cmd` (or start it through the gateway). After changes to the DesignCraft sources run `crafting-bin/designcraft/rebuild.ps1`.
2. Put the Markdown files in a folder. Headings: `#` chapter/section title, `##` and `###` subsections; `*italic*` and `**bold**` are kept. Lists and tables are not supported yet (the builder stops with a message).
3. Copy `example.toml` into the new project folder, set `template` (relative to the manifest), `source`, `output` and the `[[section]]` list, then `python build.py <manifest>.toml`.
4. A full 136 page book takes about 2.5 minutes. `--only N` builds the first N sections, `--no-pdf` skips the PDF.

## What the manifest controls
- `[[section]]`: `kind` = `title`, `copyright` (optional page 2 block, each source line is a paragraph), `front` / `back` (opener with the matter heading style), `chapter`; `file` or `glob` (natural order, so chapter-2 comes before chapter-10).
- `[toc]`, `[numbering] front_matter` (`lowerRoman`, `upperRoman`, ... or empty for one arabic numbering), `[masters]`, `[layout]` margins, `[styles]` names.
- Page size comes from the template. If you change margins or masters in the template, update `[layout]` to match.

## Conventions the template relies on
- Running headers: left shows the title-style paragraph (book title), right shows the text with the character style `Running Head`, which the builder puts on the short heading prefix ("Chapter 3", "Introduction"). Headings of the form `Prefix. Title` or `Prefix: Title` work.
- Contents is built from the chapter and matter heading styles with the styles `TOC Title` and `TOC Level 1`.
- Fonts: EB Garamond must be installed (the template uses only that family). The unused decorative character styles of the original were deleted with `style.character.delete`; `italic`, `Footnote reference` and `Endnote reference` were kept and point at EB Garamond.

## Checks after a build (`checks.py`)
- errors (exit code 1): overset text, a blank page that is not the intended one, contents entries that differ from the pages the sections start on, DesignCraft preflight errors (missing fonts or links).
- warnings: the last page of a chapter with only a few lines (`shortLast`), mostly empty body pages (`sparse`), preflight warnings.
- The page checks render every page and need Pillow (`pip install pillow`); without it only a warning is printed. `--no-checks` skips everything.
