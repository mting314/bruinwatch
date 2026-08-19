"""SVG chart output.

The charts are pure functions of their input, so the data-viz rules they have
to obey can be asserted rather than eyeballed: mark specs, no colour-only
encoding, no clipped geometry.
"""

from __future__ import annotations

import datetime as dt
import re

import pytest

from bruinwatch.web import charts, render


def series(n: int, slot: int = 1) -> charts.Series:
    return charts.Series(
        label=f"Lec {slot}",
        slot=slot,
        value_label=f"Lec {slot}",
        points=tuple(charts.Point(x=float(i), y=10.0 * i) for i in range(n)),
    )


# -- escaping --------------------------------------------------------------


def test_labels_are_escaped():
    """Course titles come from scraped HTML; they must not re-enter as markup."""
    svg = charts.bar_chart([charts.Bar(label="<script>x</script>", value=1, display="1")])
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg


def test_hrefs_are_escaped():
    svg = charts.bar_chart([charts.Bar(label="A", value=1, display="1", href='/x?a="b')])
    assert 'href="/x?a=&quot;b"' in svg


# -- line chart ------------------------------------------------------------


def test_line_chart_marks_meet_spec():
    svg = charts.line_chart([series(5)])
    # 2px stroke with round caps, set in CSS on .line
    assert 'class="line"' in svg
    # Marker >= 8px diameter, with a surface ring (CSS .marker)
    assert re.search(r'class="marker"[^>]*r="4"', svg)
    # Grid is solid; a dashed grid reads as a threshold it isn't.
    assert "stroke-dasharray" not in svg


def test_line_chart_direct_labels_every_series():
    """The relief for the light-mode contrast warning: identity is never colour
    alone, so each line carries its own end label."""
    svg = charts.line_chart([series(4, 1), series(4, 3)])
    assert svg.count('class="end-label"') == 2


def test_line_chart_uses_series_slots_not_rank():
    svg = charts.line_chart([series(3, slot=2), series(3, slot=4)])
    assert "var(--series-2)" in svg
    assert "var(--series-4)" in svg
    # Slot 1 was not used, so it must not appear -- colour follows the entity.
    assert "var(--series-1)" not in svg


def test_line_chart_reference_rule_is_labelled():
    svg = charts.line_chart([series(3)], y_max=100, reference=(100.0, "capacity"))
    assert 'class="reference"' in svg
    assert "capacity" in svg


def test_line_chart_axis_band_is_inside_the_viewbox():
    """Regression against the 'card gets a tiny nested scrollbar' anti-pattern:
    x-axis labels must be inside the SVG height, not below it."""
    height = 300
    labels = [(0.0, "Sep 01"), (4.0, "Sep 30")]
    svg = charts.line_chart([series(5)], height=height, x_labels=labels)
    ys = [float(y) for y in re.findall(r'class="tick" x="[\d.]+" y="([\d.]+)"', svg)]
    assert ys, "expected x-axis tick labels"
    assert max(ys) < height, "an axis label falls outside the viewBox"


def test_line_chart_clamps_values_above_the_ceiling():
    """An over-enrolled section is >100% full; it must not draw off-canvas."""
    over = charts.Series(label="Lec 1", slot=1, points=(charts.Point(0, 50), charts.Point(1, 130)))
    svg = charts.line_chart([over], y_max=100, height=300)
    ys = [float(m) for m in re.findall(r"[ML]([\d.]+),([\d.]+)", svg) for m in [m[1]]]
    assert min(ys) >= 0, "path escapes the top of the plot"


def test_line_chart_handles_a_single_point():
    svg = charts.line_chart([series(1)])
    assert "<svg" in svg  # no division by a zero x-span


def test_line_chart_empty_is_an_empty_state_not_a_broken_svg():
    assert "<svg" not in charts.line_chart([])
    assert "No history" in charts.line_chart([])


# -- bar chart -------------------------------------------------------------


def test_bar_chart_is_single_hue():
    """Nominal categories must not be shaded by value -- that double-encodes
    length as colour and burns the only free channel."""
    bars = [charts.Bar(label=f"C{i}", value=float(i + 1), display=str(i)) for i in range(6)]
    svg = charts.bar_chart(bars)
    assert svg.count("var(--series-1)") == 6
    for slot in (2, 3, 4):
        assert f"var(--series-{slot})" not in svg


def test_bar_chart_height_grows_with_rows():
    small = charts.bar_chart([charts.Bar("a", 1, "1")])
    large = charts.bar_chart([charts.Bar(f"r{i}", 1, "1") for i in range(10)])
    assert _svg_height(large) > _svg_height(small)


def test_bar_chart_labels_sit_outside_the_bar():
    """Values are placed past the bar end, so a short bar never clips its own
    label."""
    svg = charts.bar_chart([charts.Bar("tiny", 0.01, "0.01")], y_max=100)
    assert 'class="bar-value"' in svg
    assert "overflow: hidden" not in svg


def test_zero_valued_bar_still_renders():
    svg = charts.bar_chart([charts.Bar("none", 0.0, "0")], y_max=10)
    assert 'class="bar"' in svg


def test_bar_chart_rounded_data_end_square_baseline():
    svg = charts.bar_chart([charts.Bar("a", 5, "5")], y_max=10)
    path = re.search(r'class="bar" d="([^"]+)"', svg)
    assert path, "bar should be a path, so the ends can differ"
    # Two arcs on the data end, none at the baseline.
    assert path.group(1).count("A") == 2


def _svg_height(svg: str) -> int:
    return int(re.search(r'height="(\d+)"', svg).group(1))


# -- legend & small pieces -------------------------------------------------


def test_legend_present_for_multiple_series_absent_for_one():
    assert charts.legend([series(2, 1)]) == ""
    two = charts.legend([series(2, 1), series(2, 2)])
    assert two.count("<li>") == 2


def test_meter_clamps_out_of_range():
    assert "width:100.0%" in charts.meter(1.6)
    assert "width:0.0%" in charts.meter(-1)


def test_time_axis_labels_span_the_range():
    start = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    end = dt.datetime(2026, 9, 30, tzinfo=dt.UTC)
    labels = charts.time_axis_labels(start, end, count=5)
    assert len(labels) == 5
    assert labels[0][1] == "Sep 01"
    assert labels[-1][1] == "Sep 30"
    assert labels[0][0] < labels[-1][0]


def test_time_axis_labels_handle_a_zero_span():
    at = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    assert len(charts.time_axis_labels(at, at)) == 1


@pytest.mark.parametrize(
    ("value", "expected"), [(0, "0"), (5, "5"), (1500, "1.5k"), (2000, "2k"), (0.5, "0.5")]
)
def test_number_formatting(value, expected):
    assert charts._fmt(value) == expected


# -- page shell ------------------------------------------------------------


def test_page_declares_both_dark_mode_scopes():
    """A theme toggle must beat the OS setting, and vice versa."""
    html = render.page("T", "<p>x</p>")
    assert "prefers-color-scheme: dark" in html
    assert ':root[data-theme="dark"]' in html
    assert ':not([data-theme="light"])' in html


def test_page_escapes_its_title():
    assert "<b>" not in render.page("<b>x</b>", "")


def test_status_pill_carries_a_label_not_just_colour():
    pill = render.status_pill("Full", "critical")
    assert "Full" in pill
    assert 'class="status critical"' in pill


def test_table_view_marks_numeric_columns():
    html = render.table_view(["A", "N"], [["x", "1"]], numeric={1})
    assert html.count('class="num"') == 2  # header + cell


# -- label fit -------------------------------------------------------------


def test_long_row_labels_are_truncated_not_clipped():
    """Real subject codes overflow the gutter: "C&S BIO M120 Lec 1" is wider
    than the 122px available. Clipping would crop characters, so we measure and
    shorten instead."""
    svg = charts.bar_chart([charts.Bar("C&S BIO M120 Lec 1", 5, "5")], y_max=10)
    assert "…" in svg
    # The full value survives in the title, so nothing is lost.
    assert "<title>C&amp;S BIO M120 Lec 1</title>" in svg


def test_short_row_labels_are_left_alone():
    svg = charts.bar_chart([charts.Bar("MATH 31A", 5, "5")], y_max=10)
    assert "…" not in svg
    assert "<title>" not in svg


def test_no_rendered_text_overflows_the_viewbox():
    """Every text anchor plus its measured width must land inside the canvas."""
    bars = [
        charts.Bar("EA STDS CM188 Lec 1A", 99.9, "99.9%"),
        charts.Bar("MATH 31A", 0.01, "0.01%"),
    ]
    _assert_text_fits(charts.bar_chart(bars, width=720, y_max=100))

    lines = [
        charts.Series(
            label="Lec 1",
            slot=1,
            value_label="Lec 1",
            points=(charts.Point(0, 0), charts.Point(10, 100)),
        )
    ]
    _assert_text_fits(charts.line_chart(lines, width=720, y_max=100))


def _assert_text_fits(svg: str) -> None:
    width = int(re.search(r'viewBox="0 0 (\d+)', svg).group(1))
    pattern = re.compile(
        r'<text[^>]*\bx="([\d.]+)"[^>]*?(?:text-anchor="(\w+)")?[^>]*>(?:<title>.*?</title>)?([^<]*)</text>'
    )
    for x_str, anchor, text in pattern.findall(svg):
        x, w = float(x_str), len(text) * charts.CHAR_PX
        left = x - w if anchor == "end" else (x - w / 2 if anchor == "middle" else x)
        assert left >= -1, f"{text!r} runs off the left edge (x={left:.0f})"
        assert left + w <= width + 1, (
            f"{text!r} runs off the right edge (x={left + w:.0f} > {width})"
        )


@pytest.mark.parametrize(
    ("text", "avail", "expected_fits"),
    [("MATH 31A", 122, True), ("C&S BIO M120 Lec 1", 122, False), ("", 10, True)],
)
def test_fits(text, avail, expected_fits):
    assert charts.fits(text, avail) is expected_fits
