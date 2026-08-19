"""Server-rendered inline SVG charts.

Inline SVG rather than a JS charting library: no CDN, no build step, no
client-side data fetch, and the markup is a pure function of its input so it can
be unit tested. Every chart here is written against CSS custom properties
(``--series-1``, ``--grid``, ...) defined once in :mod:`bruinwatch.web.render`,
so light and dark mode swap in one place.

Specs follow the project's data-viz rules: 2px lines with round caps, markers
>= 8px carrying a 2px surface ring, bars capped at 24px with a 4px rounded
data-end and square baseline, solid hairline grid one step off the surface, and
text in ink tokens rather than the series colour.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import html
from collections.abc import Sequence

#: How many categorical slots exist. Past this we fold into "Other" rather than
#: inventing a hue -- a generated 9th colour is indistinguishable under CVD.
MAX_SERIES = 4


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _fmt(value: float) -> str:
    """Compact axis/label number."""
    if value >= 1000:
        return f"{value / 1000:.1f}k".replace(".0k", "k")
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


@dataclasses.dataclass(frozen=True, slots=True)
class Point:
    x: float
    y: float


@dataclasses.dataclass(frozen=True, slots=True)
class Series:
    label: str
    points: tuple[Point, ...]
    #: Categorical slot, 1-based. Fixed per entity, never by rank, so filtering
    #: a series out never repaints the survivors.
    slot: int
    #: Rendered beside the line end and in the legend.
    value_label: str = ""


def _nice_ceiling(value: float) -> float:
    """Round an axis maximum up to something a human would choose."""
    if value <= 0:
        return 1.0
    for step in (1, 2, 2.5, 5, 10):
        for magnitude in (0.01, 0.1, 1, 10, 100, 1000, 10000):
            candidate = step * magnitude
            if candidate >= value:
                return candidate
    return value


def _ticks(maximum: float, count: int = 4) -> list[float]:
    return [maximum * i / count for i in range(count + 1)]


#: Conservative width of one 12px system-ui character. Used to measure a label
#: before placing it, because SVG will happily draw text straight off the
#: canvas -- and clipping it would crop the first or last characters, which is
#: worse than not labelling at all.
CHAR_PX = 7.0


def fits(text: str, available_px: float) -> bool:
    return len(text) * CHAR_PX <= available_px


def truncate(text: str, available_px: float) -> str:
    """Shorten a label to fit, with an ellipsis.

    Real subject codes get long -- "C&S BIO M120 Lec 1" overflows the row-label
    gutter. The full value always remains in the chart's table view and in the
    element's ``<title>``, so nothing is lost.
    """
    if fits(text, available_px):
        return text
    keep = max(1, int(available_px / CHAR_PX) - 1)
    return text[:keep].rstrip() + "…"


# --------------------------------------------------------------------------
# Line chart
# --------------------------------------------------------------------------


def line_chart(
    series: Sequence[Series],
    *,
    width: int = 720,
    height: int = 300,
    y_max: float | None = None,
    y_suffix: str = "",
    x_labels: Sequence[tuple[float, str]] = (),
    reference: tuple[float, str] | None = None,
    title: str = "",
) -> str:
    """A multi-series line chart.

    ``reference`` draws a single labelled horizontal rule -- used for the 100%
    capacity line, which is the thing a reader is actually looking for.
    """
    if not series:
        return empty_state("No history recorded yet.")

    # Leave room below the plot for the x-axis band, so the axis labels are
    # inside the SVG rather than clipped by the card.
    pad_l, pad_r, pad_t, pad_b = 44, 92, 16, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    all_y = [p.y for s in series for p in s.points]
    all_x = [p.x for s in series for p in s.points]
    if not all_y:
        return empty_state("No history recorded yet.")

    top = y_max if y_max is not None else _nice_ceiling(max(all_y) * 1.05)
    x_lo, x_hi = min(all_x), max(all_x)
    x_span = (x_hi - x_lo) or 1.0

    def sx(x: float) -> float:
        return pad_l + (x - x_lo) / x_span * plot_w

    def sy(y: float) -> float:
        return pad_t + plot_h - (min(y, top) / top) * plot_h

    out: list[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="{esc(title or "line chart")}">'
    ]

    # Grid + y ticks. Solid hairlines, one step off surface, recessive.
    for tick in _ticks(top):
        y = sy(tick)
        out.append(
            f'<line class="grid" x1="{pad_l}" y1="{y:.1f}" x2="{pad_l + plot_w}" y2="{y:.1f}"/>'
        )
        out.append(
            f'<text class="tick" x="{pad_l - 8}" y="{y + 4:.1f}" text-anchor="end">'
            f"{_fmt(tick)}{esc(y_suffix)}</text>"
        )

    if reference is not None:
        ref_value, ref_label = reference
        if ref_value <= top:
            y = sy(ref_value)
            out.append(
                f'<line class="reference" x1="{pad_l}" y1="{y:.1f}" '
                f'x2="{pad_l + plot_w}" y2="{y:.1f}"/>'
            )
            out.append(
                f'<text class="tick" x="{pad_l + plot_w - 4}" y="{y - 6:.1f}" '
                f'text-anchor="end">{esc(ref_label)}</text>'
            )

    for x_value, label in x_labels:
        x = sx(x_value)
        out.append(
            f'<text class="tick" x="{x:.1f}" y="{pad_t + plot_h + 20}" '
            f'text-anchor="middle">{esc(label)}</text>'
        )

    out.append(
        f'<line class="axis" x1="{pad_l}" y1="{pad_t + plot_h}" '
        f'x2="{pad_l + plot_w}" y2="{pad_t + plot_h}"/>'
    )

    for s in series:
        if not s.points:
            continue
        colour = f"var(--series-{s.slot})"
        path = " ".join(
            f"{'M' if i == 0 else 'L'}{sx(p.x):.1f},{sy(p.y):.1f}" for i, p in enumerate(s.points)
        )
        out.append(f'<path class="line" d="{path}" stroke="{colour}"/>')

        last = s.points[-1]
        cx, cy = sx(last.x), sy(last.y)
        # 2px surface ring keeps the marker legible where lines overlap.
        out.append(f'<circle class="marker" cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{colour}"/>')
        # Direct end-label: the relief for the light-mode contrast warning on
        # the aqua and yellow slots, and how identity survives without colour.
        out.append(
            f'<text class="end-label" x="{cx + 10:.1f}" y="{cy + 4:.1f}">'
            f"{esc(s.value_label or s.label)}</text>"
        )

    out.append("</svg>")
    return "".join(out)


# --------------------------------------------------------------------------
# Horizontal bar chart
# --------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True, slots=True)
class Bar:
    label: str
    value: float
    display: str
    href: str | None = None


def bar_chart(
    bars: Sequence[Bar],
    *,
    width: int = 720,
    y_max: float | None = None,
    reference: tuple[float, str] | None = None,
    title: str = "",
) -> str:
    """A ranked horizontal bar chart.

    One hue for every bar. The categories here (courses, subjects) have no
    natural order, so shading them by value would double-encode bar length as
    colour and burn the only free channel on information the chart already
    shows.
    """
    if not bars:
        return empty_state("Not enough data yet.")

    row_h, gap, pad_t, pad_b = 26, 6, 8, 26
    label_w, value_w = 132, 66
    plot_w = width - label_w - value_w
    height = pad_t + pad_b + len(bars) * (row_h + gap)

    top = y_max if y_max is not None else _nice_ceiling(max(b.value for b in bars))

    out: list[str] = [
        f'<svg class="chart" viewBox="0 0 {width} {height}" width="100%" '
        f'height="{height}" role="img" aria-label="{esc(title or "bar chart")}">'
    ]

    for tick in _ticks(top):
        x = label_w + tick / top * plot_w
        out.append(
            f'<line class="grid" x1="{x:.1f}" y1="{pad_t}" x2="{x:.1f}" y2="{height - pad_b:.1f}"/>'
        )
        out.append(
            f'<text class="tick" x="{x:.1f}" y="{height - pad_b + 18:.1f}" '
            f'text-anchor="middle">{_fmt(tick)}</text>'
        )

    if reference is not None:
        ref_value, ref_label = reference
        if 0 < ref_value <= top:
            x = label_w + ref_value / top * plot_w
            out.append(
                f'<line class="reference" x1="{x:.1f}" y1="{pad_t}" '
                f'x2="{x:.1f}" y2="{height - pad_b:.1f}"/>'
            )
            out.append(
                f'<text class="tick" x="{x + 5:.1f}" y="{pad_t + 10}">{esc(ref_label)}</text>'
            )

    for i, bar in enumerate(bars):
        y = pad_t + i * (row_h + gap)
        # Cap the mark thickness; the band's leftover is deliberate air.
        bar_h = min(20, row_h)
        by = y + (row_h - bar_h) / 2
        bw = max(2.0, bar.value / top * plot_w)

        # Measure before placing: a label that does not fit is shortened, never
        # clipped. The full text stays in the title attribute and the table.
        shown = truncate(bar.label, label_w - 10)
        title = f"<title>{esc(bar.label)}</title>" if shown != bar.label else ""
        if bar.href:
            out.append(f'<a href="{esc(bar.href)}">')
        out.append(
            f'<text class="row-label" x="{label_w - 10}" y="{y + row_h / 2 + 4:.1f}" '
            f'text-anchor="end">{title}{esc(shown)}</text>'
        )
        # 4px rounded data-end, square against the baseline.
        out.append(
            f'<path class="bar" d="{_bar_path(label_w, by, bw, bar_h, 4)}" fill="var(--series-1)"/>'
        )
        out.append(
            f'<text class="bar-value" x="{label_w + bw + 8:.1f}" '
            f'y="{y + row_h / 2 + 4:.1f}">{esc(bar.display)}</text>'
        )
        if bar.href:
            out.append("</a>")

    out.append(
        f'<line class="axis" x1="{label_w}" y1="{pad_t}" x2="{label_w}" y2="{height - pad_b:.1f}"/>'
    )
    out.append("</svg>")
    return "".join(out)


def _bar_path(x: float, y: float, w: float, h: float, r: float) -> str:
    """Bar with rounded data-end (right) and square baseline (left)."""
    r = min(r, w / 2, h / 2)
    return (
        f"M{x:.1f},{y:.1f} H{x + w - r:.1f} "
        f"A{r},{r} 0 0 1 {x + w:.1f},{y + r:.1f} "
        f"V{y + h - r:.1f} "
        f"A{r},{r} 0 0 1 {x + w - r:.1f},{y + h:.1f} "
        f"H{x:.1f} Z"
    )


# --------------------------------------------------------------------------
# Small pieces
# --------------------------------------------------------------------------


def legend(series: Sequence[Series]) -> str:
    """Always present for two or more series; identity is never colour alone."""
    if len(series) < 2:
        return ""
    items = "".join(
        f'<li><span class="key" style="background:var(--series-{s.slot})"></span>'
        f"{esc(s.label)}</li>"
        for s in series
    )
    return f'<ul class="legend">{items}</ul>'


def meter(fraction: float, tone: str = "series-1") -> str:
    """A single ratio against its limit. The track is a lighter step of the
    same ramp, so state reads across the whole bar."""
    pct = max(0.0, min(1.0, fraction)) * 100
    return (
        f'<div class="meter" role="img" aria-label="{pct:.0f}% full">'
        f'<div class="meter-fill" style="width:{pct:.1f}%;background:var(--{tone})"></div>'
        f"</div>"
    )


def stat_tile(label: str, value: str, note: str = "") -> str:
    note_html = f'<div class="stat-note">{esc(note)}</div>' if note else ""
    return (
        f'<div class="stat"><div class="stat-label">{esc(label)}</div>'
        f'<div class="stat-value">{esc(value)}</div>{note_html}</div>'
    )


def empty_state(message: str) -> str:
    return f'<p class="empty">{esc(message)}</p>'


def time_axis_labels(
    start: dt.datetime, end: dt.datetime, count: int = 5
) -> list[tuple[float, str]]:
    """Evenly spaced date ticks across a time range, as (epoch, label)."""
    if end <= start:
        return [(start.timestamp(), start.strftime("%b %d"))]
    span = (end - start).total_seconds()
    out = []
    for i in range(count):
        at = start + dt.timedelta(seconds=span * i / (count - 1))
        out.append((at.timestamp(), at.strftime("%b %d")))
    return out
