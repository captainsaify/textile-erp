"""The Activity page's undo, and the ways it can go wrong.

Every path here dispatches to something that already existed -- the undo
service, the payment reversal, the charge path, the line restores -- so
what these guard is the *dispatch*: that the right inverse is chosen,
that it is refused when it would be dishonest, and that it cannot be run
twice.

The cases worth having are the awkward ones. A test that removes a line
and puts it back proves the easy half; the half that matters is what
happens when the stock has since been sold, when the same undo is
clicked twice, or when an edit changed a field *and* removed a line and
only one of them can be put back.
"""

from __future__ import annotations

import decimal
import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.security import hash_password
from backend.main import create_app
from backend.models import User
from backend.models.enums import UserRole
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

ORG = uuid.UUID(SEEDED_ORG_ID)
PASSWORD = "correct-horse-battery-staple"
CONTROL_PASSWORD = "a-long-generated-secret"
D = decimal.Decimal


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    import backend.api.deps as deps

    monkeypatch.setattr(deps, "get_session_factory", lambda: session_factory)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


@pytest.fixture
async def owner(session_factory: async_sessionmaker[AsyncSession]) -> User:
    async with session_factory() as session, session.begin():
        user = User(
            org_id=ORG,
            full_name="Undo Owner",
            email=f"undo-{uuid.uuid4().hex[:6]}@example.com",
            password_hash=hash_password(PASSWORD),
            control_password_hash=hash_password(CONTROL_PASSWORD),
            role=UserRole.OWNER,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def control(client: AsyncClient, owner: User) -> dict[str, str]:
    signin = await client.post(
        "/api/v1/auth/control/login",
        json={"email": owner.email, "password": CONTROL_PASSWORD},
    )
    assert signin.status_code == 200, signin.text
    return {"Authorization": f"Bearer {signin.json()['token']}"}


@pytest.fixture
async def code(owner: User, session_factory: async_sessionmaker[AsyncSession]) -> str:
    from backend.models import Brand, Product, ProductType, Supplier

    label = f"U{uuid.uuid4().hex[:4].upper()}"
    async with session_factory() as session, session.begin():
        ptype = (
            (await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG)))
            .scalars()
            .first()
        )
        assert ptype is not None
        brand = Brand(org_id=ORG, name=f"BR{label}")
        session.add_all([brand, Supplier(org_id=ORG, name=f"Supp {label}", created_by=owner.id)])
        await session.flush()
        session.add(
            Product(
                org_id=ORG,
                product_type_id=ptype.id,
                code=label,
                description="Undo probe",
                unit_id=ptype.default_unit_id,
                brand_id=brand.id,
                created_by=owner.id,
            )
        )
    return label


async def _bill(client: AsyncClient, headers: dict[str, str], code: str, lines: int = 2) -> str:
    invoice = f"U-{uuid.uuid4().hex[:5]}"
    body = {
        "supplier": f"Supp {code}",
        "invoice_no": invoice,
        "invoice_date": "2026-08-14",
        "lines": [
            {"code": code, "qty": "800", "rate": "120"},
            {"code": code, "qty": "400", "rate": "120"},
        ][:lines],
    }
    made = await client.post("/api/v1/control/purchases", headers=headers, json=body)
    assert made.status_code == 201, made.text
    return invoice


async def _activity(client: AsyncClient, headers: dict[str, str], action: str) -> dict[str, object]:
    listed = await client.get("/api/v1/control/activity", headers=headers)
    assert listed.status_code == 200, listed.text
    for item in listed.json()["items"]:
        if item["action"] == action:
            return item
    raise AssertionError(
        f"no {action} in activity: {[i['action'] for i in listed.json()['items']]}"
    )


async def _stock(session_factory: async_sessionmaker[AsyncSession], code: str) -> decimal.Decimal:
    async with session_factory() as session:
        value = (
            await session.execute(
                sa.text(
                    "SELECT qty_on_hand FROM inventory i JOIN products p ON p.id = i.product_id "
                    "WHERE p.code = :c"
                ),
                {"c": code},
            )
        ).scalar_one_or_none()
    return value if value is not None else D("0")


# --------------------------------------------------------------------
# the dispatch picks the right inverse
# --------------------------------------------------------------------


async def test_undoing_a_field_change_puts_the_quantity_and_the_stock_back(
    client: AsyncClient,
    control: dict[str, str],
    code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The case that needed the audit to record numbers rather than
    sentences: nobody has to remember what the quantity was."""
    invoice = await _bill(client, control, code)
    before = await _stock(session_factory, code)

    edited = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=control,
        json={"lines": [{"line_no": 1, "code": code, "qty": "960", "rate": "120"}]},
    )
    assert edited.status_code == 200, edited.text
    assert await _stock(session_factory, code) == before + D("160")

    item = await _activity(client, control, "purchase.edited")
    assert item["undo"] == "restore_lines"
    undone = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert undone.status_code == 200, undone.text

    assert await _stock(session_factory, code) == before
    async with session_factory() as session:
        qty = (
            await session.execute(
                sa.text(
                    "SELECT pl.qty FROM purchase_lines pl JOIN purchase_headers ph "
                    "ON ph.id = pl.purchase_header_id WHERE ph.invoice_no = :i AND pl.line_no = 1"
                ),
                {"i": invoice},
            )
        ).scalar_one()
    assert qty == D("800.000")


async def test_an_edit_that_removed_a_line_and_changed_a_field_undoes_both(
    client: AsyncClient,
    control: dict[str, str],
    code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Lines first, then fields -- a quantity is put back on the line it
    belongs to, and that line has to exist again first."""
    invoice = await _bill(client, control, code)
    before = await _stock(session_factory, code)

    edited = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=control,
        json={
            "lines": [
                {"line_no": 1, "code": code, "qty": "960", "rate": "120"},
                {"line_no": 2, "code": code, "qty": "400", "rate": "120", "removed": True},
            ]
        },
    )
    assert edited.status_code == 200, edited.text

    item = await _activity(client, control, "purchase.edited")
    undone = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert undone.status_code == 200, undone.text

    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT pl.line_no, pl.qty FROM purchase_lines pl JOIN purchase_headers ph "
                    "ON ph.id = pl.purchase_header_id WHERE ph.invoice_no = :i ORDER BY pl.line_no"
                ),
                {"i": invoice},
            )
        ).all()
    assert [(r.line_no, r.qty) for r in rows] == [(1, D("800.000")), (2, D("400.000"))]
    assert await _stock(session_factory, code) == before


async def test_the_same_undo_cannot_be_run_twice(
    client: AsyncClient,
    control: dict[str, str],
    code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Otherwise a double tap on a slow connection puts a line back
    twice, and the bill carries stock that never arrived."""
    invoice = await _bill(client, control, code)
    await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=control,
        json={
            "lines": [
                {"line_no": 1, "code": code, "qty": "800", "rate": "120"},
                {"line_no": 2, "code": code, "qty": "400", "rate": "120", "removed": True},
            ]
        },
    )
    item = await _activity(client, control, "purchase.edited")
    first = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert first.status_code == 200, first.text

    again = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert again.status_code >= 400, "an undo that has already run must be refused"

    async with session_factory() as session:
        count = (
            await session.execute(
                sa.text(
                    "SELECT count(*) FROM purchase_lines pl JOIN purchase_headers ph "
                    "ON ph.id = pl.purchase_header_id WHERE ph.invoice_no = :i"
                ),
                {"i": invoice},
            )
        ).scalar_one()
    assert count == 2, "the line came back twice"


async def test_an_undone_row_is_marked_so_the_page_stops_offering_it(
    client: AsyncClient, control: dict[str, str], code: str
) -> None:
    invoice = await _bill(client, control, code)
    await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=control,
        json={"lines": [{"line_no": 1, "code": code, "qty": "900", "rate": "120"}]},
    )
    item = await _activity(client, control, "purchase.edited")
    await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)

    # By id, not by action: undoing an edit *writes another edit*, which
    # is honest -- it is a change too -- so "the newest bill correction"
    # is now the undo itself rather than the thing that was undone.
    listed = await client.get("/api/v1/control/activity", headers=control)
    original = next(row for row in listed.json()["items"] if row["id"] == item["id"])
    assert original["undone"] is True


async def test_undoing_a_payment_reversal_records_the_payment_again(
    client: AsyncClient,
    control: dict[str, str],
    code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The reversal was the mistake. Undoing it is the payment happening
    again -- a new entry with its own reference, not the old one
    un-cancelled."""
    invoice = await _bill(client, control, code)
    paid = await client.post(
        "/api/v1/control/pay",
        headers=control,
        json={"party": f"Supp {code}", "amount": "5000", "via": "cash"},
    )
    assert paid.status_code == 201, paid.text
    reference = paid.json()["reference"]

    reversed_response = await client.post(
        f"/api/v1/control/payments/{reference}/reverse", headers=control
    )
    assert reversed_response.status_code == 200, reversed_response.text

    async with session_factory() as session:
        after_reversal = (
            await session.execute(
                sa.text("SELECT amount_paid FROM purchase_headers WHERE invoice_no = :i"),
                {"i": invoice},
            )
        ).scalar_one()
    assert after_reversal == D("0.00")

    item = await _activity(client, control, "payment.reversed")
    assert item["undo"] == "unreverse"
    undone = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert undone.status_code == 200, undone.text

    async with session_factory() as session:
        settled = (
            await session.execute(
                sa.text("SELECT amount_paid FROM purchase_headers WHERE invoice_no = :i"),
                {"i": invoice},
            )
        ).scalar_one()
    assert settled == D("5000.00"), "the money should be back on the bill"


async def test_undoing_a_rename_uses_the_name_the_audit_kept(
    client: AsyncClient,
    control: dict[str, str],
    code: str,
    session_factory: async_sessionmaker[AsyncSession],
    owner: User,
) -> None:
    from backend.services.admin.products import ProductAdminService

    async with session_factory() as session:
        await ProductAdminService(session).describe(
            ORG, owner, code=code, brand=None, description="RENAMED BY MISTAKE"
        )

    item = await _activity(client, control, "product.described")
    assert item["undo"] == "rename"
    undone = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert undone.status_code == 200, undone.text

    async with session_factory() as session:
        description = (
            await session.execute(
                sa.text("SELECT description FROM products WHERE code = :c"), {"c": code}
            )
        ).scalar_one()
    assert description == "Undo probe"


async def test_undoing_an_expense_goes_through_the_undo_service(
    client: AsyncClient,
    control: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Wired, not reimplemented: the service already reverses an expense
    with compensating entries."""
    recorded = await client.post(
        "/api/v1/control/expenses",
        headers=control,
        json={"category": "freight", "amount": "1200", "via": "cash"},
    )
    assert recorded.status_code == 201, recorded.text

    item = await _activity(client, control, "expense.created")
    assert item["undo"] == "undo:expense"
    undone = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert undone.status_code == 200, undone.text

    async with session_factory() as session:
        rows = (await session.execute(sa.text("SELECT count(*) FROM cash_ledger"))).scalar_one()
    assert rows >= 2, "the reversal is a compensating entry, not a deletion"


async def test_something_with_no_honest_inverse_is_refused_by_name(
    client: AsyncClient, control: dict[str, str], code: str
) -> None:
    """A rate correction has no recorded before-value of its own, so the
    page says so rather than offering a button that half-works."""
    invoice = await _bill(client, control, code, lines=1)
    fixed = await client.post(
        "/api/v1/control/purchases/fix-line",
        headers=control,
        json={"invoice_no": invoice, "line_no": 1, "rate": "130"},
    )
    assert fixed.status_code in (200, 201), fixed.text

    item = await _activity(client, control, "purchase.rate_corrected")
    assert item["undo"] == ""
    refused = await client.post(f"/api/v1/control/activity/{item['id']}/undo", headers=control)
    assert refused.status_code == 400
    assert "cannot be taken back" in refused.text


async def test_an_unknown_reference_is_a_404_not_a_crash(
    client: AsyncClient, control: dict[str, str]
) -> None:
    response = await client.post("/api/v1/control/activity/deadbeef/undo", headers=control)
    assert response.status_code == 404


async def test_undo_needs_the_control_credential(
    client: AsyncClient, owner: User, control: dict[str, str], code: str
) -> None:
    """It moves money and stock, so the dashboard token must not reach
    it -- the check that matters most on the whole page."""
    invoice = await _bill(client, control, code)
    await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=control,
        json={"lines": [{"line_no": 1, "code": code, "qty": "900", "rate": "120"}]},
    )
    item = await _activity(client, control, "purchase.edited")

    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}
    )
    dashboard = {"Authorization": f"Bearer {login.json()['access_token']}"}
    for path in ("/api/v1/control/activity", f"/api/v1/control/activity/{item['id']}/undo"):
        response = (
            await client.post(path, headers=dashboard)
            if path.endswith("undo")
            else await client.get(path, headers=dashboard)
        )
        assert response.status_code == 401, path
