"""The HTML shell: palette, layout, and page chrome.

The colour values here are the validated reference palette. Both modes were
checked with the data-viz validator against the surfaces below:

    light  #2a78d6 #eb6834 #1baf7a #eda100  on #fcfcfb  -> all checks pass
    dark   #3987e5 #d95926 #199e70 #c98500  on #1a1a19  -> all checks pass

Light mode carries a contrast warning on the aqua and yellow slots (2.74 and
2.11 against the surface). That is not dismissable: the relief is that every
chart ships direct end-labels *and* a table view, so no value is reachable by
colour alone.

Dark mode is a selected set of steps for the dark surface, not an automatic
inversion, and is declared under both the media query and the ``data-theme``
scope so an explicit toggle wins either way.
"""

from __future__ import annotations

from collections.abc import Collection

from . import links
from .charts import esc

STYLE = """
:root {
  color-scheme: light;
  --surface-1: #fcfcfb;
  --plane: #f9f9f7;
  --text-primary: #0b0b0b;
  --text-secondary: #52514e;
  --text-muted: #898781;
  --grid: #e1e0d9;
  --axis: #c3c2b7;
  --border: rgba(11,11,11,0.10);

  --series-1: #2a78d6;
  --series-2: #eb6834;
  --series-3: #1baf7a;
  --series-4: #eda100;
  --series-1-wash: rgba(42,120,214,0.12);

  --good: #0ca30c;
  --warning: #fab219;
  --serious: #ec835a;
  --critical: #d03b3b;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --plane: #0d0d0d;
    --text-primary: #ffffff;
    --text-secondary: #c3c2b7;
    --text-muted: #898781;
    --grid: #2c2c2a;
    --axis: #383835;
    --border: rgba(255,255,255,0.10);
    --series-1: #3987e5;
    --series-2: #d95926;
    --series-3: #199e70;
    --series-4: #c98500;
    --series-1-wash: rgba(57,135,229,0.16);
  }
}
:root[data-theme="dark"] {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --plane: #0d0d0d;
  --text-primary: #ffffff;
  --text-secondary: #c3c2b7;
  --text-muted: #898781;
  --grid: #2c2c2a;
  --axis: #383835;
  --border: rgba(255,255,255,0.10);
  --series-1: #3987e5;
  --series-2: #d95926;
  --series-3: #199e70;
  --series-4: #c98500;
  --series-1-wash: rgba(57,135,229,0.16);
}

* { box-sizing: border-box; }
body {
  margin: 0;
  background: var(--plane);
  color: var(--text-primary);
  font: 15px/1.55 system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 980px; margin: 0 auto; padding: 32px 20px 72px; }

header.site { display: flex; align-items: baseline; gap: 16px; margin-bottom: 4px; }
header.site h1 { font-size: 19px; margin: 0; letter-spacing: -0.01em; }
header.site nav { margin-left: auto; display: flex; gap: 16px; }
header.site a { color: var(--text-secondary); text-decoration: none; font-size: 14px; }
header.site a:hover { color: var(--text-primary); text-decoration: underline; }
.subtitle { color: var(--text-secondary); margin: 0 0 28px; font-size: 14px; }

.card {
  background: var(--surface-1);
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 20px 22px;
  margin-bottom: 20px;
}
.card > h2 { font-size: 15px; margin: 0 0 2px; letter-spacing: -0.005em; }
.card > .hint { color: var(--text-secondary); font-size: 13px; margin: 0 0 18px; }

/* Hero: exactly one per view. Proportional figures, project sans. */
.hero { font-size: 52px; line-height: 1.05; font-weight: 600; letter-spacing: -0.03em; }
.hero-note { color: var(--text-secondary); font-size: 14px; margin-top: 6px; }

.kpis { display: grid; grid-template-columns: repeat(auto-fit,minmax(150px,1fr)); gap: 12px; }
.stat { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; padding: 14px 16px; }
.stat-label { color: var(--text-secondary); font-size: 12px; text-transform: none; }
.stat-value { font-size: 25px; font-weight: 600; letter-spacing: -0.02em; margin-top: 2px; }
.stat-note { color: var(--text-muted); font-size: 12px; margin-top: 2px; }

/* Charts -------------------------------------------------------------- */
.chart { display: block; overflow: visible; }
.chart .grid { stroke: var(--grid); stroke-width: 1; }
.chart .axis { stroke: var(--axis); stroke-width: 1; }
.chart .reference { stroke: var(--axis); stroke-width: 1; }
.chart .line { fill: none; stroke-width: 2; stroke-linejoin: round; stroke-linecap: round; }
.chart .marker { stroke: var(--surface-1); stroke-width: 2; }
.chart .bar { }
.chart text { font: 12px system-ui, -apple-system, "Segoe UI", sans-serif; }
.chart .tick { fill: var(--text-muted); font-variant-numeric: tabular-nums; }
.chart .row-label { fill: var(--text-secondary); }
.chart .bar-value { fill: var(--text-primary); font-variant-numeric: tabular-nums; }
.chart .end-label { fill: var(--text-secondary); }
.chart a:hover text { fill: var(--text-primary); text-decoration: underline; }

.legend { list-style: none; display: flex; flex-wrap: wrap; gap: 6px 18px; padding: 0; margin: 14px 0 0; }
.legend li { display: flex; align-items: center; gap: 7px; font-size: 13px; color: var(--text-secondary); }
.legend .key { width: 11px; height: 11px; border-radius: 3px; display: inline-block; }

.meter { background: var(--series-1-wash); border-radius: 999px; height: 7px; overflow: hidden; min-width: 70px; }
.meter-fill { height: 100%; border-radius: 999px; }

/* Tables: the WCAG-clean twin of every chart. */
table { border-collapse: collapse; width: 100%; font-size: 13.5px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--border); }
th { color: var(--text-secondary); font-weight: 600; font-size: 12px; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
tbody tr:last-child td { border-bottom: none; }
table a { color: var(--text-primary); }

details.table-view { margin-top: 16px; }
details.table-view summary { cursor: pointer; color: var(--text-secondary); font-size: 13px; }
details.table-view summary:hover { color: var(--text-primary); }
details.table-view > div { margin-top: 12px; }

/* Status: colour never carries meaning alone -- always a dot plus its label. */
.status { display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; }
.status .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
.status.good .dot { background: var(--good); }
.status.warning .dot { background: var(--warning); }
.status.critical .dot { background: var(--critical); }
.status.serious .dot { background: var(--serious); }
.status.neutral .dot { background: var(--text-muted); }

/* Demo banner. Deliberately loud and un-dismissable: a page of synthetic
   numbers that looks like a real one is worse than no page at all. */
.demo-banner {
  background: var(--warning);
  color: #0b0b0b;
  font-size: 13.5px;
  font-weight: 600;
  padding: 10px 20px;
  margin: -32px -20px 24px;
  text-align: center;
}
.demo-banner span { font-weight: 400; }

.empty { color: var(--text-secondary); font-size: 14px; margin: 4px 0; }
.notice {
  background: var(--surface-1); border: 1px solid var(--border);
  border-left: 3px solid var(--series-4);
  border-radius: 8px; padding: 14px 18px; margin-bottom: 20px;
  color: var(--text-secondary); font-size: 14px;
}
.notice strong { color: var(--text-primary); }
footer.site { color: var(--text-muted); font-size: 12.5px; margin-top: 36px; }
footer.site a { color: var(--text-muted); }
"""


DEMO_BANNER = (
    '<div class="demo-banner">DEMO DATA — none of this is real. '
    "<span>Synthetic sections, enrollment curves and instructor names, "
    "generated for local development. Nothing here came from the UCLA "
    "registrar.</span></div>"
)


def page(title: str, body: str, *, subtitle: str = "", demo: bool = False) -> str:
    """Wrap page content in the site shell.

    ``demo`` prints an un-dismissable banner. A page of synthetic numbers that
    looks identical to a real one is worse than no page: it invites exactly the
    "wait, why is that professor teaching econ?" confusion.
    """
    sub = f'<p class="subtitle">{esc(subtitle)}</p>' if subtitle else ""
    banner = DEMO_BANNER if demo else ""
    return (
        '<!doctype html><html lang="en"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{'[DEMO] ' if demo else ''}{esc(title)} · BruinWatch</title>"
        f"<style>{STYLE}</style>"
        '</head><body><div class="wrap">'
        f"{banner}"
        '<header class="site"><h1>BruinWatch</h1>'
        f'<nav><a href="{links.overview()}">Overview</a>'
        f'<a href="{links.course_index()}">Courses</a>'
        f'<a href="{links.api_summary()}">API</a></nav></header>'
        f"{sub}{body}"
        '<footer class="site">Scraped from the UCLA Registrar\'s public '
        '<a href="https://sa.ucla.edu/ro/Public/SOC/">Schedule of Classes</a>. '
        "Enrollment history begins when this bot first observed a section, "
        "not when the registrar opened enrollment.</footer>"
        "</div></body></html>"
    )


def card(title: str, body: str, *, hint: str = "") -> str:
    hint_html = f'<p class="hint">{esc(hint)}</p>' if hint else ""
    return f'<section class="card"><h2>{esc(title)}</h2>{hint_html}{body}</section>'


def table_view(headers: list[str], rows: list[list[str]], *, numeric: Collection[int] = ()) -> str:
    """The accessible twin of a chart. Cell content is pre-escaped by callers
    that pass markup (status pills); plain strings are escaped here."""
    head = "".join(
        f'<th class="num">{esc(h)}</th>' if i in numeric else f"<th>{esc(h)}</th>"
        for i, h in enumerate(headers)
    )
    body = "".join(
        "<tr>"
        + "".join(
            f'<td class="num">{cell}</td>' if i in numeric else f"<td>{cell}</td>"
            for i, cell in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def collapsible_table(
    summary_text: str, headers: list[str], rows: list[list[str]], *, numeric: Collection[int] = ()
) -> str:
    return (
        f'<details class="table-view"><summary>{esc(summary_text)}</summary>'
        f"<div>{table_view(headers, rows, numeric=numeric)}</div></details>"
    )


def status_pill(status: str, tone: str) -> str:
    """Status colour plus its label -- never the colour alone."""
    return f'<span class="status {esc(tone)}"><span class="dot"></span>{esc(status)}</span>'


def notice(message: str) -> str:
    return f'<div class="notice">{message}</div>'
