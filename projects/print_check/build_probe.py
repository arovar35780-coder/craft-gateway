"""What does DesignCraft write into a print PDF? Build a document with CMYK, spot, RGB, transparency and a shadow."""
import importlib.util, json, os, sys

# The gateway client: skill/craft-apps/scripts/craftmcp.py of this repository (override with CRAFTMCP).
CLIENT = os.environ.get("CRAFTMCP") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "skill", "craft-apps", "scripts", "craftmcp.py")
spec = importlib.util.spec_from_file_location("craftmcp", CLIENT)
cm = importlib.util.module_from_spec(spec); spec.loader.exec_module(cm)
cm.session("designcraft")
OUT = sys.argv[1]


def tool(name, **a):
    r = cm.rpc("designcraft", "tools/call", {"name": name, "arguments": a}, timeout=300)
    if r.get("isError"):
        raise SystemExit(name + " failed: " + "".join(c.get("text", "") for c in r["content"])[:300])
    return "".join(c.get("text", "") for c in r["content"] if c.get("type") == "text")


def x(cmd, **p):
    out = tool("execute", command=cmd, params=p)
    try:
        return json.loads(out)
    except ValueError:
        return out


tool("new_document", width=300, height=200, margins=0, title="cmyk-probe")
x("layout.documentSetup", intent="print", bleed=9)
x("swatch.create", name="C100", color={"c": 100, "m": 0, "y": 0, "k": 0})
x("swatch.create", name="Rich Black", color={"c": 60, "m": 40, "y": 40, "k": 100})
x("swatch.create", name="K100", color={"c": 0, "m": 0, "y": 0, "k": 100})
x("swatch.create", name="Spot Orange", color={"c": 0, "m": 60, "y": 100, "k": 0}, spot=True)
x("swatch.create", name="RGB Green", color="#22aa55")


def block(rect, swatch):
    r = x("frame.create", rect=rect, content="unassigned")
    x("object.fill", swatch=swatch, ids=[r["id"]])
    x("object.stroke", swatch="None", ids=[r["id"]])
    return r["id"]


block([0, 0, 100, 60], "C100")
block([100, 0, 200, 60], "Spot Orange")
block([200, 0, 300, 60], "RGB Green")
t = block([0, 70, 150, 130], "Rich Black")
half = block([150, 70, 300, 130], "C100")
x("object.opacity", opacity=0.5, ids=[half])                 # transparency
sh = block([20, 140, 120, 190], "K100")
x("object.dropShadow", on=True, distance=4, opacity=0.5, ids=[sh])   # effect that needs flattening
r = x("frame.create", rect=[150, 140, 290, 190], content="text", text="Black text K100")
x("text.select", story=r["story"], anchor=0, focus=15)
x("type.char", attrs={"size": 14, "fill": "K100"})
print(json.dumps(x("preflight.run"), indent=1)[:900])
if len(sys.argv) > 2:                                         # optional CMYK profile as the working space
    loaded = x("color.loadProfile", path=sys.argv[2])
    print("loadProfile:", json.dumps(loaded)[:300])
    name = loaded.get("name") if isinstance(loaded, dict) else None
    print("settings:", json.dumps(x("color.settings", cmyk=name))[:300])
print(json.dumps(x("file.exportPdf", path=OUT, standard="x4", bleed=True, marks={"crop": True, "bleed": True})))
