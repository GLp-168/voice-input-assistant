from __future__ import annotations

from pathlib import Path
import xml.etree.ElementTree as ET

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]


def test_brand_icon_assets_are_transparent_and_multi_size() -> None:
    svg = ET.parse(ROOT / "assets" / "icons" / "app.svg").getroot()
    png = Image.open(ROOT / "assets" / "icons" / "app.png").convert("RGBA")
    ico = Image.open(ROOT / "assets" / "icons" / "app.ico")

    assert len(list(svg.iter("{http://www.w3.org/2000/svg}path"))) == 360
    assert png.size == (512, 512)
    assert png.getpixel((0, 0))[3] == 0
    assert png.getpixel((256, 256))[3] == 255
    alpha_bounds = png.getchannel("A").getbbox()
    assert alpha_bounds is not None
    assert 338 <= alpha_bounds[2] - alpha_bounds[0] <= 344
    assert {
        (16, 16),
        (20, 20),
        (24, 24),
        (32, 32),
        (40, 40),
        (48, 48),
        (64, 64),
        (96, 96),
        (128, 128),
        (256, 256),
    }.issubset(ico.info.get("sizes", set()))
