"""Static rendering.

The point of the static build is that it can be published to a dumb file host,
so the tests check the thing that actually breaks there: links that resolve to
nothing because a query string or a path parameter survived into the output.
"""

from __future__ import annotations

import json
import re

import pytest
import pytest_asyncio

from bruinwatch.db import models as m
from bruinwatch.scripts.render import NoTermsError, render_site
from bruinwatch.web import links
from bruinwatch.web.links import UrlStyle


@pytest_asyncio.fixture
async def populated(sessions):
    """One term, two courses, a little history."""
    import datetime as dt

    now = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    async with sessions() as s:
        term = m.Term(code="26F", name="Fall 2026", position=1, is_current=True, is_active=True)
        cs = m.SubjectArea(code="COM SCI", name="Computer Science")
        amp = m.SubjectArea(code="C&S BIO", name="Computational and Systems Biology")
        s.add_all([term, cs, amp])
        await s.flush()
        c1 = m.Course(subject_area_id=cs.id, number="32", title="Intro to CS II")
        c2 = m.Course(subject_area_id=amp.id, number="M120", title="Systems Biology")
        s.add_all([c1, c2])
        await s.flush()
        for i, course in enumerate((c1, c2)):
            sec = m.Section(
                registrar_id=f"100{i}",
                term_id=term.id,
                course_id=course.id,
                section_label="Lec 1",
                index=1,
                enrollment_status="Open",
                enrollment_count=100,
                enrollment_capacity=200,
                waitlist_status="None",
                instructors=["Instructor A."],
            )
            s.add(sec)
            await s.flush()
            for h in (0, 4, 8):
                s.add(
                    m.EnrollmentDatum(
                        section_id=sec.id,
                        enrollment_status="Open",
                        enrollment_count=100 + h,
                        enrollment_capacity=200,
                        waitlist_status="None",
                        waitlist_count=0,
                        waitlist_capacity=0,
                        created_at=now + dt.timedelta(hours=h),
                    )
                )
        await s.commit()


async def test_renders_the_expected_files(sessions, populated, tmp_path):
    written = await render_site(sessions, tmp_path)
    assert written.pages > 0

    assert (tmp_path / "index.html").exists()
    assert (tmp_path / "courses" / "index.html").exists()
    assert (tmp_path / "api" / "summary.json").exists()
    # .nojekyll matters: without it GitHub Pages hides paths beginning with _.
    assert (tmp_path / ".nojekyll").exists()


async def test_subject_codes_with_punctuation_become_safe_paths(sessions, populated, tmp_path):
    """``C&S BIO`` must not put an ampersand in a URL path."""
    await render_site(sessions, tmp_path)
    assert (tmp_path / "course" / "c-s-bio" / "m120" / "index.html").exists()
    assert (tmp_path / "course" / "com-sci" / "32" / "index.html").exists()


async def test_every_internal_link_resolves_to_a_file(sessions, populated, tmp_path):
    """The failure mode of a static build: a link that 404s on a file host."""
    await render_site(sessions, tmp_path)

    missing = []
    for page in tmp_path.rglob("*.html"):
        html = page.read_text()
        for href in re.findall(r'href="([^"]+)"', html):
            if href.startswith(("http://", "https://", "#", "mailto:")):
                continue
            target = tmp_path / links.static_path(href.split("?", 1)[0])
            if not target.exists():
                missing.append((page.relative_to(tmp_path).as_posix(), href))
    assert not missing, f"dangling internal links: {missing[:5]}"


async def test_no_dynamic_urls_leak_into_static_output(sessions, populated, tmp_path):
    """A query string or /stats/ path in the output means the URL style leaked."""
    await render_site(sessions, tmp_path)
    for page in tmp_path.rglob("*.html"):
        html = page.read_text()
        assert 'href="/stats' not in html, f"{page.name} has a dynamic /stats link"
        assert "?term=" not in html, f"{page.name} has a query-string link"


async def test_api_json_is_valid_and_matches_the_pages(sessions, populated, tmp_path):
    await render_site(sessions, tmp_path)
    summary = json.loads((tmp_path / "api" / "summary.json").read_text())
    assert summary["term"] == "26F"
    assert summary["summary"]["observations"] == 6

    course = json.loads((tmp_path / "api" / "course" / "com-sci" / "32.json").read_text())
    assert course["course_number"] == "32"
    assert len(course["sections"]) == 1


async def test_max_courses_is_reported_not_silently_applied(sessions, populated, tmp_path):
    written = await render_site(sessions, tmp_path, max_courses=1)
    assert written.truncated == 1, "a dropped course must be counted"


async def test_empty_database_raises_rather_than_writing_a_broken_site(sessions, tmp_path):
    with pytest.raises(NoTermsError):
        await render_site(sessions, tmp_path)
    assert not list(tmp_path.rglob("*.html"))


def test_url_style_is_restored_after_rendering():
    """Rendering must not leave the process in static mode -- a live server
    sharing the interpreter would start emitting file paths."""
    assert links.current_style() is UrlStyle.DYNAMIC
    with links.use_style(UrlStyle.STATIC):
        assert links.current_style() is UrlStyle.STATIC
    assert links.current_style() is UrlStyle.DYNAMIC


async def test_rendering_does_not_leak_style_to_the_live_server(sessions, populated, tmp_path):
    await render_site(sessions, tmp_path)
    assert links.current_style() is UrlStyle.DYNAMIC
    assert links.course("COM SCI", "32", "26F") == "/stats/course/COM%20SCI/32?term=26F"
