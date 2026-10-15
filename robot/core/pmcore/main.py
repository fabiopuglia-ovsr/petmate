"""Entry point: ``python -m pmcore`` or the ``pm-core`` console script."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="pm-core", description="Petmate host service")
    p.add_argument("--root", help="filesystem prefix for /etc and /var paths (dev)")
    p.add_argument("--log-level", default=None)
    p.add_argument("--log-format", choices=("json", "text"), default=None)
    p.add_argument("--no-hw", action="store_true", help="don't start cameras/encoder/display/audio")
    p.add_argument("--version", action="version", version=__version__)
    a = p.parse_args(argv)

    if a.root:
        os.environ["PETMATE_ROOT"] = a.root

    from .app import App
    from .log import setup_logging
    from .paths import Paths
    from .settings import Settings

    paths = Paths.from_env()
    settings = Settings.load(paths.host_settings)
    setup_logging(a.log_level or settings["log"]["level"], a.log_format or settings["log"]["format"])
    app = App(paths, settings, enable_hw=not a.no_hw)
    try:
        asyncio.run(app.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
