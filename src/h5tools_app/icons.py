"""Small vector-style icons, drawn on demand with Pillow instead of
relying on emoji glyphs.

Emoji icons depend on a color-emoji font being installed system-wide;
on a bare-bones Linux desktop that's often missing, so the folder/dataset
glyphs would silently fall back to "tofu" boxes. Drawing them ourselves at
a few fixed sizes/colors keeps the tree looking correct everywhere and
lets each icon adopt the app's accent color, which no bundled emoji does
anyway. Results are cached since callers re-fetch on every theme flip.
"""
from __future__ import annotations

from functools import lru_cache

from PIL import Image, ImageDraw
from PySide6.QtGui import QIcon, QImage, QPixmap

_SUPERSAMPLE = 4

GROUP = "group"
DATASET = "dataset"
MINIMIZE = "minimize"
MAXIMIZE = "maximize"
RESTORE = "restore"
CLOSE = "close"
NAVIGATE = "navigate"
CHART = "chart"


def _draw_group(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    tab_w = s * 0.46
    tab_h = s * 0.16
    body_top = pad + tab_h
    draw.rounded_rectangle([pad, pad, pad + tab_w, body_top + s * 0.06], radius=s * 0.07, fill=color)
    draw.rounded_rectangle([pad, body_top, s - pad, s - pad], radius=s * 0.12, fill=color)


def _draw_dataset(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    draw.rounded_rectangle([pad, pad, s - pad, s - pad], radius=s * 0.14, outline=color, width=width)
    inner_lo, inner_hi = pad + width / 2, s - pad - width / 2
    mid_y = pad + (s - 2 * pad) * 0.42
    draw.line([(inner_lo, mid_y), (inner_hi, mid_y)], fill=color, width=width)
    mid_x = pad + (s - 2 * pad) * 0.5
    draw.line([(mid_x, mid_y), (mid_x, inner_hi)], fill=color, width=width)


def _draw_minimize(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    y = s * 0.58
    draw.line([(pad, y), (s - pad, y)], fill=color, width=width)


def _draw_maximize(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    draw.rectangle([pad, pad, s - pad, s - pad], outline=color, width=width)


def _draw_restore(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    shift = s * 0.16
    draw.rectangle([pad + shift, pad, s - pad, s - pad - shift], outline=color, width=width)
    draw.rectangle([pad, pad + shift, s - pad - shift, s - pad], outline=color, width=width)


def _draw_close(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    draw.line([(pad, pad), (s - pad, s - pad)], fill=color, width=width)
    draw.line([(pad, s - pad), (s - pad, pad)], fill=color, width=width)


def _draw_navigate(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    # A simple crosshair/target -- used on the trigger button that opens
    # the row-navigation popover (Top / End / jump-to-row).
    cx = cy = s / 2
    r = (s - 2 * pad) / 2
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=width)
    tick = r * 0.55
    draw.line([(cx, pad), (cx, pad + tick)], fill=color, width=width)
    draw.line([(cx, s - pad - tick), (cx, s - pad)], fill=color, width=width)
    draw.line([(pad, cy), (pad + tick, cy)], fill=color, width=width)
    draw.line([(s - pad - tick, cy), (s - pad, cy)], fill=color, width=width)
    dot_r = width * 0.9
    draw.ellipse([cx - dot_r, cy - dot_r, cx + dot_r, cy + dot_r], fill=color)


def _draw_chart(draw: ImageDraw.ImageDraw, s: float, pad: float, color: str, width: int) -> None:
    # Ascending bar-chart glyph -- used on the trigger button that opens
    # the graph-configuration dialog for the currently selected columns.
    base_y = s - pad
    bar_w = (s - 2 * pad) * 0.22
    gap = (s - 2 * pad) * 0.12
    heights = (0.35, 0.62, 0.9)
    x = pad
    for h in heights:
        top_y = base_y - (s - 2 * pad) * h
        draw.rounded_rectangle([x, top_y, x + bar_w, base_y], radius=bar_w * 0.25, fill=color)
        x += bar_w + gap


_DRAWERS = {
    GROUP: _draw_group,
    DATASET: _draw_dataset,
    MINIMIZE: _draw_minimize,
    MAXIMIZE: _draw_maximize,
    RESTORE: _draw_restore,
    CLOSE: _draw_close,
    NAVIGATE: _draw_navigate,
    CHART: _draw_chart,
}


def _draw(kind: str, color: str, size: int) -> Image.Image:
    s = size * _SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    pad = s * 0.09
    width = max(1, round(s * 0.075))
    _DRAWERS[kind](draw, s, pad, color, width)
    return img.resize((size, size), Image.LANCZOS)


def _to_qpixmap(img: Image.Image) -> QPixmap:
    img = img.convert("RGBA")
    data = img.tobytes("raw", "RGBA")
    qimage = QImage(data, img.width, img.height, QImage.Format.Format_RGBA8888)
    # .copy() detaches from `data` so the QImage/QPixmap survive after this
    # function returns and `data` gets garbage collected.
    return QPixmap.fromImage(qimage.copy())


@lru_cache(maxsize=64)
def pixmap(kind: str, color: str, size: int = 16) -> QPixmap:
    return _to_qpixmap(_draw(kind, color, size))


def icon(kind: str, color: str, size: int = 16) -> QIcon:
    return QIcon(pixmap(kind, color, size))
