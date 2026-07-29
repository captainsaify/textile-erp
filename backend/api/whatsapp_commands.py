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
from backend.api.commands.capital_commands import (
    handle_approve,
    handle_capital,
    handle_reject,
    handle_withdraw,
)
from backend.api.commands.correction_commands import (
    handle_delete,
    handle_edit,
    handle_undo,
)
from backend.api.commands.money_commands import (
    handle_bank,
    handle_cash,
    handle_expense,
    handle_income,
)
from backend.api.commands.ocr_commands import handle_details
from backend.api.commands.ops_commands import handle_backup, handle_export, handle_restore
from backend.api.commands.purchase_commands import handle_purchase
from backend.api.commands.report_commands import (
    handle_customer,
    handle_dashboard,
    handle_ledger,
    handle_profit,
    handle_summary,
    handle_supplier,
)
from backend.api.commands.return_commands import handle_return
from backend.api.commands.sale_commands import handle_sale
from backend.api.commands.settings_commands import handle_settings
from backend.api.commands.settlement_commands import handle_paid, handle_received
from backend.api.commands.stock_commands import handle_search, handle_stock
from backend.api.interactive import Choice, ListMenu, Section
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
    return CommandResult(reply="\n".join(lines), interactive=main_menu(ctx.user.role))


#: The menu groups commands by *intent*, not alphabetically -- someone
#: opening it knows what they want to do, not what it is called. A list
#: caps at 10 rows total (docs/19 §2), so this is the shortlist of what
#: gets reached for; the full set stays in the text above it.
_MENU: tuple[tuple[str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        "Record",
        (
            ("sale", "Record a sale", "Stock out, money in or on credit"),
            ("paid", "Pay a supplier", "Money out against an invoice"),
            ("received", "Money received", "From a customer"),
            ("expense", "Record an expense", "Transport, packing, rent…"),
        ),
    ),
    (
        "Look up",
        (
            ("dashboard", "Dashboard", "Cash, stock, profit, who owes what"),
            ("stock", "Stock summary", "What's on hand and what's low"),
            ("summary", "Period summary", "Sales, purchases, profit"),
        ),
    ),
    (
        "Manage",
        (
            ("undo", "Undo last entry", "Reverses your most recent one"),
            ("export", "Export to Excel", "Purchases, sales or stock"),
            ("help", "All commands", "The full list"),
        ),
    ),
)


def main_menu(role: UserRole) -> ListMenu:
    """Role-filtered: a command the user can't run is never offered."""
    sections: list[Section] = []
    for title, entries in _MENU:
        rows = tuple(
            Choice(id=command, title=label, description=description)
            for command, label, description in entries
            if (spec := COMMAND_REGISTRY.get(command)) is not None
            and role_at_least(role, spec.min_role)
        )
        if rows:
            sections.append(Section(title=title, rows=rows))
    return ListMenu(
        body="What would you like to do?",
        menu_label="Choose",
        sections=tuple(sections),
    )


async def _sheet_handler(args: str, ctx: RequestContext) -> CommandResult:
    from backend.api.commands.draft_preview import handle_sheet

    return await handle_sheet(args, ctx)


async def _receive_handler(args: str, ctx: RequestContext) -> CommandResult:
    from backend.api.commands.receipt_commands import handle_receive

    return await handle_receive(args, ctx)


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
    "sheet": CommandSpec(
        name="sheet",
        syntax="sheet",
        min_role=UserRole.STAFF,
        handler=_sheet_handler,
        help_text=(
            "See the draft you're about to confirm as an Excel sheet. Nothing is saved; "
            "CONFIRM and 'discard' still work afterwards."
        ),
    ),
    "receive": CommandSpec(
        name="receive",
        syntax="receive <invoice> <CODE> <bales received>",
        min_role=UserRole.STAFF,
        handler=_receive_handler,
        help_text=(
            "Correct what actually arrived. `receive 001 35A 9` means the invoice said "
            "10 bales but 9 turned up: the bill, the payable and the stock all follow."
        ),
    ),
    "return": CommandSpec(
        name="return",
        syntax="return sale <customer|last> <CODE> <qty> [reason: <text>]\n"
        "return purchase <invoice-no|last> <CODE> <qty> [reason: <text>]",
        min_role=UserRole.STAFF,
        handler=handle_return,
        help_text="Return goods to a supplier, or take goods back from a customer.",
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
        shareable=True,
    ),
    "search": CommandSpec(
        name="search",
        syntax="search <text>",
        min_role=UserRole.STAFF,
        handler=handle_search,
        help_text="Fuzzy-search products, suppliers and customers.",
        shareable=True,
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
        shareable=True,
    ),
    "bank": CommandSpec(
        name="bank",
        syntax="bank",
        min_role=UserRole.STAFF,
        handler=handle_bank,
        help_text="Bank balance and recent entries.",
        shareable=True,
    ),
    "capital": CommandSpec(
        name="capital",
        syntax="capital <partner> <amount> <cash|bank> [contribution|withdrawal]",
        min_role=UserRole.OWNER,
        handler=handle_capital,
        help_text="Record a partner's capital contribution (or small withdrawal).",
    ),
    "withdraw": CommandSpec(
        name="withdraw",
        syntax="withdraw <partner> <amount> <cash|bank>",
        min_role=UserRole.OWNER,
        handler=handle_withdraw,
        help_text="Withdraw partner capital. Large amounts need a second partner's approval.",
    ),
    "approve": CommandSpec(
        name="approve",
        syntax="approve withdraw <id>",
        min_role=UserRole.OWNER,
        handler=handle_approve,
        help_text="Approve another partner's pending capital withdrawal.",
    ),
    "reject": CommandSpec(
        name="reject",
        syntax="reject withdraw <id>",
        min_role=UserRole.OWNER,
        handler=handle_reject,
        help_text="Reject another partner's pending capital withdrawal.",
    ),
    "dashboard": CommandSpec(
        name="dashboard",
        syntax="dashboard",
        min_role=UserRole.STAFF,
        handler=handle_dashboard,
        help_text="Cash, bank, inventory, today's activity, profit, receivables/payables.",
        shareable=True,
    ),
    "summary": CommandSpec(
        name="summary",
        syntax="summary [today|week|month|year|<DD-MM-YYYY> to <DD-MM-YYYY>]",
        min_role=UserRole.STAFF,
        handler=handle_summary,
        help_text="Condensed sales/purchases/expenses/profit digest for a period.",
        shareable=True,
    ),
    "profit": CommandSpec(
        name="profit",
        syntax="profit [today|week|month|year|<DD-MM-YYYY> to <DD-MM-YYYY>]",
        min_role=UserRole.OWNER,
        handler=handle_profit,
        help_text="Profit & loss for a period.",
        shareable=True,
    ),
    "supplier": CommandSpec(
        name="supplier",
        syntax="supplier <name>",
        min_role=UserRole.STAFF,
        handler=handle_supplier,
        help_text="Outstanding payable, aging, and recent purchases for a supplier.",
        shareable=True,
    ),
    "customer": CommandSpec(
        name="customer",
        syntax="customer <name>",
        min_role=UserRole.STAFF,
        handler=handle_customer,
        help_text="Outstanding receivable, aging, and recent sales for a customer.",
        shareable=True,
    ),
    "ledger": CommandSpec(
        name="ledger",
        syntax="ledger <supplier|customer> <name>  |  ledger <CODE>",
        min_role=UserRole.STAFF,
        handler=handle_ledger,
        help_text="Statement of invoices/payments for a party, or movement history for a product.",
        shareable=True,
    ),
    "edit": CommandSpec(
        name="edit",
        syntax="edit <product|supplier|customer|brand> <ref> <field> <value>",
        min_role=UserRole.OWNER,
        handler=handle_edit,
        help_text="Change a detail on a product, supplier, customer or brand.",
    ),
    "undo": CommandSpec(
        name="undo",
        syntax="undo  |  undo <purchase|sale> <ref>",
        min_role=UserRole.STAFF,
        handler=handle_undo,
        help_text="Reverse your last entry (or a named one) by compensating entry.",
    ),
    "delete": CommandSpec(
        name="delete",
        syntax="delete <product|supplier|customer|brand> <ref>",
        min_role=UserRole.OWNER,
        handler=handle_delete,
        help_text="Retire master data. Financial records route to 'undo' instead.",
    ),
    "settings": CommandSpec(
        name="settings",
        syntax="settings  |  settings <key> <value>",
        min_role=UserRole.OWNER,
        handler=handle_settings,
        help_text="List or change the business's configurable thresholds.",
    ),
    "export": CommandSpec(
        name="export",
        syntax="export <purchases|sales|stock> [period]",
        min_role=UserRole.STAFF,
        handler=handle_export,
        help_text="Build an Excel export. Arrives as a message when it's ready.",
    ),
    "backup": CommandSpec(
        name="backup",
        syntax="backup  |  backup now",
        min_role=UserRole.OWNER,
        handler=handle_backup,
        help_text="List backups, or take one immediately.",
    ),
    "restore": CommandSpec(
        name="restore",
        syntax="restore <backup-name> confirm <backup-name>",
        min_role=UserRole.OWNER,
        handler=handle_restore,
        help_text="Replace all data with a backup's contents. Requires double confirmation.",
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
