"""Make projects/templates/book-6x9.designcraft from a full template: one empty page, masters/styles/variables kept.

Usage: python make_clean_template.py source.designcraft [target.designcraft]
The app must be running with the control channel on port 7979.
"""
import sys
from pathlib import Path

from dc import DC

HERE = Path(__file__).resolve().parent
if len(sys.argv) < 2:
    sys.exit("usage: python make_clean_template.py source.designcraft [target.designcraft]")
SRC = Path(sys.argv[1])
DST = Path(sys.argv[2]) if len(sys.argv) > 2 else HERE.parent / "templates" / "book-6x9.designcraft"

d = DC()
d.x("file.open", path=str(SRC))
n = d.inspect()["pageCount"]
if n > 1:
    d.x("layout.pages.delete", pages=list(range(1, n)))
for it in d.inspect()["spreads"][0]["items"]:
    d.x("edit.clear", ids=[it["id"]])
d.x("layout.pages.applyParent", pages=[0], parent=None)
ins = d.inspect()
print("pages:", ins["pageCount"], "items on page 1:", len(ins["spreads"][0]["items"]), "parents:", [p["label"] for p in ins["parents"]])
print("stories left:", [(s["id"], s["length"]) for s in ins["stories"]])
print(d.x("file.saveAs", path=str(DST)))
