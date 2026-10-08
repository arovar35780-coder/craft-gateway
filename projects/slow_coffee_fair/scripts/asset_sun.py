"""Complex raster asset in PhotoCraft: 'coffee sun' sticker, 1000x1000, transparent background."""
import importlib.util, json, os, sys

# The gateway client (craftmcp.py of the craft-apps skill); point CRAFTMCP at it if it lives elsewhere.
CLIENT = os.environ.get("CRAFTMCP") or os.path.expandvars(r"%USERPROFILE%\.claude\skills\craft-apps\scripts\craftmcp.py")
spec = importlib.util.spec_from_file_location("craftmcp", CLIENT)
cm = importlib.util.module_from_spec(spec); spec.loader.exec_module(cm)
cm.session("photocraft")
CREAM = "#F3E9D8"
DX = 15  # the cream group (saucer, cup, handle, steam) spanned x 275..694: centre 484.5, 15 left of the rings centre 500


def call(tool, **a):
    r = cm.rpc("photocraft", "tools/call", {"name": tool, "arguments": a}, timeout=300)
    if r.get("isError"):
        raise SystemExit(tool + " failed: " + "".join(c.get("text", "") for c in r["content"])[:400])
    return "".join(c.get("text", "") for c in r["content"] if c.get("type") == "text")


def run(cmd, **p):
    out = call("command_run", id=cmd, params=p)
    print("%-34s ok" % cmd, flush=True)
    try:
        return json.loads(out)
    except ValueError:
        return out


call("doc_new", width=1000, height=1000, name="coffee-sun", background="transparent") if False else None
run("file.new", width=1000, height=1000, background="transparent", name="coffee-sun")

# 1. disc with a radial gradient and a warm outer glow
run("shape.create", kind="ellipse", rect=[20, 20, 960, 960], name="Disc",
    fill={"gradient": {"stops": [[0, "#EE8556"], [0.55, "#C9573B"], [1, "#8F3322"]], "style": "radial", "scale": 100}})
run("layer.layerStyle.innerShadow", color="#5E1F12", opacity=55, size=90, distance=0, choke=5)

# 2. soft highlight clipped to the disc
run("shape.create", kind="ellipse", rect=[190, 110, 420, 230], name="Highlight", fill=[255, 225, 190, 95])
run("layer.rasterize.shape")
run("filter.blur.gaussianBlur", radius=70)
run("layer.createClippingMask")

# 3. film grain: mid-grey noise layer in Overlay, clipped to the disc
run("layer.new.layer", name="Grain")
run("select.all")
run("edit.fill", color="#808080")
run("filter.noise.addNoise", amount=38, distribution="gaussian", monochromatic=True, seed=7)
run("layer.layerStyle.blendingOptions", blend="overlay", opacity=55)
run("layer.createClippingMask")
run("select.none") if False else None

# 4. concentric ripples
for i, (inset, dash) in enumerate(((120, None), (215, [2, 3]), (310, None))):
    size = 940 - 2 * (inset - 30)
    stroke = {"width": 7, "color": CREAM, "opacity": 38, "align": "center", "cap": "round"}
    if dash:
        stroke["dashes"] = dash
    run("shape.create", kind="ellipse", rect=[inset, inset, size, size], name="Ripple %d" % (i + 1), fill=None, stroke=stroke)

# 5. cup, handle, saucer
run("shape.create", kind="roundedRect", rect=[345 + DX, 520, 270, 235], radii=[24, 24, 130, 130], name="Cup", fill=CREAM)
run("shape.create", kind="ellipse", rect=[560 + DX, 565, 120, 130], name="Handle", fill=None,
    stroke={"width": 30, "color": CREAM, "opacity": 100, "align": "center"})
run("shape.create", kind="roundedRect", rect=[275 + DX, 775, 410, 30], radii=15, name="Saucer", fill=CREAM)

# 6. three steam wisps
for k, x0 in enumerate((410 + DX, 480 + DX, 550 + DX)):
    h = 105 + 25 * (k % 2)
    top = 500 - h - 70
    knots = [{"anchor": [x0, 490], "smooth": True}, {"anchor": [x0 + 22, 490 - h * 0.33], "smooth": True},
             {"anchor": [x0 - 22, 490 - h * 0.66], "smooth": True}, {"anchor": [x0 + 10, 490 - h], "smooth": True}]
    run("shape.create", kind="path", name="Steam %d" % (k + 1), fill=None,
        path={"subpaths": [{"closed": False, "knots": knots}]},
        stroke={"width": 17, "color": CREAM, "opacity": 90, "align": "center", "cap": "round", "join": "round"})

print("asset built")
