"""Purchase wave: grammar, allocation math (docs/04_Purchases.md §4),
weighted average (docs/03_Inventory.md §2 worked example), the session
flow (create supplier/product, corrections, CONFIRM), and duplicate
detection layers (docs/04_Purchases.md §6)."""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.command_types import CommandResult, RequestContext
from backend.api.commands.purchase_commands import (
    handle_purchase,
    handle_purchase_session_reply,
    parse_purchase_command,
    render_preview,
)
from backend.api.interactive import Buttons
from backend.core.exceptions import ValidationError
from backend.models import User
from backend.services.inventory_service import InventoryService
from backend.services.purchase_service import Draft, DraftLine, allocate
from backend.services.session_service import SessionService
from backend.tests.conftest import (
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    purge_business_rows,
)

D = decimal.Decimal
ORG = uuid.UUID(SEEDED_ORG_ID)

PURCHASE_TEXT = (
    "Supplier: Shree Textiles Invoice: INV-4521 Date: 24-07-2026\n"
    "TRP 100 150\n"
    "MJP 40 210\n"
    "Freight: 500\n"
    "Other: 100"
)


@pytest.fixture(autouse=True)
async def clean_tables(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
def ctx(staff_user: User, session_factory: async_sessionmaker[AsyncSession]) -> RequestContext:
    return RequestContext(user=staff_user, session_factory=session_factory)


async def _session_reply(text: str, ctx: RequestContext) -> CommandResult:
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    return await handle_purchase_session_reply(text, ctx, state)


def test_allocate_sums_exactly_with_remainder_on_largest_line() -> None:
    shares = allocate(D("500"), [D("100"), D("40")])
    assert shares == [D("357.14"), D("142.86")]
    assert sum(shares) == D("500")

    # a split that doesn't divide evenly: remainder lands on largest weight
    shares = allocate(D("100"), [D("1"), D("1"), D("1")])
    assert sum(shares) == D("100")
    assert shares[2] - shares[0] in {D("0"), D("0.01"), D("0.02")}

    assert allocate(D("0"), [D("5")]) == [D("0")]


def test_parse_purchase_command_full_grammar() -> None:
    draft = parse_purchase_command(PURCHASE_TEXT + "\nTotal: 24000")
    assert draft.supplier_name == "Shree Textiles"
    assert draft.invoice_no == "INV-4521"
    assert draft.invoice_date.isoformat() == "2026-07-24"
    assert [(line.code, line.qty, line.rate) for line in draft.lines] == [
        ("TRP", D("100"), D("150")),
        ("MJP", D("40"), D("210")),
    ]
    assert draft.freight == D("500")
    assert draft.other_charges == D("100")
    assert draft.declared_total == D("24000")
    assert draft.subtotal == D("23400.00")
    assert draft.grand_total == D("24000.00")

    with pytest.raises(ValidationError, match="Couldn't read the first line"):
        parse_purchase_command("Supplier only nonsense")
    with pytest.raises(ValidationError, match="item line"):
        parse_purchase_command("Supplier: X Invoice: 1 Date: 24-07-2026\nTRP one hundred 150")


async def test_weighted_average_worked_example(
    staff_user: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """docs/03_Inventory.md §2: opening 100 @ 150, +50 @ 160 -> 153.33;
    sale to 120 (avg unchanged); +20 @ 140 -> 151.43."""
    from backend.models import Inventory, Product, ProductType

    async with session_factory() as session:
        product_type = (
            await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG))
        ).scalar_one()
        product = Product(
            org_id=ORG,
            product_type_id=product_type.id,
            code="WAVG",
            description="Worked Example",
            unit_id=product_type.default_unit_id,
            created_by=staff_user.id,
        )
        session.add(product)
        await session.flush()
        session.add(
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty_on_hand=D("100"),
                weighted_avg_cost=D("150"),
            )
        )
        await session.commit()
        product_id = product.id

    async with session_factory() as session:
        async with session.begin():
            movement = await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product_id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty=D("50"),
                landed_cost_per_unit=D("160"),
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
            )
        assert movement.resulting_qty_on_hand == D("150.000")
        assert movement.resulting_avg_cost == D("153.3333")

    async with session_factory() as session:  # simulate the sale: qty down, avg untouched
        await session.execute(
            sa.text("UPDATE inventory SET qty_on_hand = 120 WHERE product_id = :id"),
            {"id": product_id},
        )
        await session.commit()

    async with session_factory() as session:
        async with session.begin():
            movement = await InventoryService(session).record_purchase_movement(
                ORG,
                product_id=product_id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty=D("20"),
                landed_cost_per_unit=D("140"),
                source_id=uuid.uuid4(),
                created_by=staff_user.id,
            )
        assert movement.resulting_qty_on_hand == D("140.000")
        # doc shows 151.43 (2dp); at 4dp storage precision the stored
        # 153.3333 average yields (120*153.3333 + 20*140)/140 = 151.4285
        assert movement.resulting_avg_cost == D("151.4285")


async def test_full_purchase_session_flow(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    result = await handle_purchase(PURCHASE_TEXT, ctx)
    assert "Purchase draft ready — Shree Textiles, INV-4521" in result.reply
    assert "unknown product" in result.reply
    # the missing supplier is stated, but the codes are what's being
    # asked about first -- one instruction at a time
    assert "Supplier 'Shree Textiles' isn't in your list yet" in result.reply

    result = await _session_reply("create supplier", ctx)
    assert "isn't in your list yet" not in result.reply

    result = await _session_reply("create product TRP Trouser Poly", ctx)
    assert "TRP  100.0 KG × ₹150.00 = ₹15,000.00" in result.reply
    result = await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    assert "Reply CONFIRM to save" in result.reply

    # correction before confirming
    result = await _session_reply("line 2 qty 45", ctx)
    assert "MJP  45.0 KG × ₹210.00 = ₹9,450.00" in result.reply

    result = await _session_reply("line 2 qty 40", ctx)
    result = await _session_reply("CONFIRM", ctx)
    assert "✅ Purchase confirmed — Shree Textiles, INV-4521" in result.reply
    assert "Grand total: ₹24,000.00" in result.reply
    assert "• TRP now 100.0 KG" in result.reply

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.text("SELECT status::text, subtotal, grand_total, freight FROM purchase_headers")
            )
        ).one()
        assert header.status == "confirmed"
        assert header.subtotal == D("23400.00")
        assert header.grand_total == D("24000.00")

        lines = (
            await session.execute(
                sa.text(
                    "SELECT line_no, freight_allocated, landed_cost_per_unit "
                    "FROM purchase_lines ORDER BY line_no"
                )
            )
        ).all()
        # freight by weight: 500 * 100/140, 500 * 40/140; other by value
        assert lines[0].freight_allocated == D("357.14")
        assert lines[1].freight_allocated == D("142.86")
        # landed = (15000 + 357.14 + 64.10) / 100 and (8400 + 142.86 + 35.90) / 40
        assert lines[0].landed_cost_per_unit == D("154.2124")
        assert lines[1].landed_cost_per_unit == D("214.4690")

        inventory = (
            await session.execute(
                sa.text(
                    "SELECT p.code, i.qty_on_hand, i.weighted_avg_cost FROM inventory i "
                    "JOIN products p ON p.id = i.product_id ORDER BY p.code"
                )
            )
        ).all()
        assert [(r.code, r.qty_on_hand, r.weighted_avg_cost) for r in inventory] == [
            ("MJP", D("40.000"), D("214.4690")),
            ("TRP", D("100.000"), D("154.2124")),
        ]

        debits, credits = (
            await session.execute(sa.text("SELECT SUM(debit), SUM(credit) FROM journal_lines"))
        ).one()
        assert debits == credits == D("24000.00")

        movement_count = (
            await session.execute(sa.text("SELECT count(*) FROM inventory_movements"))
        ).scalar_one()
        assert movement_count == 2

    # session cleared after confirm: next non-command gets unknown-command...
    state = await SessionService(ctx.session_factory).get(ctx.user.org_id, ctx.user.id)
    assert state.is_idle


async def test_several_corrections_in_one_message(ctx: RequestContext) -> None:
    """Corrections arrive one per line, and a bill usually needs several.

    Regression. `_CORRECTION` was matched against the whole message, with
    `$` and no re.MULTILINE, so a two-line message matched nothing at all
    and *both* corrections were dropped. Worse than dropping them, the
    fall-through answered "Reply CONFIRM to save this purchase, or tell
    me what to fix" -- which reads as acknowledgement. Seen live: the
    same pair sent five times, discarded five times, and the bill then
    confirmed carrying the numbers the sender believed they had changed.
    """
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)

    result = await _session_reply("line 1 qty 800\nline 2 qty 40", ctx)
    assert "TRP  800.0 KG" in result.reply
    assert "MJP  40.0 KG" in result.reply

    # One good, one unusable: apply the good one and name the other,
    # above the redrawn bill rather than below the CONFIRM prompt.
    result = await _session_reply("line 1 qty 90\nline 2 qty 8pp", ctx)
    assert "TRP  90.0 KG" in result.reply
    assert "8pp" in result.reply
    assert "is not a number" in result.reply
    assert result.reply.index("8pp") < result.reply.index("Purchase draft")

    # Nothing usable at all: say so. Re-prompting is what made the
    # original bug invisible.
    result = await _session_reply("line 1 quantity 90", ctx)
    assert "didn't change anything" in result.reply
    assert "TRP  90.0 KG" not in result.reply

    await _session_reply("discard", ctx)


async def test_brand_names_match_ignoring_case_and_space(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ "TOP " and "TOP" are one brand. They were two.

    The lookup compared case but not whitespace, and the unique index was
    on the raw name, so both slipped past. The catalogue ended up with
    two brands displaying identically and 26 duplicate products between
    them -- and worse, the "which brand?" question builds its choices
    from a *set* of names, so it offered TOP twice and a sale could take
    the wrong brand's stock without ever really asking.
    """
    from backend.services.purchase_service import PurchaseService

    async with session_factory() as session:
        async with session.begin():
            first = await PurchaseService(session).resolve_or_create_brand(ctx.user.org_id, "TOP")
        first_id = first.id

    for variant in ("TOP ", " top", "  ToP  "):
        async with session_factory() as session:
            async with session.begin():
                again = await PurchaseService(session).resolve_or_create_brand(
                    ctx.user.org_id, variant
                )
            assert again.id == first_id, f"{variant!r} created a second brand"

    async with session_factory() as session:
        rows = (
            await session.execute(sa.text("SELECT name FROM brands WHERE id = :i"), {"i": first_id})
        ).scalar_one()
        assert rows == "TOP"  # stored trimmed, whatever was typed


async def test_one_bill_can_carry_two_brands_for_one_code(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A code under two brands is two products, and one bill can hold both.

    Iqbal Bhai's sheet had 55X under BSQ and 55X under AR on consecutive
    rows. The draft carried a single brand, so the second line resolved
    to the first line's product -- the fuzzy search returned it happily
    once the exact lookup declined -- and the bill had to be entered
    twice, as 007 and 007B, to keep the two apart.
    """
    from backend.services.purchase_service import PurchaseService

    async with session_factory() as session:
        service = PurchaseService(session)
        async with session.begin():
            bsq = await service.resolve_or_create_brand(ctx.user.org_id, "BSQ")
            ar = await service.resolve_or_create_brand(ctx.user.org_id, "AR")
            await service.create_product(ctx.user, "55X", "Zipper sweater", bsq.id)
            await service.create_product(ctx.user, "55X", "Zipper sweater", ar.id)

    result = await handle_purchase(
        "Supplier: Iqbal Bhai Invoice: 1051 Date: 06-08-2026\n55X 800 125\n55X 800 125", ctx
    )
    # Asked, not guessed -- and not offered as a code to create, which
    # would add a third product sharing the code.
    assert "carried by" in result.reply
    assert "AR" in result.reply and "BSQ" in result.reply
    assert "create all products" not in result.reply

    result = await _session_reply("line 1 brand BSQ", ctx)
    assert "carried by" in result.reply  # line 2 still to answer
    result = await _session_reply("line 2 brand AR", ctx)
    assert "carried by" not in result.reply

    result = await _session_reply("create supplier", ctx)
    result = await _session_reply("GST 2240", ctx)
    result = await _session_reply("LBPK 2100", ctx)
    assert "Other charges: ₹4,340.00  (GST ₹2,240.00 + LBPK ₹2,100.00)" in result.reply

    result = await _session_reply("CONFIRM", ctx)
    assert "✅ Purchase confirmed" in result.reply
    assert "Grand total: ₹2,04,340.00" in result.reply

    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT b.name, i.qty_on_hand FROM inventory i "
                    "JOIN products p ON p.id = i.product_id "
                    "JOIN brands b ON b.id = p.brand_id "
                    "WHERE p.code = '55X' ORDER BY b.name"
                )
            )
        ).all()
        # One bill, two brands, 800 each -- not 1,600 under one of them.
        assert [(r[0], r[1]) for r in rows] == [("AR", D("800.000")), ("BSQ", D("800.000"))]


async def test_supplier_and_charges_are_fixable_mid_draft(ctx: RequestContext) -> None:
    """The most prominent name on a bill is often the *buyer's*.

    Iqbal Bhai's book prints "FIROZ-PNP" across the top -- Firoz,
    Panipat, the customer, us -- so whatever is read as the supplier is a
    guess. Neither that nor the charges at the foot of the bill could be
    changed once a draft existed: `_HEADER` and `_LABELED` are reachable
    only from the typed `purchase` text, never from the session an OCR
    draft lives in. The only way out was to discard and start again.
    """
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)

    result = await _session_reply("supplier Iqbal Bhai", ctx)
    assert "Iqbal Bhai" in result.reply

    result = await _session_reply("GST 2240", ctx)
    assert "Other charges: ₹2,240.00" in result.reply

    # A second charge joins it, itemised, rather than replacing it.
    result = await _session_reply("LBPK 2,100", ctx)
    assert "Other charges: ₹4,340.00" in result.reply
    assert "GST ₹2,240.00 + LBPK ₹2,100.00" in result.reply

    # Correcting one re-states that charge instead of adding again.
    result = await _session_reply("GST 2000", ctx)
    assert "Other charges: ₹4,100.00" in result.reply

    # Freight keeps its own field: it is allocated across lines by
    # weight, so it changes landed cost rather than only the total.
    result = await _session_reply("freight 500", ctx)
    assert "Freight: ₹500.00 (allocated by weight)" in result.reply
    assert "Other charges: ₹4,100.00" in result.reply

    # An ordinary item line must not be mistaken for a charge, or the
    # bill quietly grows ₹800 of nothing.
    result = await _session_reply("BSQ 800", ctx)
    assert "Reply CONFIRM" in result.reply
    assert "BSQ" not in result.reply

    await _session_reply("discard", ctx)


async def test_exact_duplicate_blocked(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    await _session_reply("confirm", ctx)

    await handle_purchase(PURCHASE_TEXT, ctx)
    result = await _session_reply("confirm", ctx)
    assert "❌ Invoice INV-4521 from Shree Textiles is already recorded" in result.reply
    await _session_reply("discard", ctx)


async def test_fuzzy_duplicate_warns_and_owner_overrides(
    ctx: RequestContext,
    owner_user: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    await _session_reply("confirm", ctx)

    # same lines/totals, slightly different invoice number -> 2-of-3 signals
    second = PURCHASE_TEXT.replace("INV-4521", "INV-4521A")
    await handle_purchase(second, ctx)
    result = await _session_reply("confirm", ctx)
    assert "looks similar to a purchase already recorded" in result.reply
    assert "Only an owner can confirm" in result.reply  # staff cannot override

    override = await _session_reply("confirm anyway", ctx)
    assert "Only an owner can override" in override.reply
    await _session_reply("discard", ctx)

    owner_ctx = RequestContext(user=owner_user, session_factory=ctx.session_factory)
    await handle_purchase(second, owner_ctx)
    result = await _session_reply("confirm", owner_ctx)
    assert "confirm anyway" in result.reply
    result = await _session_reply("confirm anyway", owner_ctx)
    assert "✅ Purchase confirmed" in result.reply


async def test_total_mismatch_resolution(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await handle_purchase(PURCHASE_TEXT + "\nTotal: 24150", ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    result = await _session_reply("confirm", ctx)
    assert "invoice shows a total of" in result.reply

    result = await _session_reply("use invoice total", ctx)
    result = await _session_reply("confirm", ctx)
    assert "✅ Purchase confirmed" in result.reply

    async with session_factory() as session:
        header = (
            await session.execute(
                sa.text("SELECT grand_total, other_charges, notes FROM purchase_headers")
            )
        ).one()
        assert header.grand_total == D("24150.00")
        assert header.other_charges == D("250.00")  # 100 + 150 reconciliation
        assert header.notes == "reconciled against declared invoice total"


def test_unknown_products_tell_the_user_how_to_create_them() -> None:
    """`create all products` existed from the start but nothing ever
    mentioned it, so a first purchase — where every code is new — read
    as a dead end. Every message that reports unknown codes must name
    the command that resolves them."""
    from backend.api.commands.purchase_commands import unresolved_help

    text = unresolved_help(["35A", "22D", "CPK"])
    assert "create all products" in text
    assert "create product 35A" in text
    # ...and only that command. Telling someone to create the products
    # *and* to reply CONFIRM in the same breath is two instructions of
    # which only one works, with no way to tell which.
    assert "CONFIRM" not in text


def test_preview_does_not_repeat_a_hint_per_unknown_line() -> None:
    """26 unknown codes produced 26 near-identical "reply create
    product X" warnings — a wall of text that buried the actual
    instruction."""
    draft = Draft(
        supplier_id=uuid.uuid4(),
        supplier_name="Wagdia",
        invoice_no="INV-001",
        invoice_date=datetime.date(2026, 7, 26),
        brand_id=None,
        brand_name=None,
        lines=[
            DraftLine(
                code=f"C{n}",
                qty=D("10"),
                rate=D("150"),
                product_id=None,
                resolved_code=None,
                unit_code=None,
                description=f"Item {n}",
            )
            for n in range(26)
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )
    text = render_preview(draft)
    assert text.count("create product") <= 1, "per-line hints should collapse when there are many"
    assert "create all products" in text


def test_preview_still_gives_per_line_hints_for_a_couple_of_unknowns() -> None:
    """With only one or two, the specific hint is genuinely more useful
    than the bulk command."""
    draft = Draft(
        supplier_id=uuid.uuid4(),
        supplier_name="Wagdia",
        invoice_no="INV-002",
        invoice_date=datetime.date(2026, 7, 26),
        brand_id=None,
        brand_name=None,
        lines=[
            DraftLine(
                code="TRP",
                qty=D("10"),
                rate=D("150"),
                product_id=None,
                resolved_code=None,
                unit_code=None,
                description="Jogging Pant",
            )
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )
    text = render_preview(draft)
    assert "create product TRP" in text
    assert "Jogging Pant" in text


def _draft(*, supplier_id: uuid.UUID | None, resolved: bool) -> Draft:
    return Draft(
        supplier_id=supplier_id,
        supplier_name="Iqbal Bhai",
        invoice_no="002",
        invoice_date=datetime.date(2026, 8, 1),
        brand_id=None,
        brand_name=None,
        lines=[
            DraftLine(
                code="028",
                qty=D("1680"),
                rate=D("115"),
                product_id=uuid.uuid4() if resolved else None,
                resolved_code="028" if resolved else None,
                unit_code="KG" if resolved else None,
                description="Children Winter Wear",
            )
        ],
        freight=D("0"),
        other_charges=D("0"),
        declared_total=None,
    )


def test_a_preview_asks_for_exactly_one_thing() -> None:
    """A real sheet produced "reply 'create supplier'", "reply *create
    all products*" and "then reply CONFIRM to save" in one message —
    three instructions, of which only one would work, and nothing to
    say which. The buttons and the words now branch on the same
    decision, so they cannot ask for different things."""
    from backend.api.commands.purchase_commands import next_step, preview_result

    # unknown supplier *and* unknown codes: the codes are asked first
    both = _draft(supplier_id=None, resolved=False)
    assert next_step(both) == "codes"
    result = preview_result(both)
    assert "create all products" in result.reply
    assert "CONFIRM" not in result.reply
    assert "reply 'create supplier'" not in result.reply
    assert isinstance(result.interactive, Buttons)
    assert result.interactive.choices[0].id == "create all products"

    # codes resolved: now the supplier is the one thing left
    supplier_only = _draft(supplier_id=None, resolved=True)
    assert next_step(supplier_only) == "supplier"
    result = preview_result(supplier_only)
    assert "One thing left" in result.reply
    assert "CONFIRM" not in result.reply
    assert isinstance(result.interactive, Buttons)
    assert result.interactive.choices[0].id == "create supplier"

    # nothing blocking: CONFIRM is offered, and only now
    ready = _draft(supplier_id=uuid.uuid4(), resolved=True)
    assert next_step(ready) == "confirm"
    result = preview_result(ready)
    assert "Reply CONFIRM to save" in result.reply
    assert isinstance(result.interactive, Buttons)
    assert result.interactive.choices[0].id == "confirm"


async def test_charge_added_to_a_confirmed_bill_lands_in_the_cost(
    ctx: RequestContext, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """GST on a supplier's bill is part of what the goods cost.

    Iqbal Bhai's 1051 carried BPK 2,100 and GST 2,240 at the foot, which
    the intake never read. With no way to add them afterwards they were
    entered as standalone operating expenses -- so the month looked
    worse while the stock looked cheaper than it was, and the margin on
    those goods was wrong in both directions at once.
    """
    from backend.api.commands.correction_commands import handle_charge

    await handle_purchase(PURCHASE_TEXT, ctx)
    await _session_reply("create supplier", ctx)
    await _session_reply("create product TRP Trouser Poly", ctx)
    await _session_reply("create product MJP Micro Jogging Pants Fabric", ctx)
    await _session_reply("CONFIRM", ctx)

    async with session_factory() as session:
        before = (
            await session.execute(
                sa.text(
                    "SELECT weighted_avg_cost FROM inventory i JOIN products p "
                    "ON p.id = i.product_id WHERE p.code = 'TRP'"
                )
            )
        ).scalar_one()

    result = await handle_charge("INV-4521 GST 2240", ctx)
    assert "GST ₹2,240.00 added to bill INV-4521" in result.reply
    assert "₹24,000.00 → ₹26,240.00" in result.reply
    assert "Now payable: ₹26,240.00" in result.reply

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text("SELECT other_charges, grand_total FROM purchase_headers")
            )
        ).one()
        assert (row.other_charges, row.grand_total) == (D("2340.00"), D("26240.00"))

        after = (
            await session.execute(
                sa.text(
                    "SELECT weighted_avg_cost FROM inventory i JOIN products p "
                    "ON p.id = i.product_id WHERE p.code = 'TRP'"
                )
            )
        ).scalar_one()
        # The stock on hand now carries its share of the charge.
        assert after > before

        # Stock quantity is untouched: the restatement moves value only,
        # so the nightly reconciliation still holds.
        mismatched = (
            await session.execute(
                sa.text(
                    "SELECT p.code FROM products p JOIN inventory i ON i.product_id = p.id "
                    "LEFT JOIN inventory_movements m ON m.product_id = p.id "
                    "GROUP BY p.id, p.code, i.qty_on_hand "
                    "HAVING i.qty_on_hand <> coalesce(sum(m.qty_delta), 0)"
                )
            )
        ).all()
        assert mismatched == []
