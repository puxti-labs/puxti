"""puxti CLI — Typer application assembled from one module per command.

The console-script entry point (`puxti = "puxti.cli:app"`) imports `app` from
here. Importing a command module registers its commands on `app` as a decorator
side effect, so the import order below is the `puxti --help` listing order.
"""
# ruff: noqa: I001

from puxti.cli._app import app

from puxti.cli import capture  # noqa: F401
from puxti.cli import scan  # noqa: F401
from puxti.cli import link  # noqa: F401
from puxti.cli import impact  # noqa: F401
from puxti.cli import redefine  # noqa: F401
from puxti.cli import correct  # noqa: F401
from puxti.cli import purge  # noqa: F401
from puxti.cli import describe  # noqa: F401
from puxti.cli import config  # noqa: F401
from puxti.cli import health  # noqa: F401
from puxti.cli import mcp  # noqa: F401
from puxti.cli import telemetry  # noqa: F401

__all__ = ["app"]
