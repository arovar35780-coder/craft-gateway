"""Markdown -> section dict via pandoc's JSON AST.

A section is
  {"file": "chapter-1.md", "kind": "title|copyright|front|chapter|back",
   "blocks": [{"type": "h1|h2|h3|h4|p|ul|hr", "runs": [[text, "i"|"b"|"bi"|""], ...]}]}
Runs carry only text and emphasis; styles are chosen later by build.py.
Anything pandoc produces that is not handled here raises, so nothing is silently dropped.
"""
import json
import re
import subprocess
from pathlib import Path

QUOTES = {"SingleQuote": ("‘", "’"), "DoubleQuote": ("“", "”")}


def pandoc_ast(path: Path, lines_are_paragraphs: bool = False) -> dict:
    # +smart (pandoc's default for markdown) gives curly quotes, en/em dashes and ellipses.
    cmd = ["pandoc", "-f", "markdown+smart", "-t", "json"]
    if lines_are_paragraphs:
        # Copyright pages are written line by line: every source line is its own paragraph.
        text = re.sub(r"(?<!\n)\n(?!\n)", "\n\n", path.read_text(encoding="utf-8"))
        out = subprocess.run(cmd, input=text.encode("utf-8"), capture_output=True, check=True)
    else:
        out = subprocess.run(cmd + [str(path)], capture_output=True, check=True)
    return json.loads(out.stdout)


def runs_of(inlines, fmt=""):
    """Flatten inlines to [[text, fmt], ...]."""
    runs = []

    def add(text, f):
        if text:
            runs.append([text, f])

    def walk(items, f):
        for x in items:
            t = x["t"]
            if t == "Str":
                add(x["c"], f)
            elif t in ("Space", "SoftBreak"):
                add(" ", f)
            elif t == "LineBreak":
                add("\n", f)
            elif t == "Emph":
                walk(x["c"], "".join(sorted(set(f + "i"))))
            elif t == "Strong":
                walk(x["c"], "".join(sorted(set(f + "b"))))
            elif t == "Quoted":
                q = QUOTES[x["c"][0]["t"]]
                add(q[0], f)
                walk(x["c"][1], f)
                add(q[1], f)
            elif t == "Link":
                walk(x["c"][1], f)
            elif t in ("Span", "Underline"):
                walk(x["c"][1] if t == "Span" else x["c"], f)
            elif t == "Code":
                add(x["c"][1], f)
            else:
                raise ValueError(f"unhandled inline {t}")

    walk(inlines, fmt)
    merged = []
    for text, f in runs:
        if merged and merged[-1][1] == f:
            merged[-1][0] += text
        else:
            merged.append([text, f])
    return merged


def blocks_of(blocks):
    out = []
    for b in blocks:
        t = b["t"]
        if t == "Header":
            out.append({"type": f"h{b['c'][0]}", "runs": runs_of(b["c"][2])})
        elif t in ("Para", "Plain"):
            out.append({"type": "p", "runs": runs_of(b["c"])})
        elif t == "BulletList":
            for item in b["c"]:
                for ib in item:
                    if ib["t"] not in ("Para", "Plain"):
                        raise ValueError(f"unhandled list item block {ib['t']}")
                    out.append({"type": "ul", "runs": runs_of(ib["c"])})
        elif t == "HorizontalRule":
            out.append({"type": "hr", "runs": []})
        else:
            raise ValueError(f"unhandled block {t}")
    return out


def convert_file(path: Path, kind: str) -> dict:
    ast = pandoc_ast(path, lines_are_paragraphs=(kind == "copyright"))
    return {"file": path.name, "kind": kind, "blocks": blocks_of(ast["blocks"])}
