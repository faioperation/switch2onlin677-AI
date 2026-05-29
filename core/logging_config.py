"""
core/logging_config.py
======================
Structured JSON logging for production.

Usage
-----
    from core.logging_config import setup_logging
    setup_logging()          # call once at app startup

Then use standard logging anywhere:
    import logging
    logger = logging.getLogger(__name__)
    logger.info("upload started", extra={"job_id": "...", "filename": "..."})

JSON output (one line per record):
    {
      "ts":      "2026-05-30T06:00:00.123Z",
      "level":   "INFO",
      "logger":  "services.upload",
      "msg":     "upload started",
      "job_id":  "abc123",
      "filename": "products.xlsx"
    }

In development (LOG_FORMAT=text) human-readable output is used instead.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import traceback
from datetime import datetime, timezone


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record, newline-terminated."""

    RESERVED = frozenset(
        logging.LogRecord(
            name="", level=0, pathname="", lineno=0,
            msg="", args=(), exc_info=None,
        ).__dict__.keys()
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts":     datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }

        # Attach any extra= kwargs that are not standard LogRecord fields
        for key, val in record.__dict__.items():
            if key not in self.RESERVED and not key.startswith("_"):
                payload[key] = val

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str | None = None) -> None:
    """
    Configure root logger for the application.

    Parameters
    ----------
    level : str | None
        Override log level (DEBUG/INFO/WARNING/ERROR).
        Defaults to LOG_LEVEL env var, then INFO.

    Format selection
    ----------------
    LOG_FORMAT=json  (default in production) → compact JSON per line
    LOG_FORMAT=text  (useful in dev)         → classic human-readable format
    """
    log_level_str = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, log_level_str, logging.INFO)

    log_format = os.getenv("LOG_FORMAT", "json").lower()

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove any handlers the framework already attached
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(log_level)

    if log_format == "text":
        handler.setFormatter(
            logging.Formatter(
                fmt="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    else:
        handler.setFormatter(_JsonFormatter())

    root.addHandler(handler)

    # Silence noisy third-party loggers at WARNING unless DEBUG mode
    if log_level > logging.DEBUG:
        for noisy in ("urllib3", "httpx", "httpcore", "openai", "PIL"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
