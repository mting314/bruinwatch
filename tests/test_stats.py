"""Analytics queries and the stats routes, against a real PostgreSQL.

Uses the same database fixture as the sync tests -- PGlite by default, so no
Docker. See ``tests/postgres.py``.
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bruinwatch import analytics
from bruinwatch.db import models as m
from bruinwatch.web.app import add_routes

NOW = dt.datetime(2026, 9, 1, 12, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def seeded(sessions: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    """Two courses in one term, with enough history to exercise every query.

    COM SCI 32 is over-subscribed (full lecture plus a waitlist); MATH 31A is
    comfortably open. That contrast is what the demand and pressure queries are
    supposed to surface.
    """
    async with sessions() as s:
        term = m.Term(code="26F", name="Fall 2026", position=0, is_current=True, is_active=True)
        older = m.Term(code="26S", name="Spring 2026", position=3, is_active=False)
        cs = m.SubjectArea(code="COM SCI", name="Computer Science")
        math = m.SubjectArea(code="MATH", name="Mathematics")
        s.add_all([term, older, cs, math])
        await s.flush()

        cs32 = m.Course(subject_area_id=cs.id, number="32", title="Intro to CS II")
        m31a = m.Course(subject_area_id=math.id, number="31A", title="Differential Calculus")
        s.add_all([cs32, m31a])
        await s.flush()

        # Current state.
        full = m.Section(
            registrar_id="1001",
            term_id=term.id,
            course_id=cs32.id,
            section_label="Lec 1",
            index=1,
            enrollment_status="Full",
            enrollment_count=200,
            enrollment_capacity=200,
            waitlist_status="Open",
            waitlist_count=60,
            waitlist_capacity=80,
            instructors=["Huang, B.K."],
        )
        second = m.Section(
            registrar_id="1002",
            term_id=term.id,
            course_id=cs32.id,
            section_label="Lec 2",
            index=2,
            enrollment_status="Waitlist",
            enrollment_count=100,
            enrollment_capacity=100,
            waitlist_status="Open",
            waitlist_count=10,
            waitlist_capacity=30,
            instructors=["Smallberg, D."],
        )
        open_one = m.Section(
            registrar_id="2001",
            term_id=term.id,
            course_id=m31a.id,
            section_label="Lec 1",
            index=1,
            enrollment_status="Open",
            enrollment_count=50,
            enrollment_capacity=200,
            waitlist_status="None",
            waitlist_count=0,
            waitlist_capacity=0,
            instructors=["Higgins, V."],
        )
        # A section in an older term, so cross-term comparison has two points.
        historical = m.Section(
            registrar_id="0900",
            term_id=older.id,
            course_id=cs32.id,
            section_label="Lec 1",
            index=1,
            enrollment_status="Full",
            enrollment_count=150,
            enrollment_capacity=150,
            waitlist_status="None",
        )
        s.add_all([full, second, open_one, historical])
        await s.flush()

        def obs(section, hours, count, capacity, status, waitlisted=0):
            return m.EnrollmentDatum(
                section_id=section.id,
                enrollment_status=status,
                enrollment_count=count,
                enrollment_capacity=capacity,
                waitlist_status="Open",
                waitlist_count=waitlisted,
                waitlist_capacity=80,
                created_at=NOW + dt.timedelta(hours=hours),
            )

        s.add_all(
            [
                # Fills over six hours, then goes Full -- the fill-speed query.
                obs(full, 0, 120, 200, "Open"),
                obs(full, 2, 170, 200, "Open"),
                obs(full, 6, 200, 200, "Full", waitlisted=20),
                obs(full, 10, 200, 200, "Full", waitlisted=60),
                obs(second, 0, 80, 100, "Open"),
                obs(second, 5, 100, 100, "Waitlist", waitlisted=10),
                obs(open_one, 0, 30, 200, "Open"),
                obs(open_one, 8, 50, 200, "Open"),
                obs(historical, 0, 150, 150, "Full"),
            ]
        )
        await s.commit()
        return {"term_id": term.id, "cs32": cs32.id}


# -- analytics -------------------------------------------------------------


async def test_summary_counts_and_window(sessions, seeded):
    async with sessions() as s:
        summary = await analytics.summary(s)
    assert summary.terms == 2
    assert summary.courses == 2
    assert summary.sections == 4
    assert summary.observations == 9
    assert summary.has_history
    # First to last observation spans ten hours.
    assert summary.days_of_history == pytest.approx(10 / 24, abs=0.01)


async def test_summary_on_an_empty_database(sessions):
    async with sessions() as s:
        summary = await analytics.summary(s)
    assert summary.observations == 0
    assert summary.has_history is False
    assert summary.days_of_history == 0.0


async def test_status_breakdown(sessions, seeded):
    async with sessions() as s:
        counts = {c.status: c.sections for c in await analytics.status_breakdown(s, "26F")}
    assert counts == {"Full": 1, "Waitlist": 1, "Open": 1}


async def test_most_in_demand_ranks_by_unmet_demand(sessions, seeded):
    async with sessions() as s:
        demand = await analytics.most_in_demand(s, "26F")

    assert [d.label for d in demand] == ["COM SCI 32", "MATH 31A"]
    cs32 = demand[0]
    # Sections aggregate: 300 enrolled + 70 waitlisted over 300 seats.
    assert cs32.sections == 2
    assert cs32.enrolled == 300
    assert cs32.capacity == 300
    assert cs32.waitlisted == 70
    assert cs32.demand_ratio == pytest.approx(370 / 300)
    # Fill is capped at 1.0 even when over-enrolled.
    assert cs32.fill_ratio == 1.0
    assert demand[1].demand_ratio == pytest.approx(0.25)


async def test_most_in_demand_skips_tiny_sections(sessions, seeded):
    """A two-seat seminar makes the ratio meaningless, so it is filtered out."""
    async with sessions() as s:
        assert await analytics.most_in_demand(s, "26F", min_capacity=400) == []


async def test_subject_pressure(sessions, seeded):
    async with sessions() as s:
        pressure = {
            p.subject_area_code: p
            for p in await analytics.subject_pressure(s, "26F", min_sections=1)
        }
    assert pressure["COM SCI"].closed_sections == 2
    assert pressure["COM SCI"].closed_share == 1.0
    assert pressure["MATH"].closed_share == 0.0


async def test_fastest_filling_measures_from_first_observation(sessions, seeded):
    async with sessions() as s:
        speed = await analytics.fastest_filling(s, "26F")

    by_label = {f.label: f for f in speed}
    # COM SCI 32 Lec 1 was first seen at hour 0 and hit Full at hour 6.
    assert by_label["COM SCI 32 Lec 1"].hours_to_full == pytest.approx(6.0)
    assert by_label["COM SCI 32 Lec 2"].hours_to_full == pytest.approx(5.0)
    # The never-full section is absent rather than reported as instant.
    assert "MATH 31A Lec 1" not in by_label
    # Ranked soonest-first.
    assert next(f.label for f in speed) == "COM SCI 32 Lec 2"


async def test_course_fill_curves(sessions, seeded):
    async with sessions() as s:
        series = await analytics.course_fill_curves(s, "COM SCI", "32", "26F")

    assert [x.section_label for x in series] == ["Lec 1", "Lec 2"]
    lec1 = series[0]
    assert len(lec1.points) == 4
    assert lec1.instructors == ("Huang, B.K.",)
    assert lec1.points[0].fill_pct == pytest.approx(60.0)
    assert lec1.peak_fill_pct == pytest.approx(100.0)
    # Chronological, so the curve reads left to right.
    assert [p.at for p in lec1.points] == sorted(p.at for p in lec1.points)


async def test_course_fill_curves_unknown_course(sessions, seeded):
    async with sessions() as s:
        assert await analytics.course_fill_curves(s, "COM SCI", "999", "26F") == []


async def test_course_term_peaks_are_oldest_first(sessions, seeded):
    async with sessions() as s:
        peaks = await analytics.course_term_peaks(s, "COM SCI", "32")
    # Ordered by term position descending == chronological, so a chart reads
    # left to right through time.
    assert [p.term_code for p in peaks] == ["26S", "26F"]
    assert peaks[0].peak_fill_pct == pytest.approx(100.0)
    assert peaks[1].peak_enrolled == 200


async def test_tracked_courses(sessions, seeded):
    async with sessions() as s:
        tracked = await analytics.tracked_courses(s, "26F")
    assert {(c[0], c[1]) for c in tracked} == {("COM SCI", "32"), ("MATH", "31A")}


@pytest.mark.parametrize(
    ("status", "tone"),
    [("Open", "good"), ("Waitlist", "warning"), ("Full", "critical"), ("???", "neutral")],
)
def test_status_tone(status, tone):
    assert analytics.status_tone(status) == tone


# -- routes ----------------------------------------------------------------


@pytest_asyncio.fixture
async def client(sessions, aiohttp_client):
    app = web.Application()
    add_routes(app, sessions)
    return await aiohttp_client(app)


async def test_overview_renders(client, seeded):
    resp = await client.get("/stats")
    assert resp.status == 200
    html = await resp.text()
    assert "COM SCI 32" in html
    assert "Most in demand" in html
    # The hero: two of three sections are not open.
    assert '<div class="hero">67%</div>' in html
    # Every chart ships its table twin.
    assert "Show the numbers" in html


async def test_overview_on_empty_database_explains_itself(sessions, aiohttp_client):
    app = web.Application()
    add_routes(app, sessions)
    client = await aiohttp_client(app)
    resp = await client.get("/stats")
    assert resp.status == 200
    html = await resp.text()
    assert "No terms loaded yet" in html or "No enrollment history yet" in html


async def test_course_detail_renders_curves(client, seeded):
    resp = await client.get("/stats/course/COM%20SCI/32?term=26F")
    assert resp.status == 200
    html = await resp.text()
    assert "Enrollment over time" in html
    assert "Term over term" in html
    assert "Huang, B.K." in html
    # Two sections plotted -> a legend is mandatory.
    assert 'class="legend"' in html


async def test_course_detail_is_case_insensitive(client, seeded):
    assert (await client.get("/stats/course/com%20sci/32?term=26F")).status == 200


async def test_course_detail_unknown_is_404(client, seeded):
    assert (await client.get("/stats/course/COM%20SCI/999?term=26F")).status == 404


async def test_course_index(client, seeded):
    resp = await client.get("/stats/courses")
    assert resp.status == 200
    assert "MATH 31A" in await resp.text()


async def test_api_summary(client, seeded):
    resp = await client.get("/api/stats/summary")
    assert resp.status == 200
    payload = await resp.json()
    assert payload["term"] == "26F"
    assert payload["summary"]["observations"] == 9
    assert payload["most_in_demand"][0]["label"] == "COM SCI 32"
    # Derived properties are serialised, not just stored columns.
    assert payload["most_in_demand"][0]["demand_ratio"] == pytest.approx(370 / 300)


async def test_api_course(client, seeded):
    resp = await client.get("/api/stats/course/COM%20SCI/32?term=26F")
    assert resp.status == 200
    payload = await resp.json()
    assert len(payload["sections"]) == 2
    assert payload["sections"][0]["points"][0]["fill_pct"] == pytest.approx(60.0)
    assert [p["term_code"] for p in payload["term_peaks"]] == ["26S", "26F"]


async def test_api_course_unknown_is_404(client, seeded):
    assert (await client.get("/api/stats/course/NOPE/1?term=26F")).status == 404


async def test_scraped_titles_cannot_inject_markup(sessions, aiohttp_client, seeded):
    """Course titles come from scraped HTML and are rendered into the page."""
    async with sessions() as s:
        course = await s.get(m.Course, seeded["cs32"])
        course.title = '<img src=x onerror="alert(1)">'
        await s.commit()

    app = web.Application()
    add_routes(app, sessions)
    client = await aiohttp_client(app)
    html = await (await client.get("/stats")).text()
    # The payload must survive only as inert text: no real tag, and the quotes
    # that would close an attribute are entity-encoded. ("onerror=" itself still
    # appears as literal text, which is harmless.)
    assert "<img" not in html
    assert '"alert(1)"' not in html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in html


# -- demo mode -------------------------------------------------------------


@pytest_asyncio.fixture
async def demo_client(sessions, aiohttp_client):
    app = web.Application()
    add_routes(app, sessions, demo=True)
    return await aiohttp_client(app)


async def test_demo_mode_banners_every_page(demo_client, seeded):
    """Synthetic data must never render as though it were real."""
    for url in ("/stats", "/stats/courses", "/stats/course/COM%20SCI/32?term=26F"):
        html = await (await demo_client.get(url)).text()
        assert "DEMO DATA" in html, f"{url} is missing the demo banner"
        assert "<title>[DEMO]" in html


async def test_real_mode_has_no_banner(client, seeded):
    html = await (await client.get("/stats")).text()
    assert "DEMO DATA" not in html
    assert "<title>[DEMO]" not in html
    # The .demo-banner CSS rule ships on every page; what must be absent is the
    # element itself.
    assert '<div class="demo-banner"' not in html


def test_demo_instructors_are_not_real_people():
    """A demo screenshot must not appear to claim a named professor teaches a
    course they do not."""
    from bruinwatch.scripts.demo import INSTRUCTORS

    assert all(name.startswith("Instructor ") for name in INSTRUCTORS)


# -- standalone site (no Discord bot) --------------------------------------


@pytest_asyncio.fixture
async def standalone_client(sessions, aiohttp_client):
    from bruinwatch.web.app import build_standalone_app

    return await aiohttp_client(build_standalone_app(sessions))


async def test_standalone_serves_the_stats_site(standalone_client, seeded):
    """The site must work with no bot behind it -- that is what lets it scale
    to zero, since only the Discord gateway forces an always-on process."""
    resp = await standalone_client.get("/stats")
    assert resp.status == 200
    assert "COM SCI 32" in await resp.text()


async def test_standalone_healthz_reports_database_reachability(standalone_client, seeded):
    resp = await standalone_client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"ok": True}


async def test_standalone_healthz_fails_when_the_database_is_gone(aiohttp_client):
    """A read-only site's only real health question."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from bruinwatch.db.session import create_session_factory
    from bruinwatch.web.app import build_standalone_app

    dead = create_session_factory(
        create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/nothing")
    )
    client = await aiohttp_client(build_standalone_app(dead))
    resp = await client.get("/healthz")
    assert resp.status == 503
    assert (await resp.json())["ok"] is False


async def test_standalone_root_redirects_to_stats(standalone_client, seeded):
    resp = await standalone_client.get("/", allow_redirects=False)
    assert resp.status == 302
    assert resp.headers["Location"] == "/stats"


async def test_standalone_api_works(standalone_client, seeded):
    resp = await standalone_client.get("/api/stats/summary")
    assert resp.status == 200
    assert (await resp.json())["term"] == "26F"
