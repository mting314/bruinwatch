"""Render the stats site to a directory of static files.

    uv run bruinwatch-render --out dist --term 26F

The pages are already pure functions of query results, so this walks the same
builders the live server uses and writes their output instead of serving it.
URLs switch to a static shape (directory-per-page, slugged segments, no query
strings) via :mod:`bruinwatch.web.links`.

Written for GitHub Pages: the scrape runs in CI against a throwaway Postgres,
this renders the result, and only the rendered output is published. Nothing has
to stay running and no database is hosted anywhere.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import pathlib
import sys
import time

import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import analytics
from .. import logging as log_setup
from ..config import Settings
from ..db import repo
from ..db.session import create_engine, create_session_factory, wait_for_database
from ..web import app as webapp
from ..web import links
from ..web.links import UrlStyle

log = structlog.get_logger("render")

#: Courses to pre-render. Every page is ~10 KB, and GitHub Pages caps a site at
#: 1 GB, so the whole catalog (10,400 courses/term, ~137 MB) fits -- but only
#: courses with recorded history have anything to show, so that is the default
#: scope. Anything dropped is reported rather than silently truncated.
DEFAULT_MAX_COURSES = 5000


class NoTermsError(RuntimeError):
    """Nothing has been scraped yet, so there is nothing to render."""


@dataclasses.dataclass
class Written:
    pages: int = 0
    bytes_: int = 0
    truncated: int = 0

    def add(self, path: pathlib.Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.pages += 1
        self.bytes_ += len(content.encode("utf-8"))


async def render_site(
    sessions: async_sessionmaker[AsyncSession],
    out: pathlib.Path,
    term: str | None = None,
    max_courses: int = DEFAULT_MAX_COURSES,
) -> Written:
    """Write the whole site to ``out``. Takes a session factory so it can be
    tested directly, without the CLI's engine setup."""
    written = Written()
    truncated = 0

    if True:
        async with sessions() as session:
            resolved = term or await repo.default_term_code(session)
            if resolved is None:
                raise NoTermsError(
                    "No terms in the database. Run `bruinwatch-scrape bootstrap` first."
                )

            summary = await analytics.summary(session)
            statuses = await analytics.status_breakdown(session, resolved)
            demand = await analytics.most_in_demand(session, resolved)
            pressure = await analytics.subject_pressure(session, resolved)
            speed = await analytics.fastest_filling(session, resolved)
            tracked = await analytics.tracked_courses(session, resolved, limit=max_courses + 1)

            truncated = max(0, len(tracked) - max_courses)
            tracked = tracked[:max_courses]

            # Static mode for everything below, so links point at files.
            with links.use_style(UrlStyle.STATIC):
                written.add(
                    out / "index.html",
                    webapp.render_overview(
                        resolved, summary, statuses, demand, pressure, speed, demo=False
                    ),
                )
                written.add(
                    out / "courses" / "index.html",
                    webapp.render_course_index(resolved, tracked, demo=False),
                )
                written.add(
                    out / "api" / "summary.json",
                    json.dumps(
                        {
                            "term": resolved,
                            "summary": webapp.jsonable(summary),
                            "status_breakdown": webapp.jsonable(statuses),
                            "most_in_demand": webapp.jsonable(demand),
                            "subject_pressure": webapp.jsonable(pressure),
                        },
                        indent=1,
                    ),
                )

                for subject_code, number, _title in tracked:
                    series = await analytics.course_fill_curves(
                        session, subject_code, number, resolved
                    )
                    if not series:
                        continue
                    peaks = await analytics.course_term_peaks(session, subject_code, number)
                    written.add(
                        out / links.static_path(links.course(subject_code, number)),
                        webapp.render_course_detail(
                            subject_code, number, resolved, series, peaks, demo=False
                        ),
                    )
                    written.add(
                        out / links.static_path(links.api_course(subject_code, number)),
                        json.dumps(
                            {
                                "subject_area_code": subject_code,
                                "course_number": number,
                                "term": resolved,
                                "sections": webapp.jsonable(series),
                                "term_peaks": webapp.jsonable(peaks),
                            },
                            indent=1,
                        ),
                    )

            # Tell static hosts not to run Jekyll over the output.
            written.add(out / ".nojekyll", "")

    written.truncated = truncated
    return written


async def build(out: pathlib.Path, term: str | None, max_courses: int) -> int:
    """CLI wrapper: settings, engine, then :func:`render_site`."""
    settings = Settings()
    log_setup.configure(settings.log_level, json_output=settings.log_json)

    engine = create_engine(
        settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        single_connection=settings.db_single_connection,
    )
    started = time.monotonic()
    try:
        await wait_for_database(engine)
        written = await render_site(create_session_factory(engine), out, term, max_courses)
    except NoTermsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        await engine.dispose()

    print(
        f"Rendered {written.pages:,} files ({written.bytes_ / 1048576:.1f} MB) "
        f"to {out} in {time.monotonic() - started:.1f}s"
    )
    if written.truncated:
        # Never let a cap look like complete coverage.
        print(
            f"NOTE: {written.truncated:,} courses with history were not rendered "
            f"(--max-courses={max_courses}).",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch-render", description=__doc__)
    parser.add_argument("--out", type=pathlib.Path, default=pathlib.Path("dist"))
    parser.add_argument("--term", default=None, help="Defaults to the registrar's current term")
    parser.add_argument("--max-courses", type=int, default=DEFAULT_MAX_COURSES)
    args = parser.parse_args()
    return asyncio.run(build(args.out, args.term, args.max_courses))


if __name__ == "__main__":
    raise SystemExit(main())
