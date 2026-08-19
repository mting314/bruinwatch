"""Run the stats site locally against synthetic data.

    uv run bruinwatch-demo

Needs no Discord token, no Postgres install and no scraped data: PGlite
supplies the database and this module supplies the numbers. It exists so the
site can be developed and reviewed before the scraper has collected anything
real -- which, given history accrues over a term, is most of the time.

**Everything it generates is fake**, and every page it serves says so in a
banner. The instructor names are deliberately non-people ("Instructor A") so a
demo screenshot can never be mistaken for a claim about a real professor.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import random
import sys

from aiohttp import web
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from ..db import models as m
from ..db.models import Base
from ..db.session import create_session_factory
from ..web.app import add_routes

#: Real UCLA subject codes and course titles, so the layout is exercised with
#: realistic string lengths. The *numbers* attached to them are invented.
CATALOG: dict[str, tuple[str, list[tuple[str, str, int, float]]]] = {
    "COM SCI": (
        "Computer Science",
        [
            ("31", "Introduction to Computer Science I", 320, 1.00),
            ("32", "Introduction to Computer Science II", 232, 1.00),
            ("33", "Introduction to Computer Organization", 240, 0.98),
            ("35L", "Software Construction", 120, 1.00),
            ("111", "Operating Systems Principles", 180, 0.94),
            ("180", "Introduction to Algorithms and Complexity", 200, 0.99),
            ("M151B", "Computer Systems Architecture", 150, 0.92),
        ],
    ),
    "MATH": (
        "Mathematics",
        [
            ("31A", "Differential and Integral Calculus", 180, 0.72),
            ("32A", "Calculus of Several Variables", 160, 0.66),
            ("33A", "Linear Algebra and Applications", 150, 0.81),
            ("61", "Introduction to Discrete Structures", 140, 0.90),
        ],
    ),
    "PSYCH": (
        "Psychology",
        [
            ("10", "Introductory Psychology", 400, 0.88),
            ("100A", "Psychological Statistics", 220, 0.79),
            ("110", "Cognitive Psychology", 180, 0.62),
        ],
    ),
    "ECON": (
        "Economics",
        [
            ("1", "Principles of Economics", 300, 0.95),
            ("11", "Microeconomic Theory", 240, 0.86),
            ("41", "Statistics for Economists", 200, 0.91),
        ],
    ),
    "CHEM": (
        "Chemistry and Biochemistry",
        [
            ("14A", "Atomic and Molecular Structure", 350, 0.93),
            ("20A", "Chemical Structure", 300, 0.87),
        ],
    ),
    "PHYSICS": (
        "Physics and Astronomy",
        [
            ("1A", "Physics for Scientists and Engineers", 280, 0.83),
            ("5A", "Physics for Life Sciences Majors", 240, 0.70),
        ],
    ),
    "ENGL": (
        "English",
        [
            ("4W", "Critical Reading and Writing", 18, 1.05),
            ("10A", "Literatures in English to 1700", 60, 0.55),
        ],
    ),
    "C&S BIO": (
        "Computational and Systems Biology",
        [("M120", "Systems Biology Modeling", 90, 0.77)],
    ),
}

#: Deliberately not real names. See the module docstring.
INSTRUCTORS = [f"Instructor {letter}." for letter in "ABCDEFGH"]

HISTORY_DAYS = 14
SAMPLES_PER_DAY = 6


def _status_for(fill: float) -> str:
    if fill >= 1.0:
        return "Full"
    if fill >= 0.98:
        return "Waitlist"
    return "Open"


async def seed(sessions: async_sessionmaker[AsyncSession], *, seed_value: int) -> dict[str, int]:
    """Populate a database with synthetic sections and fill curves."""
    rng = random.Random(seed_value)
    start = dt.datetime.now(dt.UTC) - dt.timedelta(days=HISTORY_DAYS)
    counts = {"courses": 0, "sections": 0, "observations": 0}

    async with sessions() as session:
        current = m.Term(code="26F", name="Fall 2026", position=0, is_current=True, is_active=True)
        prior = m.Term(code="26S", name="Spring 2026", position=3)
        session.add_all([current, prior])
        await session.flush()

        registrar_id = 187000000
        for code, (name, courses) in CATALOG.items():
            subject = m.SubjectArea(code=code, name=name)
            session.add(subject)
            await session.flush()

            for number, title, capacity, target in courses:
                course = m.Course(subject_area_id=subject.id, number=number, title=title)
                session.add(course)
                await session.flush()
                counts["courses"] += 1
                seats = max(10, capacity // 2)

                for lecture in range(1, rng.choice([2, 2, 3]) + 1):
                    registrar_id += 1
                    peak = min(1.08, target + rng.uniform(-0.08, 0.05))
                    waitlisted = max(0, int(seats * (peak - 1.0) * 3)) if peak > 1 else 0
                    section = m.Section(
                        registrar_id=str(registrar_id),
                        term_id=current.id,
                        course_id=course.id,
                        section_label=f"Lec {lecture}",
                        index=lecture,
                        format="Lec",
                        days=["M", "W", "F"],
                        times=["10am-10:50am"],
                        locations=["Boelter 3400"],
                        instructors=[rng.choice(INSTRUCTORS)],
                        units="4.0",
                        enrollment_status=_status_for(peak),
                        enrollment_count=int(seats * min(peak, 1.02)),
                        enrollment_capacity=seats,
                        waitlist_status="Open" if waitlisted else "None",
                        waitlist_count=waitlisted,
                        waitlist_capacity=40 if waitlisted else 0,
                    )
                    session.add(section)
                    await session.flush()
                    counts["sections"] += 1

                    # Ease toward the peak so the curves have a believable
                    # shape: fast early, flattening as the section fills.
                    steps = HISTORY_DAYS * SAMPLES_PER_DAY
                    for i in range(steps + 1):
                        progress = 1 - (1 - i / steps) ** 2.4
                        fill = max(
                            0.0,
                            0.32 * peak + 0.68 * peak * progress + rng.uniform(-0.006, 0.006),
                        )
                        session.add(
                            m.EnrollmentDatum(
                                section_id=section.id,
                                enrollment_status=_status_for(fill),
                                enrollment_count=min(int(seats * fill), int(seats * 1.02)),
                                enrollment_capacity=seats,
                                waitlist_status="Open" if fill > 1 else "None",
                                waitlist_count=max(0, int(seats * (fill - 1.0) * 3)),
                                waitlist_capacity=40,
                                created_at=start + dt.timedelta(hours=24 * i / SAMPLES_PER_DAY),
                            )
                        )
                        counts["observations"] += 1

                # One prior-term section per course, so the term-over-term
                # comparison has a second point to plot.
                registrar_id += 1
                previous = m.Section(
                    registrar_id=str(registrar_id),
                    term_id=prior.id,
                    course_id=course.id,
                    section_label="Lec 1",
                    index=1,
                    enrollment_status="Full",
                    enrollment_count=int(seats * 0.9),
                    enrollment_capacity=seats,
                    waitlist_status="None",
                    instructors=[rng.choice(INSTRUCTORS)],
                )
                session.add(previous)
                await session.flush()
                session.add(
                    m.EnrollmentDatum(
                        section_id=previous.id,
                        enrollment_status="Full",
                        enrollment_count=int(seats * 0.9),
                        enrollment_capacity=seats,
                        waitlist_status="None",
                        waitlist_count=0,
                        waitlist_capacity=0,
                        created_at=start - dt.timedelta(days=120),
                    )
                )
        await session.commit()
    return counts


async def _connect(url: str, connect_args: dict[str, object]) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(url, poolclass=StaticPool, connect_args=connect_args)
    last: Exception | None = None
    for _ in range(20):
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.drop_all)
                await conn.run_sync(Base.metadata.create_all)
            return create_session_factory(engine)
        except Exception as exc:
            last = exc
            await engine.dispose(close=False)
            await asyncio.sleep(0.5)
    raise RuntimeError(f"database never became ready: {last!r}")


async def _serve(url: str, connect_args: dict[str, object], port: int, seed_value: int) -> None:
    sessions = await _connect(url, connect_args)
    print("Seeding synthetic data ...", flush=True)
    counts = await seed(sessions, seed_value=seed_value)
    print(
        f"  {counts['courses']} courses, {counts['sections']} sections, "
        f"{counts['observations']:,} observations",
        flush=True,
    )

    app = web.Application()
    add_routes(app, sessions, demo=True)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()

    base = f"http://127.0.0.1:{port}"
    print(f"\n  overview  {base}/stats")
    print(f"  course    {base}/stats/course/COM%20SCI/32")
    print(f"  courses   {base}/stats/courses")
    print(f"  api       {base}/api/stats/summary")
    print("\nAll data is synthetic. Ctrl-C to stop.", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch-demo", description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres to seed. Defaults to a throwaway in-process PGlite. "
        "WARNING: the target database is dropped and recreated.",
    )
    parser.add_argument("--seed", type=int, default=20260819, help="RNG seed")
    args = parser.parse_args()

    if args.database_url:
        return asyncio.run(_serve(args.database_url, {}, args.port, args.seed)) or 0

    try:
        from py_pglite import PGliteConfig, PGliteManager
    except ImportError:
        print(
            "No --database-url given and py-pglite is not installed.\n"
            "Install the dev dependencies (uv sync) or pass --database-url.",
            file=sys.stderr,
        )
        return 2

    print("Starting PGlite (in-process PostgreSQL) ...", flush=True)
    with PGliteManager(PGliteConfig(use_tcp=True, tcp_port=args.port + 1, log_level="WARNING")):
        url = f"postgresql+asyncpg://postgres:postgres@127.0.0.1:{args.port + 1}/postgres"
        asyncio.run(_serve(url, {"ssl": False, "statement_cache_size": 0}, args.port, args.seed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
