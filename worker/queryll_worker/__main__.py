"""Process entry point: `python -m queryll_worker`.

Deployed on Render as a **Background Worker**, not a Web Service — there is no port to bind
and no health check to answer, which is exactly the point of a process whose only interface
is rows in a table.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv

from queryll_worker.config import ConfigError, load_settings
from queryll_worker.logging_setup import configure_logging
from queryll_worker.runner import ShutdownSignal, install_signal_handlers, run_forever

logger = logging.getLogger("queryll_worker")


def main() -> int:
    load_dotenv()
    try:
        settings = load_settings()
    except ConfigError as exc:
        configure_logging("INFO")
        logger.critical("cannot start: %s", exc)
        return 2

    configure_logging(settings.log_level)
    shutdown = ShutdownSignal()
    install_signal_handlers(shutdown)

    try:
        return asyncio.run(run_forever(settings, shutdown))
    except ConfigError as exc:
        logger.critical("cannot start: %s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 0


if __name__ == "__main__":
    sys.exit(main())
