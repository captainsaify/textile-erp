"""Interactive message payloads -- docs/19_InteractiveMessages.md.

Handlers declare *intent* ("offer these three choices"); nothing here
knows about Cloud API JSON, which lives in the client.

The platform limits from §2 are enforced in `__post_init__` rather than
trusted. They are silent failures otherwise: an over-long title is a
rejected send at runtime, discovered by a partner staring at a message
that never arrived. Constructing an invalid payload raises immediately,
so it fails in a test instead.
"""

from __future__ import annotations

import dataclasses

# docs/19_InteractiveMessages.md §2, verified against Meta's docs
MAX_BUTTONS = 3
MAX_BUTTON_TITLE = 20
MAX_BUTTON_ID = 256
MAX_LIST_ROWS = 10
MAX_LIST_SECTIONS = 10
MAX_ROW_TITLE = 24
MAX_ROW_DESCRIPTION = 72
MAX_ROW_ID = 200
MAX_SECTION_TITLE = 24
MAX_MENU_LABEL = 20
MAX_BUTTON_BODY = 1024
MAX_LIST_BODY = 4096
MAX_FOOTER = 60


class InteractiveError(ValueError):
    """A payload that Meta would reject. Raised at construction."""


def _check(value: str, limit: int, what: str) -> str:
    if not value:
        raise InteractiveError(f"{what} cannot be empty")
    if len(value) > limit:
        raise InteractiveError(f"{what} is {len(value)} chars, limit is {limit}: {value!r}")
    return value


@dataclasses.dataclass(frozen=True)
class Choice:
    """One tappable option. `id` is the string the dispatcher will feed
    to the session handlers -- i.e. the command a user would have typed
    (docs/19 §7), so the tapped and typed paths cannot diverge."""

    id: str
    title: str
    description: str = ""


@dataclasses.dataclass(frozen=True)
class Buttons:
    """Up to 3 reply buttons. For decisions."""

    body: str
    choices: tuple[Choice, ...]
    footer: str = ""

    def __post_init__(self) -> None:
        _check(self.body, MAX_BUTTON_BODY, "button message body")
        if not 1 <= len(self.choices) <= MAX_BUTTONS:
            raise InteractiveError(
                f"{len(self.choices)} buttons; must be 1-{MAX_BUTTONS}. "
                "A fourth option needs a list menu."
            )
        titles = set()
        for choice in self.choices:
            _check(choice.id, MAX_BUTTON_ID, "button id")
            _check(choice.title, MAX_BUTTON_TITLE, "button title")
            # Meta requires unique titles; duplicates are silently
            # rejected, which looks like the message simply not arriving
            if choice.title in titles:
                raise InteractiveError(f"duplicate button title {choice.title!r}")
            titles.add(choice.title)
        if self.footer:
            _check(self.footer, MAX_FOOTER, "footer")


@dataclasses.dataclass(frozen=True)
class Section:
    title: str
    rows: tuple[Choice, ...]


@dataclasses.dataclass(frozen=True)
class ListMenu:
    """A menu button opening up to 10 rows. For picking from a set."""

    body: str
    menu_label: str
    sections: tuple[Section, ...]
    footer: str = ""

    def __post_init__(self) -> None:
        _check(self.body, MAX_LIST_BODY, "list message body")
        _check(self.menu_label, MAX_MENU_LABEL, "menu button label")
        if not 1 <= len(self.sections) <= MAX_LIST_SECTIONS:
            raise InteractiveError(f"{len(self.sections)} sections; must be 1-{MAX_LIST_SECTIONS}")

        total_rows = sum(len(section.rows) for section in self.sections)
        if not 1 <= total_rows <= MAX_LIST_ROWS:
            raise InteractiveError(
                f"{total_rows} rows across all sections; the limit is {MAX_LIST_ROWS}. "
                "Trim to the most relevant and keep a typed fallback."
            )
        ids = set()
        for section in self.sections:
            _check(section.title, MAX_SECTION_TITLE, "section title")
            for row in section.rows:
                _check(row.id, MAX_ROW_ID, "row id")
                _check(row.title, MAX_ROW_TITLE, "row title")
                if row.description and len(row.description) > MAX_ROW_DESCRIPTION:
                    raise InteractiveError(
                        f"row description is {len(row.description)} chars, "
                        f"limit is {MAX_ROW_DESCRIPTION}"
                    )
                if row.id in ids:
                    raise InteractiveError(f"duplicate row id {row.id!r}")
                ids.add(row.id)
        if self.footer:
            _check(self.footer, MAX_FOOTER, "footer")


Interactive = Buttons | ListMenu


def to_cloud_api(payload: Interactive, to_number: str) -> dict[str, object]:
    """Render for the Cloud API. Isolated here so the shape Meta wants
    exists in exactly one place."""
    recipient = to_number.lstrip("+")
    if isinstance(payload, Buttons):
        interactive: dict[str, object] = {
            "type": "button",
            "body": {"text": payload.body},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": c.id, "title": c.title}}
                    for c in payload.choices
                ]
            },
        }
    else:
        interactive = {
            "type": "list",
            "body": {"text": payload.body},
            "action": {
                "button": payload.menu_label,
                "sections": [
                    {
                        "title": section.title,
                        "rows": [
                            {
                                "id": row.id,
                                "title": row.title,
                                **({"description": row.description} if row.description else {}),
                            }
                            for row in section.rows
                        ],
                    }
                    for section in payload.sections
                ],
            },
        }
    if payload.footer:
        interactive["footer"] = {"text": payload.footer}
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "interactive",
        "interactive": interactive,
    }


def as_text(payload: Interactive) -> str:
    """The plain-text equivalent, for transports that can't send
    interactive messages (docs/19 §3). Options are numbered and their
    ids shown, so the flow is completable by typing."""
    lines = [payload.body, ""]
    if isinstance(payload, Buttons):
        lines.extend(f"• Reply *{c.id}* — {c.title}" for c in payload.choices)
    else:
        for section in payload.sections:
            lines.append(f"*{section.title}*")
            for row in section.rows:
                suffix = f" — {row.description}" if row.description else ""
                lines.append(f"• Reply *{row.id}* — {row.title}{suffix}")
    if payload.footer:
        lines.extend(["", payload.footer])
    return "\n".join(lines)
