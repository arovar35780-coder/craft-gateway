"""What is really inside a print PDF: colour operators, spot colours, output intent, boxes, images and their colour spaces.

usage: python pdf_inspect.py file.pdf          (needs PyMuPDF: pip install pymupdf)
Used with build_probe.py, which makes a DesignCraft document with CMYK, spot, RGB, transparency and a drop shadow.
"""
import re
import sys

import pymupdf as fitz

path = sys.argv[1]
doc = fitz.open(path)
page = doc[0]
raw = open(path, "rb").read()
catalog = doc.xref_object(doc.pdf_catalog())
print("file:", path, "|", doc.metadata.get("format"), "| pages:", doc.page_count)
print("boxes: media", page.mediabox, "| trim", page.trimbox, "| bleed", page.bleedbox)

m = re.search(r"/OutputIntents \[ (\d+) 0 R", catalog)
if m:
    obj = doc.xref_object(int(m.group(1))).replace("\n", " ")
    cond = re.search(r"/OutputCondition \((.*?)\)\s+/", obj)
    prof = re.search(r"/DestOutputProfile (\d+) 0 R", obj)
    size = len(doc.xref_stream(int(prof.group(1)))) if prof else 0
    print("output intent: %s | condition: %s | embedded profile: %d bytes" % (re.search(r"/S /(\w+)", obj).group(1), cond.group(1) if cond else "?", size))
else:
    print("output intent: none")
print("PDF/X marker:", b"/GTS_PDFXVersion" in raw, "| spot (/Separation):", raw.count(b"/Separation"), "| ICCBased:", raw.count(b"/ICCBased"))

content = b"".join(doc.xref_stream(x) for x in page.get_contents())
ops = {op: len(re.findall(rb"(?<![A-Za-z])%s(?![A-Za-z])" % op.encode(), content)) for op in ("k", "K", "g", "G", "rg", "RG", "scn")}
print("page colour operators (k/K = CMYK, g/G = gray, rg/RG = RGB, scn = spot):", ops)
for img in page.get_images(full=True):
    obj = doc.xref_object(img[0]).replace("\n", " ")
    cs = re.search(r"/ColorSpace /(\w+)", obj)
    w, h = re.search(r"/Width (\d+)", obj), re.search(r"/Height (\d+)", obj)
    print("image xref %d: %sx%s px, colour space %s%s" % (img[0], w.group(1), h.group(1), cs.group(1) if cs else "?", ", with soft mask" if img[1] else ""))
print("fonts:", [f[3] for f in page.get_fonts()])
