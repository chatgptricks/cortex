"""Dedicated Render worker entry point for ingestion and maintenance jobs.

The public API must never share its process with Apify polling, large imports,
or OCR calls. Run this module only in Render's background-worker service.
"""
from __future__ import annotations

import logging
import signal
import threading

from .db import init_db
from .scheduler import start_scheduler, stop_scheduler


def main() -> None:
    # Keep the worker compatible with Postgres schema additions just like the
    # web service, without booting FastAPI or opening a public listener.
    init_db()
    stop_requested = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        logging.getLogger("uvicorn.error").info("Scheduler worker received shutdown signal")
        stop_requested.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    start_scheduler()
    try:
        stop_requested.wait()
    finally:
        stop_scheduler()


if __name__ == "__main__":
    main()
