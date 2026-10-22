import io

from PIL import Image, ImageDraw

from magpie.images import cut_out_background


def _png(img):
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def test_white_background_becomes_transparent_but_inner_white_stays():
    im = Image.new("RGB", (100, 100), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((10, 10, 90, 90), fill=(200, 30, 30))
    d.ellipse((40, 40, 60, 60), fill="white")
    out = Image.open(io.BytesIO(cut_out_background(_png(im)))).convert("RGBA")
    assert out.getpixel((0, 0))[3] == 0
    assert out.getpixel((50, 20))[3] == 255
    assert out.getpixel((50, 50))[3] == 255   # white inside the mark is kept


def test_already_transparent_or_busy_picture_is_left_alone():
    assert cut_out_background(_png(Image.new("RGBA", (50, 50), (0, 0, 0, 0)))) is None
    noisy = Image.effect_noise((64, 64), 80).convert("RGB")
    assert cut_out_background(_png(noisy)) is None
