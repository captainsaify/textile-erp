"""`login as test` / `login as real` / `demo` -- docs/29_DemoMode.md.

Which books a message writes to is decided before the command runs, in
the dispatcher, because by the time a command is executing the choice
has already been made for it. These three exist to *change* that choice,
so they are the only commands that read and write the mode itself.
"""

from __future__ import annotations

import datetime

from backend.api.command_types import CommandResult, RequestContext
from backend.api.interactive import Buttons, Choice
from backend.core.redis import get_redis
from backend.services.demo_service import DEMO_ORG_ID, DemoService

#: Keyed by sender, not by user id: the point of the mode is that one
#: person's phone writes to two different sets of books, and the user
#: row is the same one either way.
_KEY = "wa:demo:{number}"
#: Left on until turned off. A demo that expired mid-demonstration
#: would be worse than one somebody has to remember to leave -- and the
#: banner on every reply is the reminder.
_TTL_SECONDS = 60 * 60 * 24


def _key(whatsapp_number: str | None) -> str:
    return _KEY.format(number=whatsapp_number or "unknown")


async def is_demo(whatsapp_number: str | None) -> bool:
    return await get_redis().get(_key(whatsapp_number)) is not None


async def started_at(whatsapp_number: str | None) -> datetime.datetime | None:
    raw = await get_redis().get(_key(whatsapp_number))
    if raw is None:
        return None
    try:
        return datetime.datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw))
    except ValueError:
        return None


async def enter(whatsapp_number: str | None) -> None:
    await get_redis().set(
        _key(whatsapp_number),
        datetime.datetime.now(datetime.UTC).isoformat(),
        ex=_TTL_SECONDS,
    )


async def leave(whatsapp_number: str | None) -> None:
    await get_redis().delete(_key(whatsapp_number))


async def handle_login(args: str, ctx: RequestContext) -> CommandResult:
    """`login as test` / `login as real`.

    Phrased as a login because that is what it is from the outside: the
    same person, a different business. Anything else after `login` says
    so rather than guessing -- switching the wrong way is the one
    mistake this command can make.
    """
    target = args.strip().lower().removeprefix("as").strip()

    if target in {"test", "demo", "dummy", "sample"}:
        async with ctx.session_factory() as session, session.begin():
            service = DemoService(session)
            first_time = not await service.exists()
            # Seeded from the live business, so the demo reads the same
            # sheets and speaks the same units.
            await service.ensure(ctx.user.org_id)
        await enter(ctx.user.whatsapp_number)

        opening = (
            "Set up a fresh demo business — same units, product types and sheet "
            "templates as yours, and nothing else."
            if first_time
            else "Switched to the demo business."
        )
        return CommandResult(
            reply=(
                f"🧪 {opening}\n\n"
                "Everything you record now — purchases, sales, payments, stock — goes "
                "into these test books. Your real business is untouched and unreachable "
                "from here.\n\n"
                "• 'login as real' when you're done\n"
                "• 'reset demo' to wipe the test books and start the demonstration again"
            ),
            interactive=Buttons(
                body="You're in the demo business now.",
                choices=(
                    Choice(id="reset demo", title="Start fresh"),
                    Choice(id="login as real", title="Back to real"),
                ),
            ),
        )

    if target in {"real", "live", "back", "production", "main"}:
        was_demo = await is_demo(ctx.user.whatsapp_number)
        await leave(ctx.user.whatsapp_number)
        if not was_demo:
            return CommandResult(reply="You're already on your real books.")
        return CommandResult(
            reply=(
                "✅ Back on your real business. The demo books are still there — "
                "'login as test' returns to them, 'reset demo' empties them."
            )
        )

    return CommandResult(
        reply=(
            "Say 'login as test' for the demo business, or 'login as real' to come back.\n"
            "'demo' shows which one you're on."
        )
    )


async def handle_demo(args: str, ctx: RequestContext) -> CommandResult:
    """Which books am I writing to, and what is in them."""
    action = args.strip().lower()
    if action in {"on", "start", "test"}:
        return await handle_login("as test", ctx)
    if action in {"off", "stop", "exit", "end"}:
        return await handle_login("as real", ctx)

    active = await is_demo(ctx.user.whatsapp_number)
    if not active:
        return CommandResult(
            reply=(
                "📗 You're on your *real* business.\n"
                "Say 'login as test' to demonstrate without touching it."
            ),
            interactive=Buttons(
                body="Switch to the demo business?",
                choices=(Choice(id="login as test", title="Yes, demo mode"),),
            ),
        )

    async with ctx.session_factory() as session:
        counts = await DemoService(session).summary()
    since = await started_at(ctx.user.whatsapp_number)
    from backend.services.demo_service import demo_since

    when = f" (since {demo_since(since)})" if since else ""
    return CommandResult(
        reply=(
            f"🧪 You're in the *demo* business{when}.\n"
            f"It holds {counts['products']} product(s), {counts['purchase_headers']} purchase(s), "
            f"{counts['sales_headers']} sale(s), {counts['suppliers']} supplier(s), "
            f"{counts['customers']} customer(s).\n"
            "Your real books are untouched."
        ),
        interactive=Buttons(
            body="Demo business",
            choices=(
                Choice(id="reset demo", title="Start fresh"),
                Choice(id="login as real", title="Back to real"),
            ),
        ),
    )


async def handle_reset_demo(args: str, ctx: RequestContext) -> CommandResult:
    """Empty the demo's books so the next demonstration opens clean.

    Refused outside demo mode. The whole value of this command is that
    it deletes without asking, which is only safe because what it
    deletes was never real -- and `ctx.user.org_id` being the demo org
    is the check that guarantees that.
    """
    if not await is_demo(ctx.user.whatsapp_number):
        return CommandResult(
            reply=(
                "That only works inside the demo. You're on your real books, and "
                "nothing here deletes those. Say 'login as test' first."
            )
        )
    if ctx.user.org_id != DEMO_ORG_ID:
        # Belt and braces: the dispatcher should already have swapped
        # the org, and if it somehow hasn't, this must not run.
        return CommandResult(reply="I couldn't confirm you're in the demo — nothing was reset.")

    async with ctx.session_factory() as session, session.begin():
        removed = await DemoService(session).reset()

    total = sum(removed.values())
    if not total:
        return CommandResult(reply="🧪 The demo books were already empty.")
    return CommandResult(
        reply=(
            f"🧪 Demo books wiped — {total} row(s) across {len(removed)} table(s).\n"
            "Units, product types and sheet templates are still there, so you can "
            "start recording immediately."
        )
    )
