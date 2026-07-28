"""Operational CLI -- `python -m backend.cli --help`.

First use: registering the partners' WhatsApp numbers so the bot will
talk to them at all (docs/08_WhatsApp.md §2: unknown numbers are
silently dropped, so onboarding can't happen over WhatsApp itself).
"""

from __future__ import annotations

import asyncio
import re
from typing import Annotated

import typer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.db import dispose_engine, get_session_factory
from backend.models import Organization, User
from backend.models.enums import UserRole

cli = typer.Typer(no_args_is_help=True, help="WhatsApp Trading ERP operations CLI")


@cli.callback()
def _root() -> None:
    """Keeps subcommand names (`create-user`) even while only one exists --
    a single-command Typer app otherwise flattens into the bare command."""


_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


async def create_user_record(
    session_factory: async_sessionmaker[AsyncSession],
    full_name: str,
    whatsapp_number: str,
    role: UserRole,
) -> User:
    if not _E164.match(whatsapp_number):
        raise ValueError(f"{whatsapp_number!r} is not E.164 (expected e.g. +919876543210)")
    async with session_factory() as session:
        org = (await session.execute(select(Organization).limit(1))).scalar_one_or_none()
        if org is None:
            raise ValueError("no organization found -- run `alembic upgrade head` first")
        existing = (
            await session.execute(
                select(User).where(
                    User.whatsapp_number == whatsapp_number, User.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(f"{whatsapp_number} already belongs to {existing.full_name}")
        user = User(org_id=org.id, full_name=full_name, whatsapp_number=whatsapp_number, role=role)
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@cli.command()
def create_user(
    full_name: Annotated[str, typer.Option("--name", help="Full name")],
    whatsapp_number: Annotated[str, typer.Option("--whatsapp", help="E.164, e.g. +919876543210")],
    role: Annotated[UserRole, typer.Option("--role")] = UserRole.OWNER,
) -> None:
    """Register a WhatsApp user so the bot responds to their number."""

    async def _run() -> User:
        try:
            return await create_user_record(get_session_factory(), full_name, whatsapp_number, role)
        finally:
            await dispose_engine()

    try:
        user = asyncio.run(_run())
    except ValueError as exc:
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    typer.secho(
        f"created {user.role.value} '{user.full_name}' ({user.whatsapp_number}) id={user.id}",
        fg=typer.colors.GREEN,
    )


@cli.command("set-password")
def set_password_command(
    email: Annotated[str, typer.Option(help="The user's email address")],
    password: Annotated[str, typer.Option(prompt=True, hide_input=True, confirmation_prompt=True)],
) -> None:
    """Give an account a dashboard login.

    Independent of WhatsApp access (docs/10_API.md §3): a partner ends up
    with both, an accountant may have only this, and staff who only use
    WhatsApp keep password_hash NULL and simply cannot sign in.
    """
    import asyncio

    from backend.core.db import get_session_factory
    from backend.services.auth_service import AuthError, set_password

    async def run() -> None:
        async with get_session_factory()() as session:
            async with session.begin():
                user = await set_password(session, email, password)
            typer.echo(f"Password set for {user.full_name} <{email}> (role {user.role.value}).")

    try:
        asyncio.run(run())
    except AuthError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from None


@cli.command("assign-brand")
def assign_brand(
    brand: Annotated[str, typer.Option("--brand", help="Brand name, e.g. TOP")],
    codes: Annotated[
        str | None,
        typer.Option("--codes", help="Comma-separated codes; omit to take every brandless one"),
    ] = None,
) -> None:
    """Put existing products under a brand.

    Sheets read from now on pick the brand up from their own LABEL
    column, but products created before that landed have `brand_id`
    NULL and nothing goes back to fix them. This is that fix, and it is
    audited like any other mutation (`CLAUDE.md` rule 3) rather than
    being a quiet UPDATE.

    Codes are only unique *within* a brand, so moving a product between
    brands can collide with one already there; that is reported per
    product instead of failing the whole run.
    """

    from backend.models import Brand, Product
    from backend.services.audit_service import AuditService

    async def _run() -> tuple[str, int, list[str]]:
        wanted = {code.strip().upper() for code in codes.split(",")} if codes else None
        factory = get_session_factory()
        try:
            async with factory() as session, session.begin():
                org = (await session.execute(select(Organization).limit(1))).scalar_one()
                actor = (
                    (
                        await session.execute(
                            select(User).where(User.org_id == org.id, User.deleted_at.is_(None))
                        )
                    )
                    .scalars()
                    .first()
                )
                if actor is None:
                    raise ValueError("no user to attribute this to -- create one first")

                existing = (
                    await session.execute(
                        select(Brand).where(Brand.org_id == org.id, Brand.name == brand)
                    )
                ).scalar_one_or_none()
                if existing is None:
                    existing = Brand(org_id=org.id, name=brand)
                    session.add(existing)
                    await session.flush()

                stmt = select(Product).where(Product.org_id == org.id, Product.deleted_at.is_(None))
                if wanted is None:
                    stmt = stmt.where(Product.brand_id.is_(None))
                products = list((await session.execute(stmt)).scalars())
                if wanted is not None:
                    products = [p for p in products if p.code.upper() in wanted]

                changed = 0
                skipped: list[str] = []
                audit = AuditService(session)
                for product in products:
                    clash = (
                        await session.execute(
                            select(Product).where(
                                Product.org_id == org.id,
                                Product.brand_id == existing.id,
                                Product.deleted_at.is_(None),
                                Product.id != product.id,
                            )
                        )
                    ).scalars()
                    if any(other.code.upper() == product.code.upper() for other in clash):
                        skipped.append(product.code)
                        continue
                    before = str(product.brand_id) if product.brand_id else None
                    product.brand_id = existing.id
                    await audit.record(
                        org.id,
                        actor.id,
                        action="product.brand_assigned",
                        entity_type="products",
                        entity_id=product.id,
                        channel="cli",
                        before_state={"code": product.code, "brand_id": before},
                        after_state={"code": product.code, "brand": brand},
                    )
                    changed += 1
                return brand, changed, skipped
        finally:
            await dispose_engine()

    try:
        name, changed, skipped = asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 -- surfaced to the operator
        typer.secho(f"error: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.secho(f"{changed} product(s) now under '{name}'", fg=typer.colors.GREEN)
    if skipped:
        typer.secho(
            f"skipped (that code already exists under '{name}'): {', '.join(skipped)}",
            fg=typer.colors.YELLOW,
        )


# Must stay last: typer registers commands as the module executes, so an
# entrypoint placed above a @cli.command() runs before that command
# exists. `set-password` was unreachable this way.
if __name__ == "__main__":
    cli()
