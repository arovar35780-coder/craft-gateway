"""Quality checks run after a build (see build.py).

Each finding is (severity, kind, message) with severity "error" or "warning"; errors fail the build.
  overset       a story still has text that does not fit                         error
  preflight     DesignCraft's preflight (missing fonts/links, empty frames, ...)  as reported
  toc           contents entries differ from the pages the sections really start on  error
  blank         a page without any text that is not the intended blank page        error
  shortLast     the last page of a section holds only a few lines                  warning
  sparse        a body page is mostly empty                                        warning
Page fill is measured on the rendered page image: the lowest inked row inside the text area.
"""
import base64
import io

try:
    from PIL import Image
except ImportError:  # the visual checks are skipped without Pillow
    Image = None

INK_LUMINANCE = 200          # darker than this counts as ink
SHORT_LAST_PAGE = 0.15       # of the text area height
SPARSE_PAGE = 0.5
RENDER_SCALE = 0.5


def page_fill(d, idx, top, text_bottom, page_h):
    """(has_ink, fill) of page `idx`: fill is the share of the text area (from `top`) that is used."""
    r = d.call("ui.render", page=idx, scale=RENDER_SCALE)
    img = Image.open(io.BytesIO(base64.b64decode(r["pngBase64"]))).convert("L")
    w, h = img.size
    k = h / page_h
    y0, y1 = int(max(top - 3, 50) * k), int(min(text_bottom + 4, page_h) * k)
    px = img.load()
    last = None
    for y in range(y0, y1):
        if any(px[x, y] < INK_LUMINANCE for x in range(w)):
            last = y
    if last is None:
        return False, 0.0
    area = text_bottom - top
    return True, min(1.0, max(0.0, (last / k - top) / area))


def run(d, m, sections_report, body_first, blank_idx, page_h):
    """Run every check; `sections_report` is [{file, kind, first, last}] in build order."""
    findings = []
    ins = d.inspect()
    names = [p["name"] for sp in ins["spreads"] for p in sp["pages"]]
    where = lambda i: f"page {names[i]}" if 0 <= i < len(names) else f"page index {i}"

    for s in ins["stories"]:
        if s["overset"]:
            findings.append(("error", "overset", f"story {s['id']} has overset text ({s.get('preview', '')[:40]!r})"))

    for i in d.x("preflight.run")["issues"]:
        findings.append((i["severity"], "preflight:" + i["kind"], i["message"] + (f" ({where(i['page'])})" if i.get("page") is not None else "")))

    st = m["styles"]
    entries = d.x("toc.entries", entries=[{"style": st["chapter_heading"], "level": 1}, {"style": st["matter_heading"], "level": 1}])
    starts = [s for s in sections_report if s["kind"] in ("front", "chapter", "back")]
    if m["toc"]["enabled"]:
        if len(entries) != len(starts):
            findings.append(("error", "toc", f"contents has {len(entries)} entries for {len(starts)} sections"))
        for e, s in zip(entries, starts):
            if e["page"] != names[s["first"]]:
                findings.append(("error", "toc", f"{s['file']}: contents says page {e['page']}, the section starts on {names[s['first']]}"))

    if Image is None:
        findings.append(("warning", "pageFill", "Pillow is not installed: blank/short page checks were skipped"))
        return findings, names

    lay = m["layout"]
    text_bottom = page_h - lay["bottom"]
    section_last = {s["last"]: s for s in sections_report}
    section_first = {s["first"] for s in sections_report}
    for idx in range(len(names)):
        top = lay["opener_top"] if idx in section_first else lay["body_top"]
        has_ink, fill = page_fill(d, idx, top, text_bottom, page_h)
        if not has_ink:
            if idx != blank_idx:
                findings.append(("error", "blank", f"{where(idx)} has no text"))
            continue
        if idx < body_first:
            continue  # front matter pages are short by design
        if idx in section_last and idx not in section_first and fill < SHORT_LAST_PAGE:
            findings.append(("warning", "shortLast", f"{where(idx)}: last page of {section_last[idx]['file']} is {fill:.0%} full"))
        elif idx not in section_last and idx not in section_first and fill < SPARSE_PAGE:
            findings.append(("warning", "sparse", f"{where(idx)} is {fill:.0%} full"))
    return findings, names


def format_report(findings, title):
    errors = [f for f in findings if f[0] == "error"]
    lines = [f"Checks for {title}: {len(errors)} error(s), {len(findings) - len(errors)} warning(s)"]
    for sev, kind, msg in sorted(findings, key=lambda f: (f[0] != "error", f[1])):
        lines.append(f"  [{sev}] {kind}: {msg}")
    if not findings:
        lines.append("  all clear")
    return "\n".join(lines), len(errors)
