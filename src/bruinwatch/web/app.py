"""The stats site and JSON API.

Read-only, served from the same aiohttp app as ``/healthz`` so the bot stays a
single process. Every chart ships a table view alongside it, so no value is
reachable by colour alone.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any

import structlog
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import analytics
from ..db import repo
from . import charts, links, render
from .charts import Bar, Point, Series, esc

if TYPE_CHECKING:
    from ..bot import BruinWatchBot

log = structlog.get_logger(__name__)

#: Lines on the fill-curve chart. Past this we fold to the table rather than
#: inventing hues; four is the categorical ceiling for adjacent forms.
MAX_PLOTTED_SECTIONS = charts.MAX_SERIES


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------


async def stats_overview(request: web.Request) -> web.Response:
    sessions: async_sessionmaker[AsyncSession] = request.app["sessions"]
    async with sessions() as session:
        term = request.query.get("term") or await repo.default_term_code(session)
        summary = await analytics.summary(session)
        if term is None:
            return web.Response(
                text=render.page(
                    "Stats",
                    render.notice("No terms loaded yet — the bot has not run a sync."),
                    demo=_demo(request),
                ),
                content_type="text/html",
            )
        statuses = await analytics.status_breakdown(session, term)
        demand = await analytics.most_in_demand(session, term)
        pressure = await analytics.subject_pressure(session, term)
        speed = await analytics.fastest_filling(session, term)

    return web.Response(
        text=render_overview(term, summary, statuses, demand, pressure, speed, demo=_demo(request)),
        content_type="text/html",
    )


def render_overview(
    term: str,
    summary: analytics.Summary,
    statuses: list[analytics.StatusCount],
    demand: list[analytics.CourseDemand],
    pressure: list[analytics.SubjectPressure],
    speed: list[analytics.FillSpeed],
    *,
    demo: bool = False,
) -> str:
    """The overview page. Shared by the live server and the static renderer."""
    body: list[str] = []

    if not summary.has_history:
        body.append(
            render.notice(
                "<strong>No enrollment history yet.</strong> These pages fill in as the "
                "scraper runs — the hourly sweep records a data point whenever a section's "
                "numbers move. Come back in a day for trends, and after a second term for "
                "term-over-term comparisons."
            )
        )

    total_sections = sum(s.sections for s in statuses)
    closed = sum(s.sections for s in statuses if s.status in analytics.CLOSED_STATUSES)
    closed_pct = 100 * closed / total_sections if total_sections else 0.0

    # Hero: exactly one per view, and the number the page leads with.
    body.append(
        render.card(
            f"{term} at a glance",
            f'<div class="hero">{closed_pct:.0f}%</div>'
            f'<p class="hero-note">of {total_sections:,} tracked sections are not open '
            f"for enrollment right now.</p>" + _status_table(statuses, total_sections),
        )
    )

    body.append(
        '<div class="kpis">'
        + charts.stat_tile("Courses", f"{summary.courses:,}")
        + charts.stat_tile("Sections", f"{summary.sections:,}")
        + charts.stat_tile(
            "Observations", f"{summary.observations:,}", "enrollment changes recorded"
        )
        + charts.stat_tile(
            "History",
            f"{summary.days_of_history:.0f}d" if summary.has_history else "—",
            "since first observation",
        )
        + charts.stat_tile("Watched", f"{summary.watched_sections:,}", "sections with subscribers")
        + "</div>"
    )

    body.append(_demand_card(demand, term))
    body.append(_pressure_card(pressure))
    body.append(_speed_card(speed))

    return render.page(
        "Stats",
        "".join(body),
        subtitle=f"Enrollment analytics for {term}.",
        demo=demo,
    )


def _status_table(statuses: list[analytics.StatusCount], total: int) -> str:
    if not statuses:
        return charts.empty_state("No sections recorded for this term yet.")
    rows = [
        [
            render.status_pill(s.status, analytics.status_tone(s.status)),
            f"{s.sections:,}",
            f"{100 * s.sections / total:.1f}%" if total else "—",
        ]
        for s in statuses
    ]
    return render.collapsible_table(
        "Breakdown by status", ["Status", "Sections", "Share"], rows, numeric={1, 2}
    )


def _demand_card(demand: list[analytics.CourseDemand], term: str) -> str:
    if not demand:
        return render.card(
            "Most in demand",
            charts.empty_state("Not enough section data yet."),
        )
    bars = [
        Bar(
            label=d.label,
            value=d.demand_ratio,
            display=f"{d.demand_ratio:.2f}×",
            href=links.course(d.subject_area_code, d.course_number, term),
        )
        for d in demand
    ]
    rows = [
        [
            f'<a href="{esc(links.course(d.subject_area_code, d.course_number, term))}">'
            f"{esc(d.label)}</a>",
            esc(d.title),
            f"{d.enrolled:,}",
            f"{d.capacity:,}",
            f"{d.waitlisted:,}",
            f"{d.demand_ratio:.2f}×",
        ]
        for d in demand
    ]
    return render.card(
        "Most in demand",
        charts.bar_chart(
            bars,
            reference=(1.0, "capacity"),
            title="Courses by demand ratio",
        )
        + render.collapsible_table(
            "Show the numbers",
            ["Course", "Title", "Enrolled", "Capacity", "Waitlisted", "Demand"],
            rows,
            numeric={2, 3, 4, 5},
        ),
        hint="Seats wanted per seat available — enrolled plus waitlisted, over capacity. "
        "Above 1.0× means more students want in than the room holds.",
    )


def _pressure_card(pressure: list[analytics.SubjectPressure]) -> str:
    if not pressure:
        # The empty state has to carry the explanation itself -- the hint below
        # only renders alongside content, so without this the reader is told
        # "not enough data" with no idea what would be enough.
        return render.card(
            "Hardest subjects to get into",
            charts.empty_state(
                "No subject has at least ten tracked sections yet. This fills in once the "
                "daily catalog sync has walked the whole term."
            ),
        )
    bars = [
        Bar(
            label=p.subject_area_code,
            value=p.closed_share * 100,
            display=f"{p.closed_share * 100:.0f}%",
        )
        for p in pressure
    ]
    rows = [
        [
            esc(p.subject_area_code),
            esc(p.name),
            f"{p.closed_sections:,}",
            f"{p.sections:,}",
            f"{p.closed_share * 100:.0f}%",
        ]
        for p in pressure
    ]
    return render.card(
        "Hardest subjects to get into",
        charts.bar_chart(bars, y_max=100, title="Share of sections closed by subject")
        + render.collapsible_table(
            "Show the numbers",
            ["Code", "Subject", "Closed", "Sections", "Share"],
            rows,
            numeric={2, 3, 4},
        ),
        hint="Share of each subject's sections that are full, waitlisted, or closed. "
        "Subjects with fewer than ten sections are omitted.",
    )


def _speed_card(speed: list[analytics.FillSpeed]) -> str:
    if not speed:
        return render.card(
            "Fastest to fill",
            charts.empty_state(
                "No section has been seen going from open to full yet. This needs history "
                "spanning an enrollment pass."
            ),
        )
    bars = [
        Bar(label=s.label, value=s.hours_to_full, display=_hours(s.hours_to_full)) for s in speed
    ]
    rows = [[esc(s.label), _hours(s.hours_to_full), f"{s.capacity:,}"] for s in speed]
    return render.card(
        "Fastest to fill",
        charts.bar_chart(bars, title="Hours from first observation to full")
        + render.collapsible_table(
            "Show the numbers", ["Section", "Time to full", "Capacity"], rows, numeric={1, 2}
        ),
        hint="Measured from this bot's first observation of the section, not from when the "
        "registrar opened enrollment — a section we met already-full is not counted.",
    )


def _hours(value: float) -> str:
    if value < 1:
        return f"{value * 60:.0f} min"
    if value < 48:
        return f"{value:.1f} h"
    return f"{value / 24:.1f} d"


# --------------------------------------------------------------------------
# Course index and detail
# --------------------------------------------------------------------------


async def course_index(request: web.Request) -> web.Response:
    sessions: async_sessionmaker[AsyncSession] = request.app["sessions"]
    async with sessions() as session:
        term = request.query.get("term") or await repo.default_term_code(session)
        if term is None:
            return web.Response(
                text=render.page(
                    "Courses",
                    render.notice("No terms loaded yet."),
                    demo=_demo(request),
                ),
                content_type="text/html",
            )
        courses = await analytics.tracked_courses(session, term)

    return web.Response(
        text=render_course_index(term, courses, demo=_demo(request)),
        content_type="text/html",
    )


def render_course_index(
    term: str, courses: list[tuple[str, str, str]], *, demo: bool = False
) -> str:
    """The course index. Shared by the live server and the static renderer."""
    if not courses:
        body = render.card(
            "Courses with history",
            charts.empty_state("Nothing recorded for this term yet."),
        )
    else:
        rows = [
            [
                f'<a href="{esc(links.course(code, number, term))}">{esc(code)} {esc(number)}</a>',
                esc(title),
            ]
            for code, number, title in courses
        ]
        body = render.card(
            "Courses with history",
            render.table_view(["Course", "Title"], rows),
            hint=f"{len(courses):,} courses have recorded enrollment history in {term}, "
            "most-observed first.",
        )
    return render.page("Courses", body, subtitle=f"Tracked courses in {term}.", demo=demo)


async def course_detail(request: web.Request) -> web.Response:
    subject = request.match_info["subject"].upper()
    number = request.match_info["number"].upper()
    sessions: async_sessionmaker[AsyncSession] = request.app["sessions"]

    async with sessions() as session:
        term = request.query.get("term") or await repo.default_term_code(session)
        if term is None:
            raise web.HTTPNotFound(text="no terms loaded")
        series = await analytics.course_fill_curves(session, subject, number, term)
        peaks = await analytics.course_term_peaks(session, subject, number)

    if not series:
        raise web.HTTPNotFound(text=f"no data for {subject} {number} in {term}")

    return web.Response(
        text=render_course_detail(subject, number, term, series, peaks, demo=_demo(request)),
        content_type="text/html",
    )


def render_course_detail(
    subject: str,
    number: str,
    term: str,
    series: list[analytics.SectionSeries],
    peaks: list[analytics.TermPeak],
    *,
    demo: bool = False,
) -> str:
    """A course page. Shared by the live server and the static renderer."""
    body = [_fill_curve_card(series, term), _sections_table_card(series)]
    if peaks:
        body.append(_term_history_card(peaks))
    return render.page(
        f"{subject} {number}",
        "".join(body),
        subtitle=f"{subject} {number} — enrollment over time in {term}.",
        demo=demo,
    )


def _fill_curve_card(series: list[analytics.SectionSeries], term: str) -> str:
    plotted = [s for s in series if len(s.points) >= 2][:MAX_PLOTTED_SECTIONS]
    if not plotted:
        return render.card(
            "Enrollment over time",
            charts.empty_state(
                "Not enough observations to plot yet — a curve needs at least two points, "
                "and a point is only recorded when the numbers actually move."
            ),
        )

    chart_series = [
        Series(
            label=f"{s.section_label} ({s.capacity} seats)",
            slot=i + 1,  # fixed by section order, never by rank
            points=tuple(Point(x=p.at.timestamp(), y=p.fill_pct) for p in s.points),
            value_label=s.section_label,
        )
        for i, s in enumerate(plotted)
    ]

    starts = [s.points[0].at for s in plotted]
    ends = [s.points[-1].at for s in plotted]
    x_labels = charts.time_axis_labels(min(starts), max(ends))

    note = ""
    if len(series) > len(plotted):
        note = (
            f" Showing {len(plotted)} of {len(series)} sections; the rest are in the table below."
        )

    return render.card(
        "Enrollment over time",
        charts.line_chart(
            chart_series,
            y_max=max(100.0, max(s.peak_fill_pct for s in plotted) * 1.05),
            y_suffix="%",
            x_labels=x_labels,
            reference=(100.0, "capacity"),
            title=f"Percent of capacity filled, {term}",
        )
        + charts.legend(chart_series),
        hint="Percent of capacity filled." + note,
    )


def _sections_table_card(series: list[analytics.SectionSeries]) -> str:
    rows = []
    for s in series:
        latest = s.points[-1] if s.points else None
        rows.append(
            [
                esc(s.section_label),
                esc(", ".join(s.instructors) or "Staff"),
                render.status_pill(s.status, analytics.status_tone(s.status)),
                f"{latest.enrolled:,}" if latest else "—",
                f"{s.capacity:,}",
                charts.meter(latest.fill_pct / 100 if latest else 0.0),
                f"{len(s.points):,}",
            ]
        )
    return render.card(
        "Sections",
        render.table_view(
            ["Section", "Instructors", "Status", "Enrolled", "Capacity", "Fill", "Points"],
            rows,
            numeric={3, 4, 6},
        ),
    )


def _term_history_card(peaks: list[analytics.TermPeak]) -> str:
    if len(peaks) < 2:
        return render.card(
            "Term over term",
            charts.empty_state(
                "Only one term of history so far. This chart compares a course's peak "
                "demand across terms, so it needs a second term before it says anything."
            ),
        )
    bars = [
        Bar(label=p.term_code, value=p.peak_fill_pct, display=f"{p.peak_fill_pct:.0f}%")
        for p in peaks
    ]
    rows = [
        [
            esc(p.term_code),
            esc(p.term_name),
            f"{p.peak_enrolled:,}",
            f"{p.capacity:,}",
            f"{p.peak_waitlisted:,}",
            f"{p.peak_fill_pct:.0f}%",
        ]
        for p in peaks
    ]
    return render.card(
        "Term over term",
        charts.bar_chart(bars, y_max=100, reference=(100.0, "capacity"))
        + render.collapsible_table(
            "Show the numbers",
            ["Term", "Name", "Peak enrolled", "Capacity", "Peak waitlist", "Peak fill"],
            rows,
            numeric={2, 3, 4, 5},
        ),
        hint="Highest fill this course reached in each term we have history for.",
    )


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------


def jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        out = {f.name: jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
        # Derived properties are the interesting part; include them too.
        for name in dir(type(value)):
            if isinstance(getattr(type(value), name, None), property):
                out[name] = jsonable(getattr(value, name))
        return out
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value


async def api_summary(request: web.Request) -> web.Response:
    sessions: async_sessionmaker[AsyncSession] = request.app["sessions"]
    async with sessions() as session:
        term = request.query.get("term") or await repo.default_term_code(session)
        payload: dict[str, Any] = {
            "term": term,
            "summary": jsonable(await analytics.summary(session)),
        }
        if term:
            payload["status_breakdown"] = jsonable(await analytics.status_breakdown(session, term))
            payload["most_in_demand"] = jsonable(await analytics.most_in_demand(session, term))
            payload["subject_pressure"] = jsonable(await analytics.subject_pressure(session, term))
    return web.json_response(payload)


async def api_course(request: web.Request) -> web.Response:
    subject = request.match_info["subject"].upper()
    number = request.match_info["number"].upper()
    sessions: async_sessionmaker[AsyncSession] = request.app["sessions"]
    async with sessions() as session:
        term = request.query.get("term") or await repo.default_term_code(session)
        if term is None:
            raise web.HTTPNotFound(text="no terms loaded")
        series = await analytics.course_fill_curves(session, subject, number, term)
        if not series:
            raise web.HTTPNotFound(text=f"no data for {subject} {number} in {term}")
        peaks = await analytics.course_term_peaks(session, subject, number)
    return web.json_response(
        {
            "subject_area_code": subject,
            "course_number": number,
            "term": term,
            "sections": jsonable(series),
            "term_peaks": jsonable(peaks),
        }
    )


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def _demo(request: web.Request) -> bool:
    """Whether this app is serving synthetic data."""
    return bool(request.app.get("demo"))


def add_routes(
    app: web.Application,
    sessions: async_sessionmaker[AsyncSession],
    *,
    demo: bool = False,
) -> None:
    """Mount the stats site onto an existing aiohttp app.

    ``demo`` marks every page with a banner saying the data is synthetic.
    """
    app["sessions"] = sessions
    app["demo"] = demo
    app.router.add_get("/stats", stats_overview)
    app.router.add_get("/stats/courses", course_index)
    app.router.add_get("/stats/course/{subject}/{number}", course_detail)
    app.router.add_get("/api/stats/summary", api_summary)
    app.router.add_get("/api/stats/course/{subject}/{number}", api_course)


def build_standalone_app(
    sessions: async_sessionmaker[AsyncSession], *, demo: bool = False
) -> web.Application:
    """The stats site with no bot behind it.

    Used by ``bruinwatch-web``. The health check reports the only thing a
    read-only site can be unhealthy about -- whether it can reach the database
    -- rather than gateway state that does not exist here.
    """
    app = web.Application()
    add_routes(app, sessions, demo=demo)

    async def healthz(_: web.Request) -> web.Response:
        from sqlalchemy import text

        try:
            async with sessions() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            return web.json_response({"ok": False, "error": repr(exc)}, status=503)
        return web.json_response({"ok": True})

    async def index(_: web.Request) -> web.Response:
        raise web.HTTPFound("/stats")

    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", index)
    return app


def build_app(bot: BruinWatchBot) -> web.Application:
    """The full site: health check plus stats."""
    from ..health import add_health_routes

    app = web.Application()
    add_health_routes(app, bot)
    add_routes(app, bot.sessions)
    return app
