"""`erp products`, `merge product`, `delete product`, `contacts`,
`relink`, `unlink`, `messages`, `health`, `rebuild-ledger`.

The commands that keep the *edges* of the system honest: the catalogue
the entry form writes into, the numbers that decide who is allowed to
write anything at all, and whether the messages the system sends are
actually arriving.

Unlike the older commands here, these are thin. The work lives in
`backend/services/admin/`, which is also what the Master Control web app
calls, so the terminal and the browser cannot drift into two different
ideas of what merging two products means. Each service opens its own
guard; this file resolves arguments, asks for confirmation, and prints.
"""

from __future__ import annotations

from typing import Annotated, Any

import typer

from backend.admin import console
from backend.admin.app import cli, confirm, run
from backend.admin.commands.manage import merge
from backend.admin.harness import AdminContext, AdminError
from backend.services import message_log
from backend.services.admin.contacts import ContactAdminService
from backend.services.admin.diagnostics import DiagnosticsService
from backend.services.admin.products import ProductAdminService


def _notes(result: dict[str, Any]) -> None:
    for note in result.get("notes", []):
        console.item(note)
    if result.get("reversal"):
        console.item(console.dim(f"reversible with: erp unmerge {result['reversal']}"))


# --- the catalogue ----------------------------------------------------


@cli.command("products")
def products(
    query: Annotated[str, typer.Argument(help="Filter by code or description")] = "",
) -> None:
    """The catalogue, with what has happened to each row.

    Sorted by code so duplicates land next to each other -- which is the
    whole point of looking at this list."""

    async def action(ctx: AdminContext) -> None:
        items = await ProductAdminService(ctx.session).catalogue(ctx.org_id, query=query)
        if not items:
            console.warn("no products match")
            return
        console.table(
            [
                [
                    item["code"],
                    item["brand"] or "—",
                    item["description"][:28],
                    item["on_hand"],
                    item["avg_cost"],
                    str(item["purchases"]),
                    str(item["sales"]),
                ]
                for item in items
            ],
            headers=["code", "label", "description", "on hand", "avg cost", "bought", "sold"],
        )
        spare = [i for i in items if i["deletable"]]
        if spare:
            console.say()
            console.item(
                console.dim(
                    f"{len(spare)} product(s) have never been bought or sold: "
                    + ", ".join(i["label"] for i in spare[:5])
                    + ("…" if len(spare) > 5 else "")
                )
            )

    run(action)


@merge.command("product")
def merge_product(
    loser: Annotated[str, typer.Argument(help="The code that stops existing")],
    into: Annotated[str, typer.Argument(metavar="into CODE", help="Literal word `into`")],
    winner: Annotated[str, typer.Argument(help="The code that survives")],
    loser_label: Annotated[
        str | None, typer.Option("--from-label", help="Label of the losing product")
    ] = None,
    winner_label: Annotated[
        str | None, typer.Option("--to-label", help="Label of the surviving product")
    ] = None,
) -> None:
    """Fold one product into another, replaying the cost.

    The repair for the same cloth entered under two codes, or one code
    under two labels. Unlike merging parties, this changes a number: the
    survivor's weighted average is recomputed from both histories in
    movement order, because that is the only value it could honestly
    have afterwards.

    Codes are ambiguous on these books -- three brands carry 55X -- so
    give `--from-label` / `--to-label` when a code is not unique."""

    async def action(ctx: AdminContext) -> None:
        if into.lower() != "into":
            raise AdminError("usage: erp merge product 55X into 55XL --to-label TOP")
        service = ProductAdminService(ctx.session)
        plan = await service.merge_plan(
            ctx.org_id,
            loser_code=loser,
            loser_brand=loser_label,
            winner_code=winner,
            winner_brand=winner_label,
        )
        console.head(f"Merge {plan.loser_label} → {plan.winner_label}")
        console.item(f"{len(plan.movements)} stock movement(s)")
        console.item(f"{len(plan.purchase_lines)} purchase line(s), {len(plan.sales_lines)} sale")
        console.item(
            f"{console.qty(plan.loser_qty)} + {console.qty(plan.winner_qty)} "
            f"= {console.qty(plan.loser_qty + plan.winner_qty)} on hand"
        )
        console.item(
            console.dim(
                f"averages {plan.loser_avg} and {plan.winner_avg} — the survivor's is replayed"
            )
        )
        if not plan.ok:
            for blocker in plan.blockers:
                console.bad(blocker)
            raise AdminError("nothing was changed.")

        confirm(
            ctx,
            expected=plan.winner_label,
            prompt=f"Type the surviving product ({plan.winner_label}): ",
        )
        result = await service.merge_apply(ctx.org_id, ctx.actor, plan, dry_run=ctx.dry_run)
        _notes(result)

    run(action)


@cli.command("delete-product")
def delete_product(
    code: Annotated[str, typer.Argument(help="Product code")],
    label: Annotated[
        str | None, typer.Option("--label", help="Brand, if the code is shared")
    ] = None,
) -> None:
    """Remove a product nothing has ever happened to.

    A typo made while entering a bill, caught after saving. A product
    with history is not a mistake in the catalogue -- it is part of the
    record -- and merging is the repair for that."""

    async def action(ctx: AdminContext) -> None:
        service = ProductAdminService(ctx.session)
        product = await service.resolve(ctx.org_id, code, label)
        confirm(ctx, expected=product.code, prompt=f"Type {product.code} to confirm: ")
        result = await service.delete(ctx.org_id, ctx.actor, code=code, brand=label)
        console.ok(f"{result['label']} removed")
        _notes(result)

    run(action)


# --- who the system can reach -----------------------------------------


@cli.command("contacts")
def contacts() -> None:
    """Which number reaches which person, and when it last did.

    A number that has never been seen is either wrong or unused, and
    from a list of names those look identical."""

    async def action(ctx: AdminContext) -> None:
        items = await ContactAdminService(ctx.session).contacts(ctx.org_id)
        console.table(
            [
                [
                    item["name"],
                    item["role"],
                    item["whatsapp_number"] or console.red("none"),
                    item["email"] or "—",
                    (item["last_seen"] or "never")[:16].replace("T", " "),
                ]
                for item in items
            ],
            headers=["name", "role", "whatsapp", "email", "last seen"],
        )

    run(action)


@cli.command("relink")
def relink(
    number: Annotated[str, typer.Argument(help="The WhatsApp number, e.g. 7000087329")],
    to: Annotated[str, typer.Argument(metavar="to NAME", help="Literal word `to`")],
    name: Annotated[str, typer.Argument(help="Whose number it now is")],
) -> None:
    """Point a WhatsApp number at a person.

    Until this is run, messages from a new SIM reach an unrecognised
    number -- and the correct response to an unrecognised number is
    silence, so the symptom is "nothing happens"."""

    async def action(ctx: AdminContext) -> None:
        if to.lower() != "to":
            raise AdminError('usage: erp relink 7000087329 to "Firoz"')
        result = await ContactAdminService(ctx.session).relink(
            ctx.org_id, ctx.actor, number=number, to_name=name
        )
        console.ok(f"{result['number']} → {result['user']}")
        _notes(result)

    run(action)


@cli.command("unlink")
def unlink(name: Annotated[str, typer.Argument(help="Whose number to remove")]) -> None:
    """Take a number away without giving it to anyone. For a SIM that is
    gone rather than moved."""

    async def action(ctx: AdminContext) -> None:
        result = await ContactAdminService(ctx.session).unlink(ctx.org_id, ctx.actor, name=name)
        console.ok(f"{result['number']} no longer reaches anyone")
        _notes(result)

    run(action)


# --- did it arrive ----------------------------------------------------


@cli.command("messages")
def messages(
    failed: Annotated[bool, typer.Option("--failed", help="Only what did not arrive")] = False,
    limit: Annotated[int, typer.Option("--limit", help="How many rows")] = 30,
    hours: Annotated[int, typer.Option("--hours", help="Window for the summary")] = 24,
) -> None:
    """What the system sent and received, and what came back.

    Seventeen messages failed overnight with Meta code 131047 and the
    only way to find out was to read the container's stdout."""

    async def action(ctx: AdminContext) -> None:
        summary = await message_log.failure_summary(ctx.session, since_hours=hours)
        console.head(f"Last {hours}h: {summary['messages']} message(s), {summary['failed']} failed")
        for cause in summary["causes"]:
            console.bad(
                f"{cause['count']}× {cause['code']} — {cause['meaning'] or cause['detail']}"
            )
        if not summary["causes"]:
            console.ok("everything sent in that window arrived")

        console.say()
        rows = await message_log.recent(ctx.session, limit=limit, failed_only=failed)
        if not rows:
            console.warn("nothing recorded yet")
            return
        console.table(
            [
                [
                    row.created_at.strftime("%d %b %H:%M"),
                    "→" if row.direction == "out" else "←",
                    row.peer,
                    row.kind,
                    ("ok" if row.ok else console.red(row.error_code or "failed")),
                    row.preview[:38].replace("\n", " "),
                ]
                for row in rows
            ],
            headers=["when", "", "who", "kind", "result", "message"],
        )

    run(action)


# --- is the machine well ----------------------------------------------


@cli.command("health")
def health() -> None:
    """Size, disk, whether the nightly jobs still run, and whether the
    running balances still equal what they summarise.

    `erp check` answers whether the books balance. This answers the
    questions underneath it."""

    async def action(ctx: AdminContext) -> None:
        report = await DiagnosticsService(ctx.session).report(ctx.org_id)
        console.head(ctx.org.name)
        console.table(
            [[row["label"], str(row["count"])] for row in report["counts"]],
            headers=["", "rows"],
        )
        console.say()
        console.item(f"database {report['database_mb']} MB, {report['disk_free_gb']} GB free")
        console.item(f"{report['backups']} backup(s), newest {report['newest_backup'] or 'none'}")
        for run_info in report["nightly"]:
            when = run_info["last_run"][:16].replace("T", " ")
            line = f"{run_info['kind']} reconciliation last ran {when}"
            console.bad(line) if run_info["stale"] else console.item(line)
        if not report["nightly"]:
            console.bad("no reconciliation has ever run — the nightly job is not scheduled")
        for drift in report["ledger_drift"]:
            console.bad(
                f"{drift['ledger']} running balance says {drift['says']}, "
                f"should be {drift['should_be']} — run: erp rebuild-ledger"
            )
        if not report["ledger_drift"]:
            console.ok("every running balance agrees with the rows behind it")

    run(action)


@cli.command("rebuild-ledger")
def rebuild_ledger() -> None:
    """Rewrite every running balance from the rows themselves.

    Computes rather than destroys, like `recost`: the amounts are never
    touched, only the derived snapshot beside each one. Safe to run."""

    async def action(ctx: AdminContext) -> None:
        result = await DiagnosticsService(ctx.session).rebuild_ledgers(ctx.org_id, ctx.actor)
        for note in result["notes"]:
            console.item(note)
        console.ok(f"{result['corrected']} running balance(s) corrected")

    run(action)
