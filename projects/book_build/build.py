"""Build a book in DesignCraft from a manifest (see example.toml) and Markdown files.

Usage: python build.py book.toml [--only N] [--no-pdf] [--out PATH]
  --only N   build only the first N sections (title counts as one)
  --out PATH base path for the .designcraft/.pdf outputs (default: `output` of the manifest)
The app must be running with the control channel on port 7979 (designcraft --control 7979).

Rules: title, copyright and every opener page are laid out from the template's masters; each
section is one story, styled paragraph by paragraph before it flows, then threaded across as many
pages as it needs. The contents page is reserved before the body so its page numbers are final.
"""
import argparse
import re
import sys
import time
import tomllib
from pathlib import Path

import checks
from convert import convert_file
from dc import DC

DEFAULT_STYLES = {
    "title": "Book Title (Front)",
    "subtitle": "Book Subtitle (Front)",
    "author": "Author Name and Publishing House (Front)",
    "publisher": "Author Name and Publishing House (Front)",
    "copyright": "Copyright",
    "matter_heading": "Front or Back Matter (In TOC)",
    "chapter_heading": "Heading 1",
    "heading2": "Heading 2",
    "heading3": "Heading 3",
    "body_first": "Body (No Indent)",
    "body": "Body (Indented)",
    "running_head": "Running Head",
}
DEFAULT_LAYOUT = {"opener_top": 98.0, "body_top": 53.0, "bottom": 67.0, "inside": 58.0, "outside": 50.0, "copyright_top": 405.75, "halftitle_top": 204.75, "title_top": 177.75}
MAX_PAGES_PER_SECTION = 80
# The short part of a section heading that goes in the running header: "Chapter 1", "Introduction", "Conclusion".
HEAD_PREFIX = re.compile(r"^(Chapter \d+|[^.:]+?)(?=[.:]\s)")


def b(s: str) -> int:
    return len(s.encode("utf-8"))


def natural_key(name: str):
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def load_manifest(path: Path) -> dict:
    m = tomllib.loads(path.read_text(encoding="utf-8"))
    base = path.parent
    m["styles"] = {**DEFAULT_STYLES, **m.get("styles", {})}
    m["layout"] = {**DEFAULT_LAYOUT, **m.get("layout", {})}
    m["masters"] = {"opener": "C", "body": "A", "title": "B", "blank": "B", **m.get("masters", {})}
    m["toc"] = {"enabled": True, "title": "Contents", "blank_after": True, **m.get("toc", {})}
    m["title_page"] = {"title_size": 0, **m.get("title_page", {})}
    m["numbering"] = {"front_matter": "", **m.get("numbering", {})}
    m["source_dir"] = (base / m["source"]).resolve()
    m["template_path"] = (base / m["template"]).resolve()
    m["output_base"] = (base / m["output"]).resolve()
    return m


def collect_sections(m: dict) -> list:
    """Convert the manifest's sections to section dicts, in reading order."""
    src = m["source_dir"]
    out = []
    for entry in m["section"]:
        kind = entry["kind"]
        if "glob" in entry:
            files = sorted(src.glob(entry["glob"]), key=lambda p: natural_key(p.name))
            if not files:
                raise FileNotFoundError(f"no files match {entry['glob']} in {src}")
        else:
            f = src / entry["file"]
            if not f.exists():
                if entry.get("optional"):
                    continue
                raise FileNotFoundError(f)
            files = [f]
        out += [convert_file(f, kind) for f in files]
    return out


def para_styles(sec, styles):
    """[(text, style, runs, running-head byte length or None)] for a section."""
    out = []
    after_heading = True
    for blk in sec["blocks"]:
        t = blk["type"]
        text = "".join(r[0] for r in blk["runs"])
        kind = sec["kind"]
        if kind == "copyright":
            style = styles["copyright"]
        elif kind in ("title", "halftitle"):
            if kind == "halftitle" and t not in ("h1", "h2"):
                continue  # the half-title carries only the title and subtitle
            style = {"h1": styles["title"], "h2": styles["subtitle"], "h3": styles["author"], "h4": styles["publisher"]}[t]
        elif t == "h1":
            style = styles["matter_heading"] if kind in ("front", "back") else styles["chapter_heading"]
        elif t == "h2":
            style = styles["heading2"]
        elif t == "h3":
            style = styles["heading3"]
        elif t == "p":
            style = styles["body_first"] if after_heading else styles["body"]
        else:
            raise ValueError(f"no style for block {t} in {sec['file']}")
        after_heading = t.startswith("h") or t == "hr"
        head = None
        if t == "h1" and kind not in ("title", "halftitle"):
            m = HEAD_PREFIX.match(text)
            head = b(m.group(1)) if m else None
        out.append((text, style, blk["runs"], head))
    return out


class Builder:
    def __init__(self, dc: DC, m: dict):
        self.d = dc
        self.m = m
        self.styles = m["styles"]
        self.lay = m["layout"]
        self.masters = m["masters"]
        self.total = 1

    # -- pages -------------------------------------------------------------------------
    # Page geometry is computed here rather than read back: document.inspect composes every
    # story, which gets slow as the book grows. Facing pages: page 0 is a lone right page
    # (spread 0); then left+right pairs, the right page offset by one page width in the spread.
    def setup(self):
        d = self.d
        d.x("file.open", path=str(self.m["template_path"]))
        ins = d.inspect()
        self.page_w, self.page_h = ins["settings"]["pageWidth"], ins["settings"]["pageHeight"]
        n = ins["pageCount"]
        if n > 1:
            d.x("layout.pages.delete", pages=list(range(1, n)))
        for it in d.inspect()["spreads"][0]["items"]:
            d.x("edit.clear", ids=[it["id"]])
        d.x("layout.pages.applyParent", pages=[0], parent=None)
        self.total = 1

    @staticmethod
    def page_spread(idx):
        return (idx + 1) // 2

    @staticmethod
    def page_side(idx):
        return "right" if idx % 2 == 0 else "left"

    def top(self, parent):
        return self.lay["body_top"] if parent == self.masters["body"] else self.lay["opener_top"]

    def add_page(self, parent):
        r = self.d.x("layout.pages.insert", count=1, parent=parent)
        self.total = r["total"]
        idx = self.total - 1
        lay = self.lay
        self.d.x(
            "layout.marginsAndColumns",
            pages=[idx],
            margins={"top": self.top(parent), "bottom": lay["bottom"], "inside": lay["inside"], "outside": lay["outside"]},
        )
        return idx

    def frame_rect(self, idx, parent):
        lay = self.lay
        x0 = self.page_w if self.page_side(idx) == "right" and idx > 0 else 0.0
        x1 = x0 + self.page_w
        if self.page_side(idx) == "left":
            return [x0 + lay["outside"], self.top(parent), x1 - lay["inside"], self.page_h - lay["bottom"]]
        return [x0 + lay["inside"], self.top(parent), x1 - lay["outside"], self.page_h - lay["bottom"]]

    # -- text --------------------------------------------------------------------------
    def fill_story(self, story, paras):
        d = self.d
        d.x("story.setText", story=story, text="\n".join(p[0] for p in paras))
        off = 0
        for text, style, runs, head in paras:
            end = off + b(text)
            d.x("text.select", story=story, anchor=off, focus=end)
            d.x("style.paragraph.apply", name=style)
            if style == self.styles["title"] and self.m["title_page"]["title_size"]:
                # The original template sets the title smaller than its paragraph style.
                d.x("type.char", attrs={"size": self.m["title_page"]["title_size"]})
            if head:
                d.x("text.select", story=story, anchor=off, focus=off + head)
                d.x("style.character.apply", name=self.styles["running_head"])
            ro = off
            for rt, fmt in runs:
                rb = b(rt)
                if fmt:
                    d.x("text.select", story=story, anchor=ro, focus=ro + rb)
                    if "i" in fmt:
                        d.x("type.italic")
                    if "b" in fmt:
                        d.x("type.bold")
                ro += rb
            off = end + 1
        d.x("text.select", story=story, anchor=0, focus=0)

    def overset(self, story):
        return self.d.x("story.get", story=story)["overset"]

    # -- sections ----------------------------------------------------------------------
    def build_section(self, sec, first_page=None):
        d = self.d
        opener = self.masters["opener"]
        kind = sec["kind"]
        if first_page is None:
            parent = {"copyright": None, "title": self.masters["title"]}.get(kind, opener)
            idx = self.add_page(parent)
        else:
            parent, idx = None, first_page
        rect = self.frame_rect(idx, parent)
        top = {"copyright": "copyright_top", "halftitle": "halftitle_top", "title": "title_top"}.get(kind)
        if top:
            rect[1] = self.lay[top]
        r = d.x("frame.create", spread=self.page_spread(idx), rect=rect, content="text", text="x", caret=False)
        frame, story = r["id"], r["story"]
        self.fill_story(story, para_styles(sec, self.styles))
        n = 1
        while self.overset(story) is not None:
            if n >= MAX_PAGES_PER_SECTION:
                raise RuntimeError(f"{sec['file']}: still overset after {n} pages")
            idx = self.add_page(self.masters["body"])
            t = d.x("frame.thread", **{"from": frame, "rect": self.frame_rect(idx, self.masters["body"]), "spread": self.page_spread(idx)})
            frame = t["to"]
            n += 1
        return {"file": sec["file"], "story": story, "pages": n, "last_page": idx}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest")
    ap.add_argument("--only", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--no-pdf", action="store_true")
    ap.add_argument("--no-checks", action="store_true", help="skip the quality checks (errors make the exit code 1)")
    a = ap.parse_args()
    m = load_manifest(Path(a.manifest).resolve())
    sections = collect_sections(m)
    if a.only:
        sections = sections[: a.only]
    d = DC()
    bl = Builder(d, m)
    bl.setup()
    toc_page = body_first = blank_idx = None
    report = []
    for i, sec in enumerate(sections):
        if toc_page is None and m["toc"]["enabled"] and sec["kind"] not in ("halftitle", "title", "copyright"):
            toc_page = bl.add_page(m["masters"]["opener"])  # reserved; generated after the body exists
            if m["toc"]["blank_after"]:
                blank_idx = bl.add_page(m["masters"]["blank"])  # the empty page that follows the contents (no number by default)
            body_first = bl.total
        t0 = time.time()
        r = bl.build_section(sec, first_page=0 if i == 0 else None)
        r["sec"] = round(time.time() - t0, 1)
        print(r, flush=True)
        report.append({"file": r["file"], "kind": sec["kind"], "first": r["last_page"] - r["pages"] + 1, "last": r["last_page"]})
    front = m["numbering"]["front_matter"]
    if toc_page is not None and front:
        # Front matter (title, copyright, contents) in its own style; the body restarts at 1 in arabic.
        # Sections come before the contents are generated so its page numbers use the final names.
        d.x("layout.section", page=1, startNumber=1, style=front)
        d.x("layout.section", page=body_first + 1, startNumber=1, style="arabic")
    if toc_page is not None:
        st = m["styles"]
        entries = [{"style": st["chapter_heading"], "level": 1}, {"style": st["matter_heading"], "level": 1}]
        t = d.x("toc.generate", entries=entries, title=m["toc"]["title"], pageNumbers=True, page=toc_page + 1)
        print("toc:", t, "overset:", bl.overset(t["story"]))
    ins = d.inspect()
    print("pages:", ins["pageCount"], "overset stories:", [s["id"] for s in ins["stories"] if s["overset"]])
    base = str(Path(a.out).resolve()) if a.out else str(m["output_base"])
    Path(base).parent.mkdir(parents=True, exist_ok=True)
    errors = 0
    if not a.no_checks:
        findings, _ = checks.run(d, m, report, body_first or len(report), blank_idx, bl.page_h)
        text, errors = checks.format_report(findings, Path(base).name)
        print(text)
        Path(base + ".report.txt").write_text(text + chr(10), encoding="utf-8")
    print(d.x("file.saveAs", path=base + ".designcraft"))
    if not a.no_pdf:
        print(d.x("file.exportPdf", path=base + ".pdf"))
    if errors:
        print(f"BUILD HAS {errors} ERROR(S): see {base}.report.txt")
        sys.exit(1)


if __name__ == "__main__":
    main()
