# Example: Slow Coffee Fair flyer

An A5 flyer built entirely through the gateway: the image is drawn in PhotoCraft, the layout is set in DesignCraft.

1. `python scripts/asset_sun.py` draws the coffee-cup-in-the-sun image in PhotoCraft. Export it as
   `assets/coffee-sun.png` (transparent background).
2. `python scripts/flyer_v2.py` lays out the flyer in DesignCraft, places the image and saves
   `slow-coffee-fair-v2.designcraft`, `.png` and `.pdf` in this folder. `scripts/flyer_v1.py` is the first,
   type-only version.

Needs the gateway running; the scripts use the repository's client `skill/craft-apps/scripts/craftmcp.py` (override
with `CRAFTMCP`). The fonts are a display serif
(New Kansas) and a monospace (Ballinger Mono); install them or change `SERIF` and `MONO` at the top of the scripts
to fonts you have.
