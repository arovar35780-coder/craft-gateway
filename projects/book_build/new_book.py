"""Create a new book project from the template.

Usage: python new_book.py <slug> --title "Book Title" --author "Author Name" [--subtitle "..."]
                          [--publisher "..."] [--chapters 3] [--year 2026] [--dir ..]

Makes <dir>/<slug>/ (default: next to this folder in projects/<slug>/, snake_case like the other projects) with book.toml (the manifest), text/ (stub Markdown files to replace with the
real text) and out/. Then build it with `python build.py <dir>/<slug>/book.toml`.
The template is <projects>/templates/book-6x9.designcraft (the path in the manifest is relative to the new folder).
"""
import argparse
import datetime
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

MANIFEST = """# Book manifest (see example.toml next to new_book.py for every option and README.md for the rules).
template = "{template}"
source = "text"
output = "out/{slug}"

[[section]]
kind = "halftitle"
file = "title.md"

[[section]]
kind = "copyright"
file = "copyright.md"
optional = true

[[section]]
kind = "title"
file = "title.md"

[[section]]
kind = "front"
file = "introduction.md"
optional = true

[[section]]
kind = "chapter"
glob = "chapter-*.md"

[[section]]
kind = "back"
file = "conclusion.md"
optional = true

[toc]
enabled = true
title = "Contents"
blank_after = true

[title_page]
title_size = 33

[numbering]
front_matter = "lowerRoman"
"""


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("slug")
    ap.add_argument("--title", required=True)
    ap.add_argument("--author", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--publisher", default="")
    ap.add_argument("--chapters", type=int, default=3)
    ap.add_argument("--year", type=int, default=datetime.date.today().year)
    ap.add_argument("--dir", default=str(HERE.parent))
    a = ap.parse_args()

    slug = slugify(a.slug)
    root = Path(a.dir).resolve() / slug
    if root.exists():
        raise SystemExit(f"{root} already exists; nothing was changed")
    (root / "text").mkdir(parents=True)
    (root / "out").mkdir()

    template = (HERE.parent / "templates" / "book-6x9.designcraft").resolve()
    rel = Path(__import__("os").path.relpath(template, root)).as_posix()
    (root / "book.toml").write_text(MANIFEST.format(template=rel, slug=slug), encoding="utf-8")

    title = f"# {a.title}\n\n"
    if a.subtitle:
        title += f"## {a.subtitle}\n\n"
    title += f"### {a.author}\n"
    if a.publisher:
        title += f"\n#### {a.publisher}\n"
    files = {"title.md": title}
    files["copyright.md"] = (
        f"Copyright © {a.year} {a.author.upper()}\n\n"
        "All rights reserved. No part of this publication may be reproduced, distributed or transmitted in any form or by any means, "
        "including photocopying, recording, or other electronic or mechanical methods, without the prior written permission of the publisher, "
        "except in the case of brief quotations embodied in critical reviews and certain other non-commercial uses permitted by copyright law.\n\n"
        f"{a.title}" + (f": {a.subtitle}" if a.subtitle else "") + "\n"
    )
    files["introduction.md"] = "# Introduction: Replace With Your Introduction\n\nReplace this text with the introduction.\n\nA second paragraph, with *italic* words.\n"
    for n in range(1, a.chapters + 1):
        files[f"chapter-{n}.md"] = (
            f"# Chapter {n}. Replace With the Title of Chapter {n}\n\n## First section\n\n"
            f"Replace this text with chapter {n}.\n\nA second paragraph, with *italic* words.\n"
        )
    files["conclusion.md"] = "# Conclusion: Replace With Your Conclusion\n\nReplace this text with the conclusion.\n"
    for name, text in files.items():
        (root / "text" / name).write_text(text, encoding="utf-8")

    print(f"Created {root}")
    print("  edit text/*.md (the files are stubs), delete the ones you do not need,")
    print("  and add copyright details; the ISBN line is not included.")
    print(f"  build:  python build.py {root / 'book.toml'}")


if __name__ == "__main__":
    main()
