"""The read-only stats site and JSON API."""

from .app import add_routes, build_app

__all__ = ["add_routes", "build_app"]
