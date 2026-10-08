"""Slow Coffee Fair flyer in DesignCraft through the gateway. Font pair 13: New Kansas (display/serif) + Ballinger Mono."""
import importlib.util, json, os

from pathlib import Path

# The gateway client (craftmcp.py of the craft-apps skill); point CRAFTMCP at it if it lives elsewhere.
CLIENT = os.environ.get("CRAFTMCP") or os.path.expandvars(r"%USERPROFILE%\.claude\skills\craft-apps\scripts\craftmcp.py")
PROJECT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("craftmcp", CLIENT)
cm = importlib.util.module_from_spec(spec); spec.loader.exec_module(cm)
cm.session("designcraft")

SERIF, MONO = "New Kansas", "Ballinger Mono"
CREAM, GREEN, TERRA = "#F3E9D8", "#1E3B33", "#C9573B"
W, H = 420, 595


def tool(name, **a):
    r = cm.rpc("designcraft", "tools/call", {"name": name, "arguments": a}, timeout=300)
    if r.get("isError"):
        raise SystemExit(name + " failed: " + "".join(c.get("text", "") for c in r["content"])[:400])
    return "".join(c.get("text", "") for c in r["content"] if c.get("type") == "text")


def x(cmd, **p):
    out = tool("execute", command=cmd, params=p)
    try:
        return json.loads(out)
    except ValueError:
        return out


tool("new_document", width=W, height=H, margins=0, title="Slow Coffee Fair")
for name, col in (("Cream", CREAM), ("Green", GREEN), ("Terracotta", TERRA)):
    x("swatch.create", name=name, color=col)


def block(rect, swatch, shape="rectangle"):
    r = x("frame.create", rect=rect, shape=shape, content="unassigned")
    x("object.fill", swatch=swatch, ids=[r["id"]])
    x("object.stroke", swatch="None", ids=[r["id"]])
    return r["id"]


def text(rect, s, family, style, size, color, leading=None, tracking=0, align="left"):
    r = x("frame.create", rect=rect, content="text", text=s)
    story = r["story"]
    x("text.select", story=story, anchor=0, focus=len(s.encode("utf-8")))
    attrs = {"fontFamily": family, "fontStyle": style, "size": size, "fill": color}
    if leading:
        attrs["leading"] = {"kind": "points", "value": leading}
    if tracking:
        attrs["tracking"] = tracking
    x("type.char", attrs=attrs)
    x("type.para", attrs={"align": align, "spaceBefore": 0, "spaceAfter": 0})
    x("text.select", story=story, anchor=0, focus=0)
    return r["id"]


# background, sun, bottom band
block([0, 0, W, H], "Cream")
x("edit.deselectAll")  # place_image fills a selected graphic frame: the background block was still selected
placed = json.loads(tool("place_image", path=str(PROJECT / "assets" / "coffee-sun.png"), x=240, y=40, width=170))
print("place_image ->", placed)
pid = placed.get("frame") or placed.get("id")
x("selection.set", ids=[pid])
x("transform.set", x=240, y=40, width=170, height=170, ref=0)
x("object.fit", mode="fitContentToFrame", ids=[pid])
x("object.stroke", swatch="None", ids=[pid])
block([0, 508, W, H], "Green")

# headline over the sun
text([34, 30, 390, 52], "VOL. 07  ·  SATURDAY 14 JUNE", MONO, "Medium", 9, "Green", tracking=120)
text([30, 76, 400, 330], "Slow\nCoffee\nFair", SERIF, "Bold", 78, "Green", leading=76, tracking=-20)

# rule and tagline
block([34, 340, 386, 341], "Green")
text([34, 354, 380, 410], "Twelve roasters, one old barn\nand no reason to hurry.", SERIF, "Regular Italic", 18, "Green", leading=24)

# details
text([34, 428, 386, 490],
     "SAT 14 JUNE  /  10:00 – 18:00\nTHE OLD BARN, RIVERSIDE LANE\nFREE ENTRY  /  BRING A MUG",
     MONO, "Regular", 10, "Green", leading=17, tracking=40)

# footer
text([34, 530, 386, 552], "Live music from 15:00", SERIF, "Medium Italic", 15, "Cream")
text([34, 560, 386, 578], "SLOWCOFFEEFAIR.EXAMPLE", MONO, "Medium", 9, "Cream", tracking=120)
base = str(PROJECT / "slow-coffee-fair-v2")
print(x("file.saveAs", path=base + ".designcraft"))
print(tool("export_png", path=base + ".png", page=0))
print(x("file.exportPdf", path=base + ".pdf"))
print("flyer built")
