"""Shared shapes for the WhatsApp command registry
(docs/17_CodingStandards.md §6) -- separate module so command handler
modules and the registry can both import them without cycles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.models import User
from backend.models.enums import UserRole


@dataclass(frozen=True)
class RequestContext:
    user: User
    session_factory: async_sessionmaker[AsyncSession]
    message_id: str | None = None


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
