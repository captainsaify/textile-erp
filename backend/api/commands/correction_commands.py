"""`edit`, `undo`, `delete` -- docs/08_WhatsApp.md #edit / #undo /
#delete, docs/04_Purchases.md §8.

Grouped in one module because they are one idea seen from three angles:
how a mistake gets fixed. `edit` changes what is safe to change,
`delete` files away what has no financial history, and anything with
history routes to `undo`, which reverses by compensating entry.
"""

from __future__ import annotations

from backend.api.command_types import CommandResult, RequestContext
from backend.core.exceptions import DomainError, ValidationError
from backend.services.edit_service import EDITABLE, EditService, RoutedToUndo
from backend.services.undo_service import UndoResult, UndoService

EDIT_USAGE = "Usage: edit <product|supplier|customer|brand> <ref> <field> <value>"
DELETE_USAGE = "Usage: delete <product|supplier|customer|brand> <ref>"
UNDO_USAGE = "Usage: undo   (your last entry)   or   undo <purchase|sale> <ref>"


def parse_edit_command(args: str) -> tuple[str, str, str, str]:
    """`<entity> <ref> <field> <value>`, where **ref may contain spaces**
    ("Acme Traders"). Splitting on whitespace positionally would chop
    such a name in half, so the field name anchors the parse: it is the
    first token after the entity that names an editable field, and
    everything between is the reference.
    """
    tokens = args.split()
    if len(tokens) < 4:
        raise ValidationError(EDIT_USAGE)
    entity = tokens[0].lower()
    fields = EDITABLE.get(entity)
    if fields is None:
        # unknown entity: let the service produce the specific message,
        # falling back to a positional split so it has something to say
        return entity, tokens[1], tokens[2], " ".join(tokens[3:])

    for index in range(2, len(tokens) - 1):
        if tokens[index].lower() in fields:
            reference = " ".join(tokens[1:index])
            if not reference:
                raise ValidationError(EDIT_USAGE)
            return entity, reference, tokens[index].lower(), " ".join(tokens[index + 1 :])
    raise ValidationError(
        f"I couldn't find a field name in that. Editable on a {entity}: "
        f"{', '.join(sorted(fields))}.\n{EDIT_USAGE}"
    )


async def handle_edit(args: str, ctx: RequestContext) -> CommandResult:
    try:
        entity, reference, field, value = parse_edit_command(args)
    except ValidationError as exc:
        return CommandResult(reply=exc.message)
    try:
        async with ctx.session_factory() as session:
            result = await EditService(session).edit(
                ctx.user,
                entity=entity,
                reference=reference,
                field=field,
                value=value,
                whatsapp_message_id=ctx.message_id,
            )
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(
        reply=(
            f"✏️ {result.entity} {result.reference} — {result.field}: "
            f"{result.before} → {result.after}"
        )
    )


async def handle_delete(args: str, ctx: RequestContext) -> CommandResult:
    parts = args.split(maxsplit=1)
    if len(parts) < 2:
        return CommandResult(reply=DELETE_USAGE)
    entity, reference = parts[0], parts[1].strip()
    try:
        async with ctx.session_factory() as session:
            result = await EditService(session).delete(
                ctx.user,
                entity=entity,
                reference=reference,
                whatsapp_message_id=ctx.message_id,
            )
    except RoutedToUndo as exc:
        # not a dead end: say what to do instead (docs/08_WhatsApp.md
        # #delete routes financial records to the undo/cancel flow)
        return CommandResult(reply=f"🚫 {exc.message}")
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(
        reply=(
            f"🗑️ {result.entity} {result.name} deleted. It stays in your history and on "
            "past transactions — it just won't show up in new ones."
        )
    )


def render_undo(result: UndoResult) -> str:
    lines = [f"↩️ Undone — {result.description}."]
    lines.append("The original entry is kept and marked cancelled, not erased.")
    if result.cost_approximated:
        lines.append(
            "⚠️ Some of that stock had already moved on, so the average cost couldn't be "
            "unwound exactly. Worth a manual check."
        )
    if result.negative_stock_codes:
        lines.append(
            f"⚠️ {len(result.negative_stock_codes)} product(s) now show negative stock — "
            'reply "stock negative" to see them.'
        )
    return "\n".join(lines)


async def handle_undo(args: str, ctx: RequestContext) -> CommandResult:
    text = args.strip()
    entity: str | None = None
    reference: str | None = None
    if text:
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return CommandResult(reply=UNDO_USAGE)
        entity, reference = parts[0].lower(), parts[1].strip()

    try:
        async with ctx.session_factory() as session:
            result = await UndoService(session).undo(
                ctx.user,
                entity=entity,
                reference=reference,
                whatsapp_message_id=ctx.message_id,
            )
    except ValidationError as exc:
        return CommandResult(reply=exc.message)
    except DomainError as exc:
        return CommandResult(reply=exc.message)
    return CommandResult(reply=render_undo(result))
