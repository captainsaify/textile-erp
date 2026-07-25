"""WhatsApp command registry -- the single source for routing, help
output, and permission enforcement (docs/17_CodingStandards.md §6).

Every command in docs/08_WhatsApp.md gets exactly one entry here as it
is implemented; `help` renders from this data so the two can't drift.
The registry grows with each feature phase -- the CommandSpec shape
gains a dedicated `parser` field when the first structured-grammar
command (purchase/sale) lands.
"""

from __future__ import annotations

import difflib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from backend.core.security import role_at_least
from backend.models import User
from backend.models.enums import UserRole


@dataclass(frozen=True)
class RequestContext:
    user: User


@dataclass(frozen=True)
class CommandResult:
    reply: str


CommandHandler = Callable[[str, RequestContext], Awaitable[CommandResult]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    syntax: str
    min_role: UserRole
    handler: CommandHandler
    help_text: str


async def handle_help(args: str, ctx: RequestContext) -> CommandResult:
    """`help` / `help <command>` -- docs/08_WhatsApp.md#help. Output is
    role-filtered: a command the user can't run is not listed."""
    topic = args.strip().lower()
    if topic:
        spec = COMMAND_REGISTRY.get(topic)
        if spec is None or not role_at_least(ctx.user.role, spec.min_role):
            suggestion = closest_command(topic, ctx.user.role)
            hint = f" Did you mean '{suggestion}'?" if suggestion else ""
            return CommandResult(reply=f"I don't know a command called '{topic}'.{hint}")
        return CommandResult(reply=f"*{spec.name}*\nSyntax: {spec.syntax}\n{spec.help_text}")

    lines = ["🤖 Available commands:"]
    for spec in COMMAND_REGISTRY.values():
        if role_at_least(ctx.user.role, spec.min_role):
            lines.append(f"• {spec.name} — {spec.help_text}")
    lines.append("Send 'help <command>' for syntax and details.")
    return CommandResult(reply="\n".join(lines))


COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "help": CommandSpec(
        name="help",
        syntax="help [command]",
        min_role=UserRole.VIEWER,
        handler=handle_help,
        help_text="Show available commands, or details for one command.",
    ),
}


def closest_command(word: str, role: UserRole) -> str | None:
    """Fuzzy suggestion for a mistyped command, limited to commands the
    user is actually allowed to run."""
    visible = [
        name for name, spec in COMMAND_REGISTRY.items() if role_at_least(role, spec.min_role)
    ]
    matches = difflib.get_close_matches(word.lower(), visible, n=1, cutoff=0.6)
    return matches[0] if matches else None
