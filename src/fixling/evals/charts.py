"""Tiny dependency-free SVG charts for the evaluation report (accessible: each has a <title> and <desc>)."""

from __future__ import annotations

from dataclasses import dataclass
from xml.sax.saxutils import escape

# One validated, colour-blind-safe pair; text and bars keep >= 4.5:1 contrast on white.
BAR = "#2d6cdf"
WHISKER = "#1b1d1f"
GRID = "#d9dbde"
TEXT = "#1b1d1f"


@dataclass(frozen=True)
class Bar:
    label: str
    value: float  # 0..1
    low: float
    high: float
    note: str = ""  # e.g. "n=12"


def bar_chart_svg(title: str, description: str, bars: list[Bar], *, width: int = 640) -> str:
    """Horizontal bars (a proportion each, 0-100%) with confidence-interval whiskers."""
    row_h, left, right, top = 34, 190, 70, 34
    height = top + row_h * max(1, len(bars)) + 30
    plot = width - left - right

    def x(v: float) -> float:
        return left + max(0.0, min(1.0, v)) * plot

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}" '
        f'role="img" aria-labelledby="t d" font-family="system-ui, sans-serif" font-size="13">',
        f'<title id="t">{escape(title)}</title><desc id="d">{escape(description)}</desc>',
        f'<rect width="{width}" height="{height}" fill="#ffffff"/>',
        f'<text x="{left}" y="20" font-weight="600" fill="{TEXT}">{escape(title)}</text>',
    ]
    for tick in (0, 0.25, 0.5, 0.75, 1.0):
        gx = x(tick)
        parts.append(f'<line x1="{gx:.1f}" y1="{top - 6}" x2="{gx:.1f}" y2="{height - 28}" stroke="{GRID}"/>')
        parts.append(
            f'<text x="{gx:.1f}" y="{height - 12}" text-anchor="middle" fill="{TEXT}">{int(tick * 100)}%</text>'
        )
    for i, b in enumerate(bars):
        y = top + i * row_h
        cy = y + row_h / 2
        parts.append(f'<text x="{left - 8}" y="{cy + 4:.1f}" text-anchor="end" fill="{TEXT}">{escape(b.label)}</text>')
        parts.append(
            f'<rect x="{left}" y="{y + 7}" width="{max(0.0, x(b.value) - left):.1f}" height="{row_h - 14}" fill="{BAR}"/>'
        )
        parts.append(
            f'<line x1="{x(b.low):.1f}" y1="{cy:.1f}" x2="{x(b.high):.1f}" y2="{cy:.1f}" stroke="{WHISKER}" stroke-width="1.5"/>'
        )
        for cap in (b.low, b.high):
            parts.append(
                f'<line x1="{x(cap):.1f}" y1="{cy - 5:.1f}" x2="{x(cap):.1f}" y2="{cy + 5:.1f}" stroke="{WHISKER}" stroke-width="1.5"/>'
            )
        label = f"{b.value:.0%}" + (f" ({escape(b.note)})" if b.note else "")
        parts.append(f'<text x="{x(b.high) + 6:.1f}" y="{cy + 4:.1f}" fill="{TEXT}">{label}</text>')
    parts.append("</svg>")
    return "\n".join(parts)
