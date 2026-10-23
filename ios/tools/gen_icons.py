#!/usr/bin/env python3
"""Generates one tinted app icon per built-in theme from the primary icon.

  python ios/tools/gen_icons.py

For each theme in magpie/static/themes/*.json it writes
  - ios/Magpie/Assets.xcassets/icon-<id>.appiconset   (the Xcode build)
  - ios/xtool/icons/icon-<id>@2x.png, @3x.png, ~ipad@2x.png   (the xtool build, which has no asset catalog)
and prints the lines to keep in project.yml / Magpie-Info.plist. The icon: the theme's background in place of the white,
the wing hues rotated towards the theme's accent, and a soft accent glow behind the dark body on dark themes.
"""
import colorsys
import json
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = Path(__file__).resolve().parents[2]
THEMES = sorted((ROOT / "magpie" / "static" / "themes").glob("*.json"))
SOURCE = ROOT / "ios" / "Magpie" / "Assets.xcassets" / "AppIcon.appiconset" / "icon-1024.png"
CATALOG = ROOT / "ios" / "Magpie" / "Assets.xcassets"
LOOSE = ROOT / "ios" / "xtool" / "icons"
WING_HUE = 225 / 360          # the blue-violet the wing is built around


def rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def hue_of(color: tuple[int, int, int]) -> float:
    return colorsys.rgb_to_hsv(*(c / 255 for c in color))[0]


def background_mask(img: Image.Image) -> Image.Image:
    """White (255) where the plain background is, found by flood fill from the corners so the bird's white belly stays."""
    work = img.convert("RGB")
    marker = (255, 0, 255)
    for corner in ((0, 0), (img.width - 1, 0), (0, img.height - 1), (img.width - 1, img.height - 1)):
        ImageDraw.floodfill(work, corner, marker, thresh=28)
    diff = ImageChops.difference(work, Image.new("RGB", img.size, marker)).convert("L")
    mask = diff.point(lambda v: 255 if v < 8 else 0)
    return mask.filter(ImageFilter.GaussianBlur(1.2))   # soft edge, no white fringe


def tinted(source: Image.Image, theme: dict) -> Image.Image:
    c = theme["colors"]
    bg, surface, accent = rgb(c["bg"]), rgb(c["surface"]), rgb(c["accent"])
    dark = theme["base"] == "dark"
    size = source.size

    mask = background_mask(source)

    # Rotate saturated pixels (the wing) towards the accent; leave the black/white body alone.
    hsv = source.convert("RGB").convert("HSV")
    h, s, v = hsv.split()
    shift = round(((hue_of(accent) - WING_HUE) % 1.0) * 256)
    rotated = Image.merge("HSV", (h.point(lambda x: (x + shift) % 256), s, v)).convert("RGB")
    saturated = s.point(lambda x: 255 if x > 70 else 0).filter(ImageFilter.GaussianBlur(2))
    bird = Image.composite(rotated, source.convert("RGB"), saturated)

    # Background: a soft diagonal gradient from bg to surface.
    backdrop = Image.new("RGB", size, bg)
    vertical = Image.linear_gradient("L").resize(size)
    gradient = ImageChops.add(vertical, vertical.transpose(Image.TRANSPOSE), scale=2)   # top-left to bottom-right
    backdrop = Image.composite(Image.new("RGB", size, surface), backdrop, gradient.point(lambda x: x // 2))

    bird_alpha = ImageChops.invert(mask)
    if dark:
        # The body is near-black: a glow in the accent colour keeps it readable on a dark background.
        glow_alpha = bird_alpha.filter(ImageFilter.MaxFilter(21)).filter(ImageFilter.GaussianBlur(26)).point(lambda x: int(x * 0.55))
        backdrop = Image.composite(Image.new("RGB", size, accent), backdrop, glow_alpha)
    return Image.composite(bird, backdrop, bird_alpha).convert("RGB")


def update_configs(ids: list[str]) -> None:
    """Keeps the build configs in step with the generated icons (safe to run again)."""
    import plistlib
    import re

    names = [f"icon-{i}" for i in ids]

    # xtool: alternate icons are loose files in the bundle, declared in Info.plist.
    plist_path = ROOT / "ios" / "xtool" / "Magpie-Info.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    alternates = {n: {"CFBundleIconFiles": [n]} for n in names}
    icons = {"CFBundlePrimaryIcon": {"CFBundleIconFiles": ["icon-1024"]}, "CFBundleAlternateIcons": alternates}
    plist["CFBundleIcons"] = icons
    plist["CFBundleIcons~ipad"] = icons
    plist_path.write_bytes(plistlib.dumps(plist))

    block = "# icons:begin (written by tools/gen_icons.py)\nresources:\n" + "".join(
        f"  - icons/{n}{suffix}.png\n" for n in names for suffix in ("@2x", "@3x", "~ipad@2x")) + "# icons:end\n"
    for name in ("xtool.yml", "xtool.share.yml"):
        path = ROOT / "ios" / "xtool" / name
        text = path.read_text(encoding="utf-8")
        text = re.sub(r"# icons:begin.*?# icons:end\n", "", text, flags=re.S)
        # `resources` belongs to the app, so it goes before `extensions:` (a list nested under it would be the extension's)
        marker = "extensions:"
        text = text.replace(marker, block + marker, 1) if marker in text else text.rstrip("\n") + "\n" + block
        path.write_text(text, encoding="utf-8")

    # Xcode: alternate icon sets from the asset catalog.
    project = ROOT / "ios" / "project.yml"
    text = project.read_text(encoding="utf-8")
    setting = f"        ASSETCATALOG_COMPILER_ALTERNATE_APPICON_NAMES: \"{' '.join(names)}\"\n"
    if "ASSETCATALOG_COMPILER_ALTERNATE_APPICON_NAMES" in text:
        text = re.sub(r"        ASSETCATALOG_COMPILER_ALTERNATE_APPICON_NAMES:.*\n", setting, text)
    else:
        text = text.replace("        ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon\n",
                            "        ASSETCATALOG_COMPILER_APPICON_NAME: AppIcon\n" + setting, 1)
    project.write_text(text, encoding="utf-8")


def main() -> int:
    source = Image.open(SOURCE).convert("RGB")
    LOOSE.mkdir(parents=True, exist_ok=True)
    ids = []
    for path in THEMES:
        theme = json.loads(path.read_text(encoding="utf-8"))
        tid = theme["id"]
        icon = tinted(source, theme)
        name = f"icon-{tid}"

        folder = CATALOG / f"{name}.appiconset"
        folder.mkdir(parents=True, exist_ok=True)
        icon.save(folder / f"{name}.png", optimize=True)
        (folder / "Contents.json").write_text(json.dumps({
            "images": [{"filename": f"{name}.png", "idiom": "universal", "platform": "ios", "size": "1024x1024"}],
            "info": {"author": "xcode", "version": 1},
        }, indent=2) + "\n", encoding="utf-8")

        for suffix, px in (("@2x", 120), ("@3x", 180), ("~ipad@2x", 152)):
            icon.resize((px, px), Image.LANCZOS).save(LOOSE / f"{name}{suffix}.png", optimize=True)
        ids.append(tid)
        print("wrote", name)

    update_configs(ids)
    print("updated Magpie-Info.plist, xtool.yml, xtool.share.yml, project.yml")
    return 0


if __name__ == "__main__":
    sys.exit(main())
