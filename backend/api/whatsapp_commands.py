"""WhatsApp command registry -- the single source for routing, help
output, and permission enforcement (docs/17_CodingStandards.md §6).

Every command in docs/08_WhatsApp.md gets exactly one entry here as it
is implemented; `help` renders from this data so the two can't drift.
"""

from __future__ import annotations

import difflib

from backend.api.command_types import (
    CommandHandler,
    CommandResult,
    CommandSpec,
    RequestContext,
)
from backend.api.commands.money_commands import (
    handle_bank,
    handle_cash,
    handle_expense,
    handle_income,
)
from backend.api.commands.ocr_commands import handle_details
from backend.api.commands.purchase_commands import handle_purchase
from backend.api.commands.sale_commands import handle_sale
from backend.api.commands.settlement_commands import handle_paid, handle_received
from backend.api.commands.stock_commands import handle_search, handle_stock
from backend.core.security import role_at_least
from backend.models.enums import UserRole

__all__ = [
    "COMMAND_REGISTRY",
    "CommandHandler",
    "CommandResult",
    "CommandSpec",
    "RequestContext",
    "closest_command",
    "handle_help",
]


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
    "purchase": CommandSpec(
        name="purchase",
        syntax="purchase Supplier: <name> Invoice: <no> Date: <DD-MM-YYYY> [Brand: <name>]\n"
        "<CODE> <qty> <rate> ... / Freight: <amt> / Other: <amt> / Total: <amt>",
        min_role=UserRole.STAFF,
        handler=handle_purchase,
        help_text="Record a purchase (draft + CONFIRM). Photo OCR arrives soon.",
    ),
    "sale": CommandSpec(
        name="sale",
        syntax="sale Customer: <name> [cash|bank|credit]\n<CODE> <qty> <rate> ...",
        min_role=UserRole.STAFF,
        handler=handle_sale,
        help_text="Record a sale. Defaults to credit; warns below cost or over limit.",
    ),
    "received": CommandSpec(
        name="received",
        syntax="received Customer: <name> <amount> <cash|bank> [against <ref>]",
        min_role=UserRole.STAFF,
        handler=handle_received,
        help_text="Record money received from a customer (oldest invoice first).",
    ),
    "paid": CommandSpec(
        name="paid",
        syntax="paid Supplier: <name> <amount> <cash|bank> [against <invoice>]",
        min_role=UserRole.STAFF,
        handler=handle_paid,
        help_text="Record money paid to a supplier (oldest invoice first).",
    ),
    "details": CommandSpec(
        name="details",
        syntax="details Supplier: <name> Invoice: <no> Date: DD-MM-YYYY Rate: <rate> "
        "[Brand: <name>] [Freight: <amt>] [Other: <amt>] [Total: <amt>]",
        min_role=UserRole.STAFF,
        handler=handle_details,
        help_text="Fill in the invoice details for a purchase sheet you photographed.",
    ),
    "stock": CommandSpec(
        name="stock",
        syntax="stock | stock <CODE> | stock low | stock negative",
        min_role=UserRole.STAFF,
        handler=handle_stock,
        help_text="Stock summary, one product's detail, or low/negative lists.",
    ),
    "search": CommandSpec(
        name="search",
        syntax="search <text>",
        min_role=UserRole.STAFF,
        handler=handle_search,
        help_text="Fuzzy-search products, suppliers and customers.",
    ),
    "expense": CommandSpec(
        name="expense",
        syntax="expense <category> <amount> <cash|bank> [description] [paid by <partner>]",
        min_role=UserRole.STAFF,
        handler=handle_expense,
        help_text="Record a business expense.",
    ),
    "income": CommandSpec(
        name="income",
        syntax="income <category> <amount> <cash|bank> [description]",
        min_role=UserRole.STAFF,
        handler=handle_income,
        help_text="Record non-sales income (interest, commission, ...).",
    ),
    "cash": CommandSpec(
        name="cash",
        syntax="cash",
        min_role=UserRole.STAFF,
        handler=handle_cash,
        help_text="Cash balance and recent entries.",
    ),
    "bank": CommandSpec(
        name="bank",
        syntax="bank",
        min_role=UserRole.STAFF,
        handler=handle_bank,
        help_text="Bank balance and recent entries.",
    ),
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
