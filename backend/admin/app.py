"""Typer wiring, global flags, and the one place a session is opened."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

import typer

from backend.admin import console
from backend.admin.harness import AdminContext, AdminError, resolve_context
from backend.core.db import dispose_engine, get_session_factory
from backend.core.exceptions import ValidationError

cli = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Repair the books from the command line. See ADMIN.md.",
    rich_markup_mode=None,
)


@dataclasses.dataclass
class _Flags:
    demo: bool = False
    dry_run: bool = False
    assume_yes: bool = False


FLAGS = _Flags()


@cli.callback()
def _root(
    demo: Annotated[
        bool, typer.Option("--demo", help="Act on the demo business instead of the real books.")
    ] = False,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Show the full effect, change nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Skip typed confirmation. For scripts, not people.")
    ] = False,
) -> None:
    FLAGS.demo = demo
    FLAGS.dry_run = dry_run
    FLAGS.assume_yes = yes


def run(action: Callable[[AdminContext], Awaitable[Any]]) -> Any:
    """Open one session, resolve who and which books, run, close.

    Every command goes through here, so there is exactly one place that
    decides what `--demo` means and exactly one that prints an error
    without a traceback."""

    async def _main() -> Any:
        factory = get_session_factory()
        try:
            async with factory() as session:
                ctx = await resolve_context(
                    session,
                    demo=FLAGS.demo,
                    dry_run=FLAGS.dry_run,
                    assume_yes=FLAGS.assume_yes,
                )
                if not ctx.is_demo:
                    console.say(console.dim(f"── {ctx.org.name} (real books) ──"))
                else:
                    console.say(console.yellow(f"── {ctx.org.name} ── demo, not the real books"))
                return await action(ctx)
        finally:
            await dispose_engine()

    try:
        return asyncio.run(_main())
    except (AdminError, ValidationError) as exc:
        # ReconciliationRegressed has already printed its detail.
        if str(exc) != "reconciliation regressed":
            console.say()
            console.bad(str(exc))
        raise typer.Exit(1) from None


def confirm(ctx: AdminContext, *, expected: str, prompt: str) -> None:
    """Confirmation by typing the thing back, not by typing 'y'.

    `y/n` is muscle memory and gets answered before it is read. Typing
    `1051` requires having looked at what is about to happen."""
    if ctx.assume_yes:
        return
    typed = typer.prompt(f"  {prompt}")
    if typed.strip() != expected:
        raise AdminError(f"got {typed.strip()!r}, expected {expected!r} -- nothing was changed.")
