"""Hit the live registrar and print what we parse. No database, no Discord.

The fastest way to tell whether the registrar changed its HTML out from under
us, and the check that ``build_model`` is still producing blobs the endpoint
accepts::

    uv run python -m bruinwatch.scripts.probe "COM SCI" 32 --term 26F
    uv run python -m bruinwatch.scripts.probe MATH 31A --term 26F --json
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys

from ..registrar.client import RegistrarClient
from ..registrar.model import build_model
from ..registrar.parsing import parse_course_summary


async def probe(subject: str, number: str, term: str, as_json: bool) -> int:
    model = build_model(subject, number, term)
    async with RegistrarClient(user_agent="bruinwatch-probe (+local development)") as client:
        markup = await client.get_course_summary(model)

    sections = parse_course_summary(markup, term, subject.upper(), number.upper())

    if as_json:
        json.dump([dataclasses.asdict(s) for s in sections], sys.stdout, indent=2, default=str)
        print()
        return 0 if sections else 1

    print(f"model  {model}")
    print(f"bytes  {len(markup):,}")
    print(f"found  {len(sections)} section(s)\n")
    if not sections:
        print("No sections parsed. Either the course isn't offered this term, or")
        print("the registrar changed its markup. Compare against:")
        print(
            f"  https://sa.ucla.edu/ro/Public/SOC/Results"
            f"?t={term}&sBy=subject&subj={subject.replace(' ', '+')}"
        )
        return 1

    for section in sections:
        e = section.enrollment
        print(f"  {section.section_label:<10} id={section.registrar_id}")
        print(f"    status      {e.enrollment_status} {e.enrollment_count}/{e.enrollment_capacity}")
        print(f"    waitlist    {e.waitlist_status} {e.waitlist_count}/{e.waitlist_capacity}")
        print(
            f"    days/times  {'/'.join(section.days) or 'N/A'}  {'/'.join(section.times) or 'N/A'}"
        )
        print(f"    locations   {', '.join(section.locations) or 'N/A'}")
        print(f"    instructors {', '.join(section.instructors) or 'N/A'}")
        print(f"    units       {section.units}")
        print(f"    url         {section.detail_url}\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="bruinwatch.scripts.probe", description=__doc__)
    parser.add_argument("subject", help='Subject area, e.g. "COM SCI"')
    parser.add_argument("number", help="Catalog number, e.g. 32 or M151B")
    parser.add_argument("--term", default="26F", help="Term code (default: %(default)s)")
    parser.add_argument("--json", action="store_true", help="Emit parsed sections as JSON")
    args = parser.parse_args()
    return asyncio.run(probe(args.subject, args.number, args.term, args.json))


if __name__ == "__main__":
    raise SystemExit(main())
