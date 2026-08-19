"""URL generation, in two flavours.

The same pages are served two ways: live from aiohttp, and pre-rendered to a
directory of files for static hosting. Those need different URLs -- a static
host has no query strings and no path parameters -- so every internal link goes
through here rather than being written inline.

The mode is ambient rather than threaded through every function because it is a
pure presentation concern: the card builders assemble the same HTML either way.
A :class:`~contextvars.ContextVar` keeps that safe under concurrency, so a
render running alongside a live server cannot leak its mode into responses.
"""

from __future__ import annotations

import contextlib
import re
import urllib.parse
from collections.abc import Iterator
from contextvars import ContextVar
from enum import StrEnum


class UrlStyle(StrEnum):
    #: Live server: query strings and percent-encoded path segments.
    DYNAMIC = "dynamic"
    #: Static files: directory-per-page, slugged segments, no query strings.
    STATIC = "static"


_style: ContextVar[UrlStyle] = ContextVar("url_style", default=UrlStyle.DYNAMIC)

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(value: str) -> str:
    """URL-safe segment for a subject code. ``C&S BIO`` -> ``c-s-bio``.

    Verified collision-free across all 180 subject codes the registrar
    publishes; ``tests/test_links.py`` re-checks that against the fixture.
    """
    return _SLUG_RE.sub("-", value.lower()).strip("-")


@contextlib.contextmanager
def use_style(style: UrlStyle) -> Iterator[None]:
    token = _style.set(style)
    try:
        yield
    finally:
        _style.reset(token)


def current_style() -> UrlStyle:
    return _style.get()


def _static() -> bool:
    return _style.get() is UrlStyle.STATIC


# --------------------------------------------------------------------------
# The links themselves
# --------------------------------------------------------------------------


def overview(term: str | None = None) -> str:
    if _static():
        return "./" if term is None else f"./?term={term}"
    return "/stats" if term is None else f"/stats?term={urllib.parse.quote(term)}"


def course_index() -> str:
    return "/courses/" if _static() else "/stats/courses"


def course(subject_area_code: str, number: str, term: str | None = None) -> str:
    if _static():
        # Static output is one term deep; the term is implied by the build.
        return f"/course/{slug(subject_area_code)}/{slug(number)}/"
    path = f"/stats/course/{urllib.parse.quote(subject_area_code)}/{urllib.parse.quote(number)}"
    return f"{path}?term={urllib.parse.quote(term)}" if term else path


def api_summary() -> str:
    return "/api/summary.json" if _static() else "/api/stats/summary"


def api_course(subject_area_code: str, number: str) -> str:
    if _static():
        return f"/api/course/{slug(subject_area_code)}/{slug(number)}.json"
    return f"/api/stats/course/{urllib.parse.quote(subject_area_code)}/{urllib.parse.quote(number)}"


def static_path(url: str) -> str:
    """Filesystem path, relative to the output root, for a static URL."""
    trimmed = url.lstrip("/") or ""
    if trimmed.endswith("/") or trimmed == "":
        return f"{trimmed}index.html"
    return trimmed
