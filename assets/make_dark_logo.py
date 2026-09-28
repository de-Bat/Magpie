"""Build the dark-theme logo assets from assets/magpie-logo.png.

The magpie's near-black head, back and tail vanish on dark backgrounds, so the dark
variant lifts those tones to a readable navy (hue kept) and adds a faint light rim.

    pip install pillow numpy && python assets/make_dark_logo.py

Writes assets/magpie-logo-dark.png, the web app's *-dark icons, and the iOS dark app icon.
"""
import numpy as np
from PIL import Image, ImageFilter

def darkify(src: Image.Image, lift_to=0.50, knee=0.42, rim=0.012, rim_alpha=0.55, rim_rgb=(233, 228, 218)) -> Image.Image:
    im = src.convert("RGBA")
    a = np.asarray(im).astype(np.float32) / 255
    rgb, alpha = a[..., :3], a[..., 3:]
    v = rgb.max(axis=2, keepdims=True)
    # weight 1 for black, 0 at the knee and above: only the dark plumage moves
    w = np.clip(1 - v / knee, 0, 1) ** 1.2
    target_v = v + (lift_to - v) * w
    scale = np.where(v > 1e-4, target_v / np.maximum(v, 1e-4), 1)
    lifted = rgb * scale
    # pure-black pixels have no hue: give them the logo's navy
    navy = np.array([0.16, 0.25, 0.48], dtype=np.float32)
    lifted = np.where(v < 0.03, navy * (target_v / navy.max()), lifted)
    out = np.concatenate([np.clip(lifted, 0, 1), alpha], axis=2)
    body = Image.fromarray((out * 255).astype(np.uint8), "RGBA")
    if rim > 0:
        size = im.size[0]
        r = max(1, round(size * rim))
        mask = im.getchannel("A").filter(ImageFilter.MaxFilter(2 * r + 1)).filter(ImageFilter.GaussianBlur(r * 0.6))
        halo = Image.new("RGBA", im.size, rim_rgb + (0,))
        halo.putalpha(mask.point(lambda p: int(p * rim_alpha)))
        body = Image.alpha_composite(halo, body)
    return body



if __name__ == "__main__":
    from pathlib import Path
    R = str(Path(__file__).resolve().parent.parent) + "/"
    src = Image.open(R + "assets/magpie-logo.png")
    dark = darkify(src, lift_to=0.56, rim=0.006, rim_alpha=0.35)
dark.save(R + "assets/magpie-logo-dark.png", optimize=True)
for name, size in [("logo-96-dark.png", 96), ("favicon-dark.png", 64), ("favicon-32-dark.png", 32)]:
    dark.resize((size, size), Image.LANCZOS).save(R + "magpie/static/icons/" + name, optimize=True)

# iOS 18 dark app icon: same bird placement as the light icon, on a transparent background
# (iOS draws the dark backdrop). Place the bird where it sits in the light icon.
icon = np.asarray(Image.open(R + "assets/app-icon-1024.png").convert("RGB")).astype(int)
ys, xs = np.where((255 * 3 - icon.sum(axis=2)) > 30)
box = (xs.min(), ys.min(), xs.max() + 1, ys.max() + 1)
lb = src.getchannel("A").getbbox()
bird = dark.crop(lb).resize((box[2] - box[0], box[3] - box[1]), Image.LANCZOS)
out = Image.new("RGBA", (1024, 1024), (0, 0, 0, 0))
out.alpha_composite(bird, (box[0], box[1]))
out.save(R + "ios/Magpie/Assets.xcassets/AppIcon.appiconset/icon-1024-dark.png", optimize=True)
print("done")
