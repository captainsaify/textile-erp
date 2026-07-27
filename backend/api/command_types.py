"""Shared shapes for the WhatsApp command registry
(docs/17_CodingStandards.md §6) -- separate module so command handler
modules and the registry can both import them without cycles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.interactive import Interactive
from backend.models import User
from backend.models.enums import UserRole


@dataclass(frozen=True)
class RequestContext:
    user: User
    session_factory: async_sessionmaker[AsyncSession]
    message_id: str | None = None


@dataclass(frozen=True)
class Notification:
    """A message to someone other than the sender. Carries its own
    buttons because the person who has to *act* on it is the recipient,
    not the sender -- a withdrawal approval is useless as a button on
    the requester's screen."""

    to_number: str
    body: str
    interactive: Interactive | None = None


@dataclass(frozen=True)
class CommandResult:
    reply: str
    #: Messages to people *other* than the sender -- the dual-approval
    #: request in docs/06_Accounting.md §8 is the first command that has
    #: to reach a second person. Delivered after the reply, best-effort:
    #: a partner being unreachable must not undo a committed transaction.
    notifications: tuple[Notification, ...] = ()
    #: Optional buttons/list menu accompanying `reply`
    #: (docs/19_InteractiveMessages.md). `reply` is always sent and is
    #: always sufficient on its own -- a transport that can't render
    #: this degrades to text and the flow still completes.
    interactive: Interactive | None = None


CommandHandler = Callable[[str, RequestContext], Awaitable[CommandResult]]


@dataclass(frozen=True)
class CommandSpec:
    name: str
    syntax: str
    min_role: UserRole
    handler: CommandHandler
    help_text: str
