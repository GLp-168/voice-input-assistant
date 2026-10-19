"""Generate the shared Voice Input brand mark as PNG and multi-size ICO assets."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[1]
ICON_DIR = ROOT / "assets" / "icons"
SVG_PATH = ICON_DIR / "app.svg"
PNG_PATH = ICON_DIR / "app.png"
ICO_PATH = ICON_DIR / "app.ico"

ACCENT = (15, 127, 114)
WARM = (244, 210, 138)
SKY = (127, 216, 255)
CSS_START_ANGLE = 150.0
MASTER_SIZE = 1024
PNG_SIZE = 512
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)
ICO_CORE_DIAMETERS = {
    16: 14,
    20: 17,
    24: 19,
    32: 24,
    40: 27,
    48: 32,
    64: 42,
    96: 63,
    128: 84,
    256: 168,
}


def _mix(left: tuple[int, int, int], right: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(a + (b - a) * amount) for a, b in zip(left, right, strict=True))


def _conic_color(css_angle: float) -> tuple[int, int, int]:
    local = (css_angle - CSS_START_ANGLE) % 360.0
    if local < 120.0:
        return _mix(ACCENT, WARM, local / 120.0)
    if local < 240.0:
        return _mix(WARM, SKY, (local - 120.0) / 120.0)
    return _mix(SKY, ACCENT, (local - 240.0) / 120.0)


def _brand_mark(size: int, *, core_diameter: float, halo_extent: float) -> Image.Image:
    center = (size - 1) / 2.0
    radius = core_diameter / 2.0
    pixels = bytearray(size * size * 4)
    offset = 0
    for y in range(size):
        dy = y - center
        for x in range(size):
            dx = x - center
            distance = math.hypot(dx, dy)
            if distance <= radius + 1.0:
                css_angle = math.degrees(math.atan2(dx, -dy)) % 360.0
                red, green, blue = _conic_color(css_angle)

                alpha = 255
                if distance > radius:
                    alpha = round(255 * max(0.0, radius + 1.0 - distance))
                pixels[offset : offset + 4] = bytes((red, green, blue, alpha))
            elif halo_extent > 0 and distance <= radius + halo_extent:
                progress = (distance - radius) / halo_extent
                alpha = round(18 * (1.0 - progress) ** 2)
                pixels[offset : offset + 4] = bytes((*ACCENT, alpha))
            offset += 4

    return Image.frombytes("RGBA", (size, size), bytes(pixels))


def _resize_premultiplied(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    return image.convert("RGBa").resize(size, Image.Resampling.LANCZOS).convert("RGBA")


def _render_icon(size: int, core_diameter: float, halo_extent: float) -> Image.Image:
    scale = 8 if size <= 64 else 4
    rendered = _brand_mark(
        size * scale,
        core_diameter=core_diameter * scale,
        halo_extent=halo_extent * scale,
    )
    return _resize_premultiplied(rendered, (size, size))


def _write_svg() -> None:
    size = 512
    center = size / 2
    radius = 168
    wedges: list[str] = []
    for angle in range(360):
        start = math.radians(angle)
        end = math.radians(angle + 1.02)
        start_x = center + radius * math.sin(start)
        start_y = center - radius * math.cos(start)
        end_x = center + radius * math.sin(end)
        end_y = center - radius * math.cos(end)
        red, green, blue = _conic_color(angle + 0.5)
        color = f"#{red:02x}{green:02x}{blue:02x}"
        wedges.append(
            f'<path d="M {center:g} {center:g} L {start_x:.3f} {start_y:.3f} '
            f'A {radius:g} {radius:g} 0 0 1 {end_x:.3f} {end_y:.3f} Z" '
            f'fill="{color}" stroke="{color}" stroke-width="1"/>'
        )
    SVG_PATH.write_text(
        "\n".join(
            (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" role="img" aria-label="Voice Input brand mark">',
                '  <g shape-rendering="geometricPrecision">',
                *(f"    {wedge}" for wedge in wedges),
                "  </g>",
                "</svg>",
                "",
            )
        ),
        encoding="utf-8",
    )


def generate() -> tuple[Path, Path, Path]:
    ICON_DIR.mkdir(parents=True, exist_ok=True)
    _write_svg()
    png = _render_icon(PNG_SIZE, core_diameter=336, halo_extent=0)
    png.save(PNG_PATH, format="PNG", optimize=True)
    frames = [
        _render_icon(
            size,
            core_diameter=ICO_CORE_DIAMETERS[size],
            halo_extent=0,
        )
        for size in sorted(ICO_SIZES, reverse=True)
    ]
    frames[0].save(
        ICO_PATH,
        format="ICO",
        sizes=[(size, size) for size in ICO_SIZES],
        append_images=frames[1:],
    )
    return SVG_PATH, PNG_PATH, ICO_PATH


def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    svg, png, ico = generate()
    print(f"generated {svg}")
    print(f"generated {png}")
    print(f"generated {ico}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
