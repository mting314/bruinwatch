"""structlog setup, bridged so discord.py's stdlib logging matches.

structlog emits *through* stdlib logging rather than printing directly. That is
what makes the bridge real: our structured events and discord.py's plain
`logging` calls go through one handler, one format and one level, instead of
two pipelines racing for stderr.

It is also required for correctness. ``structlog.stdlib.add_logger_name`` reads
``logger.name``, which a ``PrintLogger`` does not have -- pairing the two raises
``AttributeError`` on the very first log line, which took down every CLI.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure(level: str = "INFO", *, json_output: bool = False) -> None:
    numeric = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    renderer: structlog.types.Processor = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            # Hands the event dict to the stdlib handler configured below.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=renderer,
            # Applied to records from libraries that never went through
            # structlog, so discord.py's output is shaped like ours.
            foreign_pre_chain=[
                structlog.stdlib.add_log_level,
                structlog.stdlib.add_logger_name,
                structlog.processors.TimeStamper(fmt="iso", utc=True),
            ],
        )
    )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric)

    # discord.py is chatty about gateway keepalives at INFO.
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)
