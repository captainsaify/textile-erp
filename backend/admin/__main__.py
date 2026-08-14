"""`python -m backend.admin` -- and, via the wrapper, `erp`.

Importing the command modules is what registers them on the app, so the
imports are the wiring and are not unused.
"""

from __future__ import annotations

from backend.admin.app import cli
from backend.admin.commands import (  # noqa: F401
    add,
    catalog,
    fix,
    manage,
    safety,
    show,
    stock,
)

__all__ = ["cli"]

if __name__ == "__main__":
    cli()
