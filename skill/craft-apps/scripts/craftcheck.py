"""Pre-delivery checks (`craftmcp.py check <slot>`) and test-document cleanup (`craftmcp.py cleanup <slot>`).

Standard library only. The functions take the craftmcp module (`cm`) for its rpc/session/Fail helpers.

check designcraft   preflight.run (overset text, missing fonts, low-res images) PLUS what preflight does not
                    see: items off the page, and text characters the font has no glyph for (the app silently
                    draws those with a fallback face). Glyph coverage is read from the cmap tables of the font
                    files installed in the Windows font folders.
check photocraft    canvas facts of the rendered document: size, transparency, content bounds and centring.
cleanup <slot>      close documents in the running app. Dry run unless --yes. Documents with unsaved changes are
                    never closed unless their title matches --dirty-match.
"""
import json
import os
import re
import struct
import sys
import tempfile


# ----------------------------------------------------------------------------- helpers

def mcp(cm, slot, tool, **arguments):
    result = cm.rpc(slot, "tools/call", {"name": tool, "arguments": arguments}, cm.DEFAULT_TIMEOUT)
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    if result.get("isError"):
        raise cm.Fail("%s %s: %s" % (slot, tool, text[:300]))
    try:
        return json.loads(text)
    except ValueError:
        return text


def command(cm, slot, name, params=None):
    tool, arguments = cm.run_arguments(slot, name, params or {})
    return mcp(cm, slot, tool, **arguments)


class Report:
    def __init__(self):
        self.lines = []  # (severity, area, message)

    def add(self, severity, area, message):
        self.lines.append((severity, area, message))

    def count(self, severity):
        return sum(1 for s, _, _ in self.lines if s == severity)

    def print(self):
        order = {"error": 0, "warning": 1, "info": 2}
        for sev, area, msg in sorted(self.lines, key=lambda x: order[x[0]]):
            print("%-7s %-10s %s" % (sev.upper(), area, msg))
        print("\n%d error(s), %d warning(s), %d note(s)" % (self.count("error"), self.count("warning"), self.count("info")))


# ----------------------------------------------------------------------------- minimal SFNT reader

def _u16(b, o):
    return struct.unpack_from(">H", b, o)[0]


def _u32(b, o):
    return struct.unpack_from(">I", b, o)[0]


def _face_offsets(f):
    f.seek(0)
    if f.read(4) == b"ttcf":
        f.seek(8)
        n = struct.unpack(">I", f.read(4))[0]
        return list(struct.unpack(">%dI" % n, f.read(4 * n)))
    return [0]


def _tables(f, face_offset):
    f.seek(face_offset)
    head = f.read(12)
    n = _u16(head, 4)
    recs = f.read(16 * n)
    out = {}
    for i in range(n):
        tag = recs[16 * i:16 * i + 4].decode("latin1")
        out[tag] = (_u32(recs, 16 * i + 8), _u32(recs, 16 * i + 12))
    return out


def _names(f, tables):
    if "name" not in tables:
        return {}
    off, length = tables["name"]
    f.seek(off)
    data = f.read(min(length, 1 << 20))
    count, string_offset = _u16(data, 2), _u16(data, 4)
    found = {}
    for i in range(count):
        r = 6 + 12 * i
        platform, _enc, lang, name_id, ln, so = struct.unpack_from(">HHHHHH", data, r)
        raw = data[string_offset + so:string_offset + so + ln]
        if platform in (0, 3):
            text = raw.decode("utf-16-be", "replace")
            rank = 0 if (platform == 3 and lang == 0x409) else 1
        elif platform == 1:
            text = raw.decode("mac_roman", "replace")
            rank = 2
        else:
            continue
        if name_id not in found or rank < found[name_id][0]:
            found[name_id] = (rank, text)
    return {k: v[1] for k, v in found.items()}


def _font_dirs():
    dirs = []
    if os.environ.get("WINDIR"):
        dirs.append(os.path.join(os.environ["WINDIR"], "Fonts"))
    if os.environ.get("LOCALAPPDATA"):
        dirs.append(os.path.join(os.environ["LOCALAPPDATA"], "Microsoft", "Windows", "Fonts"))
    return dirs


def font_index():
    """[(family, style, path, face_index)] for the installed .ttf/.otf/.ttc/.otc files (cached by size and mtime)."""
    cache_path = os.path.join(tempfile.gettempdir(), "craft-fontindex.json")
    try:
        with open(cache_path, "r", encoding="utf-8") as fh:
            cache = json.load(fh)
    except (OSError, ValueError):
        cache = {}
    fresh, out = {}, []
    for d in _font_dirs():
        if not os.path.isdir(d):
            continue
        for name in os.listdir(d):
            if not name.lower().endswith((".ttf", ".otf", ".ttc", ".otc")):
                continue
            path = os.path.join(d, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            key = path.lower()
            sig = [st.st_size, int(st.st_mtime)]
            entry = cache.get(key)
            if entry and entry.get("sig") == sig:
                faces = entry["faces"]
            else:
                faces = []
                try:
                    with open(path, "rb") as f:
                        for idx, off in enumerate(_face_offsets(f)):
                            n = _names(f, _tables(f, off))
                            fam = n.get(16) or n.get(1)
                            sty = n.get(17) or n.get(2) or "Regular"
                            if fam:
                                faces.append([fam, sty, idx])
                except (OSError, struct.error):
                    faces = []
            fresh[key] = {"sig": sig, "faces": faces}
            out.extend((fam, sty, path, idx) for fam, sty, idx in faces)
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            json.dump(fresh, fh)
    except OSError:
        pass
    return out


def cmap_ranges(path, face_index=0):
    """Code point ranges [(lo, hi)] a font file has glyphs for (cmap formats 4 and 12)."""
    with open(path, "rb") as f:
        off = _face_offsets(f)[face_index]
        tables = _tables(f, off)
        if "cmap" not in tables:
            return []
        c_off, c_len = tables["cmap"]
        f.seek(c_off)
        data = f.read(c_len)
    best = None
    for i in range(_u16(data, 2)):
        platform, enc, sub = struct.unpack_from(">HHI", data, 4 + 8 * i)
        fmt = _u16(data, sub)
        rank = {(3, 10): 0, (0, 4): 0, (0, 6): 0, (3, 1): 1, (0, 3): 1}.get((platform, enc), 9)
        if fmt in (4, 12) and (best is None or rank < best[0]):
            best = (rank, sub, fmt)
    if best is None:
        return []
    _, sub, fmt = best
    ranges = []
    if fmt == 12:
        for g in range(_u32(data, sub + 12)):
            start, end, _ = struct.unpack_from(">III", data, sub + 16 + 12 * g)
            ranges.append((start, end))
    else:
        seg = _u16(data, sub + 6) // 2
        ends = struct.unpack_from(">%dH" % seg, data, sub + 14)
        starts = struct.unpack_from(">%dH" % seg, data, sub + 16 + 2 * seg)
        for s, e in zip(starts, ends):
            if s != 0xFFFF:
                ranges.append((s, e))
    return ranges


def _covers(ranges, cp):
    return any(lo <= cp <= hi for lo, hi in ranges)


# ----------------------------------------------------------------------------- check designcraft

def check_designcraft(cm, args):
    slot = "designcraft"
    report = Report()
    doc = mcp(cm, slot, "inspect_document")
    if not isinstance(doc, dict) or "spreads" not in doc:
        raise cm.Fail("designcraft: no document is open")
    report.add("info", "document", "%d page(s), %sx%s pt%s" % (
        doc.get("pageCount", 0), doc["settings"].get("pageWidth"), doc["settings"].get("pageHeight"),
        ", unsaved changes" if doc.get("dirty") else ""))

    # 1. the app's own preflight
    pre = command(cm, slot, "preflight.run")
    for issue in pre.get("issues", []):
        where = ""
        if issue.get("page") is not None:
            where = " (page %s)" % (issue["page"] + 1)
        report.add("error" if issue.get("severity") == "error" else "warning", "preflight",
                   "%s%s%s" % (issue.get("message", issue.get("kind")), where, " [item %s]" % issue["item"] if "item" in issue else ""))
    if not pre.get("issues"):
        report.add("info", "preflight", "no issues")

    # 2. items off the page
    for spread in doc["spreads"]:
        pages = [p["bounds"] for p in spread.get("pages", [])]
        if not pages:
            continue
        x0, y0 = min(p[0] for p in pages), min(p[1] for p in pages)
        x1, y1 = max(p[2] for p in pages), max(p[3] for p in pages)
        for item in spread.get("items", []):
            if item.get("hidden"):
                continue
            b = item["bounds"]
            label = "%s %s" % (item.get("kind", "item"), item.get("id"))
            if b[2] <= x0 or b[0] >= x1 or b[3] <= y0 or b[1] >= y1:
                report.add("error", "geometry", "%s lies entirely off the page" % label)
                continue
            over = {"left": x0 - b[0], "top": y0 - b[1], "right": b[2] - x1, "bottom": b[3] - y1}
            over = {k: round(v, 1) for k, v in over.items() if v > args.bleed + 0.05}
            if over:
                covers = b[0] <= x0 and b[1] <= y0 and b[2] >= x1 and b[3] >= y1
                text = ", ".join("%s by %s pt" % kv for kv in over.items())
                report.add("info" if covers else "warning", "geometry",
                           "%s extends past the page: %s%s" % (label, text, " (full-bleed background)" if covers else ""))

    # 3. glyph coverage of non-ASCII text
    stories = doc.get("stories", [])
    saved = doc.get("selection", {}) or {}
    index = None
    problems = unverified = samples = 0
    for story in stories:
        info = mcp(cm, slot, "execute", command="story.get", params={"story": story["id"]})
        text = info.get("text", "") if isinstance(info, dict) else ""
        if all(ord(c) < 128 for c in text):
            continue
        if index is None:
            index = font_index()
        for m in re.finditer(r"[^\x00-\x7f]+", text):
            if samples >= args.max_samples:
                break
            samples += 1
            word = m.group(0)
            start = len(text[:m.start()].encode("utf-8"))
            end = start + len(word[0].encode("utf-8"))
            command(cm, slot, "text.select", {"story": story["id"], "anchor": start, "focus": end})
            attrs = command(cm, slot, "type.selectionAttrs").get("chars", {})
            fam, sty = attrs.get("fontFamily"), attrs.get("fontStyle")
            faces = [x for x in index if x[0].lower() == str(fam).lower()]
            face = next((x for x in faces if x[1].lower() == str(sty).lower()), None) or (faces[0] if faces else None)
            if face is None:
                unverified += 1
                report.add("info", "glyphs", "story %s: font %s %s is not an installed file (bundled or missing), %r not verified" % (story["id"], fam, sty, word))
                continue
            ranges = cmap_ranges(face[2], face[3])
            missing = sorted({c for c in word if not _covers(ranges, ord(c))})
            if missing:
                problems += 1
                report.add("error", "glyphs", "story %s: %s %s has no glyph for %s in %r; the app draws them with a fallback font" % (
                    story["id"], fam, sty, " ".join("%s (U+%04X)" % (c, ord(c)) for c in missing), word))
    if samples >= args.max_samples:
        report.add("warning", "glyphs", "stopped after %d non-ASCII samples (--max-samples)" % args.max_samples)
    if samples and not problems and not unverified:
        report.add("info", "glyphs", "%d non-ASCII run(s) checked, all have glyphs" % samples)
    elif not samples:
        report.add("info", "glyphs", "no non-ASCII text, glyph check skipped")

    # restore the user's text selection (the probe moved it)
    t = saved.get("text")
    try:
        if t:
            command(cm, slot, "text.select", {"story": t["story"], "anchor": t["anchor"], "focus": t["focus"]})
    except cm.Fail:
        pass
    return report


# ----------------------------------------------------------------------------- check photocraft

def check_photocraft(cm, args):
    import base64
    import io
    try:
        from PIL import Image
    except ImportError:
        raise cm.Fail("check photocraft needs Pillow (pip install pillow)")
    report = Report()
    reply = mcp(cm, "photocraft", "control_call", method="doc.render", params={"maxSide": 0})
    if not isinstance(reply, dict) or "base64" not in reply:
        raise cm.Fail("photocraft: doc.render returned no image (%s)" % str(reply)[:150])
    im = Image.open(io.BytesIO(base64.b64decode(reply["base64"]))).convert("RGBA")
    w, h = im.size
    report.add("info", "canvas", "%dx%d px" % (w, h))
    alpha = im.getchannel("A")
    lo, hi = alpha.getextrema()
    box = alpha.point(lambda v: 255 if v > 8 else 0).getbbox()
    if lo == 255:
        report.add("info", "canvas", "fully opaque (no transparent pixels)")
        box = (0, 0, w, h)
    elif box is None:
        report.add("error", "canvas", "the document is fully transparent")
        return report
    else:
        report.add("info", "canvas", "has transparency; content bounds %s" % (box,))
        left, top, right, bottom = box[0], box[1], w - box[2], h - box[3]
        report.add("info", "centering", "margins left %d / right %d, top %d / bottom %d px; content centre is %+.1f px horizontally, %+.1f px vertically off the canvas centre" % (
            left, right, top, bottom, (box[0] + box[2]) / 2 - w / 2, (box[1] + box[3]) / 2 - h / 2))
        if min(left, right, top, bottom) <= 0:
            report.add("warning", "canvas", "content touches the canvas edge (clipped glow or shadow?)")
    return report


# ----------------------------------------------------------------------------- cleanup

CLEANUP = {
    # slot: (inspect tool, key path to the documents list, title key, index key or None, close command, close params)
    "designcraft": ("ui_inspect", ("documents",), "title", None, "file.close", lambda i: {"index": i}),
    "photocraft": ("ui_inspect", ("session", "documents"), "name", "index", "file.close", lambda i: {"document": i}),
    "vectorcraft": ("inspect_ui", ("documents",), "title", None, "file.close", lambda i: {"index": i}),
}


def _documents(cm, slot):
    tool, path, title_key, index_key, _, _ = CLEANUP[slot]
    data = mcp(cm, slot, tool)
    for key in path:
        data = data.get(key) if isinstance(data, dict) else None
    if not isinstance(data, list):
        raise cm.Fail("%s: could not read the document list" % slot)
    return [{"index": d.get(index_key, pos) if index_key else pos, "title": str(d.get(title_key, "?")), "dirty": bool(d.get("dirty"))}
            for pos, d in enumerate(data)]


def cleanup(cm, args):
    slot = args.slot
    if slot not in CLEANUP:
        raise cm.Fail("cleanup is available for: %s" % ", ".join(sorted(CLEANUP)))
    docs = _documents(cm, slot)
    pattern = re.compile(args.dirty_match) if args.dirty_match else None
    plan = []
    for d in docs:
        if not d["dirty"]:
            d["action"] = "close" if args.clean else "keep (clean)"
        elif pattern and pattern.search(d["title"]):
            d["action"] = "close (unsaved changes discarded: matches --dirty-match)"
        else:
            d["action"] = "keep (UNSAVED, not matched)"
        if d["action"].startswith("close"):
            plan.append(d)
    print("%s: %d open document(s)" % (slot, len(docs)))
    for d in docs:
        print("  [%d] %-40s %-9s -> %s" % (d["index"], d["title"][:40], "unsaved" if d["dirty"] else "clean", d["action"]))
    if not plan:
        print("nothing to close (use --clean for documents without unsaved changes, --dirty-match REGEX for throwaway unsaved ones)")
        return 0
    if not args.yes:
        print("\ndry run: %d document(s) would be closed; repeat with --yes to do it" % len(plan))
        return 0
    _, _, _, _, close_command, close_params = CLEANUP[slot]
    for d in sorted(plan, key=lambda x: -x["index"]):  # highest index first: closing shifts the later ones down
        command(cm, slot, close_command, close_params(d["index"]))
    after = _documents(cm, slot)
    print("\nclosed %d; %d document(s) remain: %s" % (len(plan), len(after), ", ".join("%s%s" % (d["title"], "*" if d["dirty"] else "") for d in after) or "-"))
    return 0 if len(after) == len(docs) - len(plan) else 1


# ----------------------------------------------------------------------------- entry points

CHECKS = {"designcraft": check_designcraft, "photocraft": check_photocraft}


def run_check(cm, args):
    check_slot = CHECKS.get(args.slot)
    if check_slot is None:
        raise cm.Fail("no checks for %s yet (available: %s)" % (args.slot, ", ".join(sorted(CHECKS))))
    cm.check_slot(args.slot)
    cm.session(args.slot, cm.DEFAULT_TIMEOUT)
    report = check_slot(cm, args)
    report.print()
    return 1 if report.count("error") else 0


def run_cleanup(cm, args):
    cm.check_slot(args.slot)
    cm.session(args.slot, cm.DEFAULT_TIMEOUT)
    return cleanup(cm, args)
