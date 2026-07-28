"""Sharing a result with the group -- docs/22_GroupBroadcast.md §4.

`summary`, `dashboard`, `ledger`, `supplier`, `customer` and the like
answer a question for the person who asked. Often the answer is the
thing the other partner wanted too, and re-typing it into the group is
exactly the manual step this system exists to remove.

So a shareable result ends with one button. Tapping it posts **the text
that was shown**, held in the session -- not a re-run of the query.
Re-running could produce a different answer a minute later, and sharing
something the sender never saw is worse than not sharing at all.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.api.interactive import Buttons, Choice
from backend.services.session_service import IDLE, SessionService, SessionState

SHARE_STATE = "awaiting_share_choice"

SHARE_ID = "share group"
DECLINE_ID = "share no"


def offer(result: CommandResult) -> Buttons:
    return Buttons(
        body="Share this with the group?",
        choices=(
            Choice(id=SHARE_ID, title="Share to group"),
            Choice(id=DECLINE_ID, title="Just for me"),
        ),
    )


async def remember(result: CommandResult, ctx: RequestContext) -> None:
    await SessionService(ctx.session_factory).set(
        ctx.user.org_id,
        ctx.user.id,
        SHARE_STATE,
        {"body": result.reply, "file": result.share_file or ""},
    )


async def handle_share_reply(text: str, ctx: RequestContext, state: SessionState) -> CommandResult:
    from pathlib import Path

    from backend.services.group_relay import get_group_relay

    sessions = SessionService(ctx.session_factory)
    choice = text.strip().lower().removeprefix("share ").strip()
    await sessions.set(ctx.user.org_id, ctx.user.id, IDLE, {})

    if choice not in {"group", "yes", "share", "share to group"}:
        return CommandResult(reply="Kept it to yourself.")

    body = str(state.context.get("body", ""))
    file_path = str(state.context.get("file", ""))
    relay = get_group_relay()

    who = ctx.user.full_name
    if file_path:
        result = await relay.send_file(Path(file_path), caption=f"{body}\n— shared by {who}")
    else:
        result = await relay.send_text(f"{body}\n\n— shared by {who}")

    if result.delivered:
        return CommandResult(reply="📣 Sent to the group.")
    # Say why. A silent failure here means someone believes the other
    # partner has seen a number they haven't.
    return CommandResult(reply=f"⚠️ Couldn't share that. {result.reason}")
