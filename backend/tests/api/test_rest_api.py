"""REST API -- docs/10_API.md.

The tests that matter here are the authorisation boundaries. Every
repository query already filters by org, so these check the layer that
makes tampering structurally impossible rather than merely unlikely:
`org_id` comes from the token, a refresh token can't be used as an
access token, and owner-only figures stay owner-only.
"""

from __future__ import annotations

import datetime
import decimal
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.security import hash_password
from backend.main import create_app
from backend.models import Organization, User
from backend.models.enums import UserRole
from backend.tests.conftest import (
    SEEDED_MAIN_WAREHOUSE_ID,
    SEEDED_ORG_ID,
    purge_business_rows,
)

ORG = uuid.UUID(SEEDED_ORG_ID)
PASSWORD = "correct-horse-battery-staple"
ZERO = decimal.Decimal("0")


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    yield
    await purge_business_rows(session_factory)


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[AsyncClient]:
    """The app resolves its own session factory, so point it at the test
    database rather than the developer's."""
    import backend.api.deps as deps

    monkeypatch.setattr(deps, "get_session_factory", lambda: session_factory)
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def _make_login_user(
    session_factory: async_sessionmaker[AsyncSession], *, role: UserRole, email: str
) -> User:
    async with session_factory() as session, session.begin():
        user = User(
            org_id=ORG,
            full_name=f"{role.value.title()} Probe",
            email=email,
            password_hash=hash_password(PASSWORD),
            role=role,
        )
        session.add(user)
        await session.flush()
        await session.refresh(user)
        return user


@pytest.fixture
async def owner(session_factory: async_sessionmaker[AsyncSession]) -> User:
    return await _make_login_user(
        session_factory, role=UserRole.OWNER, email=f"owner-{uuid.uuid4().hex[:6]}@example.com"
    )


@pytest.fixture
async def staff(session_factory: async_sessionmaker[AsyncSession]) -> User:
    return await _make_login_user(
        session_factory, role=UserRole.STAFF, email=f"staff-{uuid.uuid4().hex[:6]}@example.com"
    )


async def _token(client: AsyncClient, user: User) -> str:
    response = await client.post(
        "/api/v1/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return str(response.json()["access_token"])


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------


async def test_login_returns_a_token_pair(client: AsyncClient, owner: User) -> None:
    response = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["access_token"] and body["refresh_token"]
    assert body["role"] == "owner"
    assert body["expires_in"] == 15 * 60  # short by design for a financial system


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: AsyncClient, owner: User
) -> None:
    """Different answers would confirm which addresses have accounts."""
    wrong = await client.post("/api/v1/auth/login", json={"email": owner.email, "password": "nope"})
    missing = await client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "nope"}
    )
    assert wrong.status_code == missing.status_code == 401
    assert wrong.json()["error"]["message"] == missing.json()["error"]["message"]


async def test_whatsapp_only_account_cannot_sign_in(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """password_hash NULL means no dashboard access — WhatsApp identity
    does not imply a login (docs/10_API.md §3)."""
    async with session_factory() as session, session.begin():
        session.add(
            User(
                org_id=ORG,
                full_name="WhatsApp Only",
                email="wa-only@example.com",
                whatsapp_number=f"+9199{uuid.uuid4().hex[:8]}",
                role=UserRole.STAFF,
            )
        )
    response = await client.post(
        "/api/v1/auth/login", json={"email": "wa-only@example.com", "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_endpoints_require_a_token(client: AsyncClient) -> None:
    response = await client.get("/api/v1/dashboard")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthenticated"


async def test_a_refresh_token_is_not_accepted_as_an_access_token(
    client: AsyncClient, owner: User
) -> None:
    """It lives far longer, so accepting it would silently extend every
    session to the refresh lifetime."""
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]
    response = await client.get("/api/v1/dashboard", headers=_auth(refresh_token))
    assert response.status_code == 401


async def test_refresh_issues_a_new_access_token(client: AsyncClient, owner: User) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}
    )
    response = await client.post(
        "/api/v1/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )
    assert response.status_code == 200
    assert await _dashboard_ok(client, response.json()["access_token"])


async def _dashboard_ok(client: AsyncClient, token: str) -> bool:
    return (await client.get("/api/v1/dashboard", headers=_auth(token))).status_code == 200


async def test_logout_revokes_the_refresh_token(client: AsyncClient, owner: User) -> None:
    login = await client.post(
        "/api/v1/auth/login", json={"email": owner.email, "password": PASSWORD}
    )
    refresh_token = login.json()["refresh_token"]
    assert (
        await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    ).status_code == 204

    again = await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert again.status_code == 401
    assert "logged out" in again.json()["error"]["message"]


async def test_a_tampered_token_is_rejected(client: AsyncClient, owner: User) -> None:
    token = await _token(client, owner)
    forged = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    assert (await client.get("/api/v1/dashboard", headers=_auth(forged))).status_code == 401


async def test_me_reports_the_signed_in_account(client: AsyncClient, staff: User) -> None:
    token = await _token(client, staff)
    body = (await client.get("/api/v1/auth/me", headers=_auth(token))).json()
    assert body["role"] == "staff"
    assert body["org_id"] == str(ORG)


# --------------------------------------------------------------------
# authorisation
# --------------------------------------------------------------------


async def test_profit_is_owner_only(client: AsyncClient, staff: User, owner: User) -> None:
    """Margin is partner-level information (docs/14_Security.md #rbac)."""
    staff_response = await client.get(
        "/api/v1/reports/profit-loss", headers=_auth(await _token(client, staff))
    )
    assert staff_response.status_code == 403
    assert staff_response.json()["error"]["code"] == "forbidden"

    owner_response = await client.get(
        "/api/v1/reports/profit-loss", headers=_auth(await _token(client, owner))
    )
    assert owner_response.status_code == 200


async def test_dashboard_omits_partner_capital_for_staff(
    client: AsyncClient, staff: User, owner: User
) -> None:
    staff_body = (
        await client.get("/api/v1/dashboard", headers=_auth(await _token(client, staff)))
    ).json()
    owner_body = (
        await client.get("/api/v1/dashboard", headers=_auth(await _token(client, owner)))
    ).json()
    assert "partner_capital" not in staff_body  # absent, not null
    assert "partner_capital" in owner_body


async def test_org_scope_comes_from_the_token_not_the_url(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A second org's purchase must be invisible even with its real id."""
    from backend.models import PurchaseHeader, Supplier, Warehouse

    async with session_factory() as session, session.begin():
        other_org = Organization(name="Someone Else Ltd")
        session.add(other_org)
        await session.flush()
        actor = User(
            org_id=other_org.id,
            full_name="Other Owner",
            email=f"other-{uuid.uuid4().hex[:6]}@example.com",
            role=UserRole.OWNER,
        )
        session.add(actor)
        await session.flush()
        warehouse = Warehouse(org_id=other_org.id, name="Other WH", is_default=True)
        supplier = Supplier(org_id=other_org.id, name="Other Supp", created_by=actor.id)
        session.add_all([warehouse, supplier])
        await session.flush()
        foreign = PurchaseHeader(
            org_id=other_org.id,
            supplier_id=supplier.id,
            warehouse_id=warehouse.id,
            invoice_no="OTHER-1",
            invoice_date=datetime.date.today(),
            grand_total=1,
            status="confirmed",
            created_by=actor.id,
        )
        session.add(foreign)
        await session.flush()
        foreign_id = foreign.id
        other_org_id = other_org.id

    token = await _token(client, owner)
    response = await client.get(f"/api/v1/purchases/{foreign_id}", headers=_auth(token))
    assert response.status_code == 404, "another org's record must not be readable"

    listing = (await client.get("/api/v1/purchases", headers=_auth(token))).json()
    assert all(item["invoice_no"] != "OTHER-1" for item in listing["items"])

    # organizations aren't in the purge order (there is normally exactly
    # one), so this test removes the one it invented
    import sqlalchemy as sa

    async with session_factory() as session, session.begin():
        for table in ("purchase_headers", "suppliers", "warehouses", "users"):
            await session.execute(
                sa.text(f"DELETE FROM {table} WHERE org_id = :oid"), {"oid": other_org_id}
            )
        await session.execute(
            sa.text("DELETE FROM organizations WHERE id = :oid"), {"oid": other_org_id}
        )


# --------------------------------------------------------------------
# shape and behaviour
# --------------------------------------------------------------------


async def test_errors_use_the_documented_envelope(client: AsyncClient, owner: User) -> None:
    token = await _token(client, owner)
    response = await client.get("/api/v1/purchases/not-a-uuid", headers=_auth(token))
    assert response.status_code == 404
    body = response.json()
    assert set(body["error"]) >= {"code", "message"}


async def test_money_is_serialised_as_strings_not_floats(client: AsyncClient, owner: User) -> None:
    """A float would lose precision on the way to the browser, which is
    the one place this system has been careful about all along."""
    body = (
        await client.get("/api/v1/dashboard", headers=_auth(await _token(client, owner)))
    ).json()
    assert isinstance(body["cash_balance"], str)
    assert isinstance(body["inventory"]["value"], str)
    assert isinstance(body["month_profit"]["net_profit"], str)


async def test_pagination_rejects_an_unusable_cursor(client: AsyncClient, owner: User) -> None:
    token = await _token(client, owner)
    response = await client.get("/api/v1/purchases?cursor=!!!not-base64", headers=_auth(token))
    assert response.status_code in {200, 400}


async def test_limit_is_capped(client: AsyncClient, owner: User) -> None:
    token = await _token(client, owner)
    response = await client.get("/api/v1/purchases?limit=5000", headers=_auth(token))
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "validation_error"


async def test_openapi_schema_is_generated(client: AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    for path in ("/api/v1/auth/login", "/api/v1/dashboard", "/api/v1/purchases"):
        assert path in paths


# --------------------------------------------------------------------
# reconciliation: acknowledging a mismatch
# --------------------------------------------------------------------


async def _mismatch_run(session_factory: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    from backend.models import ReconciliationRun

    async with session_factory() as session:
        run = ReconciliationRun(
            org_id=ORG,
            kind="inventory",
            status="mismatch",
            checked_count=3,
            mismatch_count=1,
            details=[{"subject": "TRP", "expected": "100", "actual": "90"}],
        )
        session.add(run)
        await session.commit()
        await session.refresh(run)
        return run.id


async def test_unacknowledged_mismatches_are_listed_for_the_dashboard(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    run_id = await _mismatch_run(session_factory)
    token = await _token(client, owner)

    response = await client.get(
        "/api/v1/inventory/reconciliations?unacknowledged=true", headers=_auth(token)
    )

    assert response.status_code == 200, response.text
    ids = [row["id"] for row in response.json()["data"]]
    assert str(run_id) in ids


async def test_acknowledging_records_who_looked_and_when(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Acknowledging is not fixing. The mismatch stays on the row; what
    is recorded is that a person saw it (docs/03_Inventory.md §6)."""
    run_id = await _mismatch_run(session_factory)
    token = await _token(client, owner)

    response = await client.post(
        f"/api/v1/inventory/reconcile/{run_id}/acknowledge", headers=_auth(token)
    )

    assert response.status_code == 200, response.text
    body = response.json()["data"]
    assert body["acknowledged_at"] is not None
    assert body["acknowledged_by"] == str(owner.id)
    # the discrepancy itself is untouched -- a job that repaired the
    # number would destroy the evidence something upstream is broken
    assert body["mismatch_count"] == 1
    assert body["status"] == "mismatch"

    listed = await client.get(
        "/api/v1/inventory/reconciliations?unacknowledged=true", headers=_auth(token)
    )
    assert str(run_id) not in [row["id"] for row in listed.json()["data"]]


async def test_acknowledging_twice_keeps_the_first_timestamp(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """When it was *first* seen is the useful fact; moving it on every
    click would erase how long it went unnoticed."""
    run_id = await _mismatch_run(session_factory)
    token = await _token(client, owner)

    first = await client.post(
        f"/api/v1/inventory/reconcile/{run_id}/acknowledge", headers=_auth(token)
    )
    second = await client.post(
        f"/api/v1/inventory/reconcile/{run_id}/acknowledge", headers=_auth(token)
    )

    assert second.json()["already_acknowledged"] is True
    assert second.json()["data"]["acknowledged_at"] == first.json()["data"]["acknowledged_at"]


async def test_staff_cannot_acknowledge_a_mismatch(
    client: AsyncClient, staff: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Someone is taking responsibility for having looked, so it has to
    be attributable to an owner."""
    run_id = await _mismatch_run(session_factory)
    token = await _token(client, staff)

    response = await client.post(
        f"/api/v1/inventory/reconcile/{run_id}/acknowledge", headers=_auth(token)
    )

    assert response.status_code == 403, response.text


async def test_acknowledging_another_orgs_run_is_a_404(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.models import ReconciliationRun

    async with session_factory() as session:
        other_org = Organization(name=f"Other {uuid.uuid4().hex[:6]}")
        session.add(other_org)
        await session.flush()
        run = ReconciliationRun(
            org_id=other_org.id, kind="inventory", status="mismatch", mismatch_count=1
        )
        session.add(run)
        await session.commit()
        run_id = run.id

    token = await _token(client, owner)
    response = await client.post(
        f"/api/v1/inventory/reconcile/{run_id}/acknowledge", headers=_auth(token)
    )

    assert response.status_code == 404, response.text


async def test_requesting_an_export_over_http_actually_enqueues_it(
    client: AsyncClient, owner: User
) -> None:
    """Untested until now, and broken: the handler read the org's date
    through the request session and then opened `session.begin()`, which
    raises once anything has autobegun -- and authenticating the request
    always has."""
    token = await _token(client, owner)

    response = await client.post(
        "/api/v1/reports/export", json={"type": "purchases"}, headers=_auth(token)
    )

    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    polled = await client.get(f"/api/v1/reports/export/{job_id}", headers=_auth(token))
    assert polled.status_code == 200, polled.text
    assert polled.json()["status"] in {"queued", "generating", "ready"}
    assert polled.json()["type"] == "purchases"


async def test_an_unknown_report_type_is_rejected(client: AsyncClient, owner: User) -> None:
    token = await _token(client, owner)
    response = await client.post(
        "/api/v1/reports/export", json={"type": "everything"}, headers=_auth(token)
    )
    assert response.status_code == 400, response.text


# --------------------------------------------------------------------
# what the web dashboard reads
# --------------------------------------------------------------------


async def test_monthly_metrics_never_plots_a_partial_month_as_a_full_one(
    client: AsyncClient, owner: User
) -> None:
    """The current month is month-to-date. Plotting it beside complete
    months as if it were complete is how a trend chart invents a
    downturn every 1st of the month."""
    import datetime

    token = await _token(client, owner)
    response = await client.get("/api/v1/metrics/monthly?months=3", headers=_auth(token))

    assert response.status_code == 200, response.text
    points = response.json()["data"]
    assert len(points) == 3
    # oldest first, so a chart can plot them left to right without sorting
    assert points[0]["month"] < points[-1]["month"]
    assert points[-1]["month"] == datetime.date.today().strftime("%Y-%m")
    # money crosses the wire as strings; a float here would undo the
    # NUMERIC discipline everywhere else (docs/21 §5)
    assert isinstance(points[0]["net_profit"], str)


async def test_monthly_metrics_are_owner_only(client: AsyncClient, staff: User) -> None:
    token = await _token(client, staff)
    response = await client.get("/api/v1/metrics/monthly", headers=_auth(token))
    assert response.status_code == 403, response.text


async def test_the_audit_log_is_owner_only(client: AsyncClient, staff: User) -> None:
    token = await _token(client, staff)
    assert (await client.get("/api/v1/audit", headers=_auth(token))).status_code == 403


async def test_receivables_and_payables_list_who_owes_what(
    client: AsyncClient, owner: User
) -> None:
    token = await _token(client, owner)

    for path in ("/api/v1/receivables", "/api/v1/payables"):
        response = await client.get(path, headers=_auth(token))
        assert response.status_code == 200, response.text
        assert isinstance(response.json()["data"], list)


async def test_a_purchase_without_a_scan_says_so_rather_than_404ing_the_page(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The detail page asks for a scan url before deciding what to draw;
    a purchase typed in by hand simply has none."""
    import datetime
    import decimal
    import uuid as uuid_module

    from backend.models import PurchaseHeader, Supplier
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    async with session_factory() as session:
        supplier = Supplier(
            org_id=ORG, name=f"Typed {uuid_module.uuid4().hex[:6]}", created_by=owner.id
        )
        session.add(supplier)
        await session.flush()
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            invoice_no=f"TYPED-{uuid_module.uuid4().hex[:5]}",
            invoice_date=datetime.date.today(),
            grand_total=decimal.Decimal("100"),
            status="confirmed",
            created_by=owner.id,
        )
        session.add(header)
        await session.commit()
        purchase_id = header.id

    token = await _token(client, owner)
    detail = await client.get(f"/api/v1/purchases/{purchase_id}", headers=_auth(token))

    assert detail.status_code == 200, detail.text
    assert detail.json()["has_scan"] is False
    assert detail.json()["scan_url"] is None
    assert detail.json()["supplier"].startswith("Typed")

    scan = await client.get(f"/api/v1/purchases/{purchase_id}/scan", headers=_auth(token))
    assert scan.status_code == 404


async def test_stock_carries_the_brand_because_codes_are_brand_scoped(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`products_org_code_active_uq` makes a code unique only within a
    brand. A stock list without the brand shows two identical codes and
    no way to tell which is which."""
    import decimal
    import uuid as uuid_module

    from backend.models import Brand, Inventory, Product
    from backend.tests.conftest import SEEDED_KG_UNIT_ID, SEEDED_TEXTILE_TYPE_ID

    suffix = uuid_module.uuid4().hex[:6]
    code = f"VVP{suffix.upper()}"

    async with session_factory() as session:
        brand = Brand(org_id=ORG, name=f"Alpha{suffix}")
        session.add(brand)
        await session.flush()
        for brand_id in (brand.id, None):
            product = Product(
                org_id=ORG,
                product_type_id=uuid_module.UUID(SEEDED_TEXTILE_TYPE_ID),
                code=code,
                brand_id=brand_id,
                description="Golden Velvet Pant",
                unit_id=uuid_module.UUID(SEEDED_KG_UNIT_ID),
                created_by=owner.id,
            )
            session.add(product)
            await session.flush()
            session.add(
                Inventory(
                    org_id=ORG,
                    product_id=product.id,
                    warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                    qty_on_hand=decimal.Decimal("10"),
                    weighted_avg_cost=decimal.Decimal("150"),
                )
            )
        await session.commit()

    token = await _token(client, owner)
    response = await client.get("/api/v1/inventory", headers=_auth(token))

    assert response.status_code == 200, response.text
    same_code = [row for row in response.json()["items"] if row["code"] == code]
    assert len(same_code) == 2
    # one carries the brand, the other records that it has none -- the
    # two rows are distinguishable, which is the whole point
    assert sorted(str(row["brand"]) for row in same_code) == [f"Alpha{suffix}", "None"]


async def test_the_audit_log_says_what_actually_changed(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Twenty rows reading "product.created" answer nothing. The payload
    that names the product is written on every mutation already -- it
    just wasn't being returned."""
    from backend.models import AuditLog

    async with session_factory() as session:
        session.add(
            AuditLog(
                org_id=ORG,
                actor_user_id=owner.id,
                action="product.created",
                entity_type="products",
                entity_id=uuid.uuid4(),
                after_state={"code": "TRP", "description": "Jogging Pant", "brand_id": None},
                channel="whatsapp",
            )
        )
        await session.commit()

    token = await _token(client, owner)
    response = await client.get("/api/v1/audit?limit=5", headers=_auth(token))

    assert response.status_code == 200, response.text
    entry = next(row for row in response.json()["data"] if row["action"] == "product.created")
    assert entry["after_state"]["code"] == "TRP"
    assert entry["after_state"]["description"] == "Jogging Pant"
    assert "entity_id" in entry

    filtered = await client.get("/api/v1/audit?action=product.created", headers=_auth(token))
    assert {row["action"] for row in filtered.json()["data"]} == {"product.created"}

    actions = await client.get("/api/v1/audit/actions", headers=_auth(token))
    assert actions.status_code == 200, actions.text
    assert any(row["action"] == "product.created" for row in actions.json()["data"])


# --------------------------------------------------------------------
# parties -- docs/21 §2, the Parties and Ledger tabs
# --------------------------------------------------------------------


async def test_parties_lists_everyone_not_only_those_who_owe(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """`receivables`/`payables` only ever list a party who owes money,
    so a supplier you had paid off could not be looked up at all."""
    import uuid as uuid_module

    from backend.models import Customer, Supplier

    suffix = uuid_module.uuid4().hex[:6]
    async with session_factory() as session:
        session.add_all(
            [
                Supplier(org_id=ORG, name=f"Settled Up {suffix}", created_by=owner.id),
                Customer(org_id=ORG, name=f"Never Traded {suffix}", created_by=owner.id),
            ]
        )
        await session.commit()

    token = await _token(client, owner)
    response = await client.get("/api/v1/parties", headers=_auth(token))

    assert response.status_code == 200, response.text
    rows = {row["name"]: row for row in response.json()["data"]}
    assert rows[f"Settled Up {suffix}"]["kind"] == "supplier"
    assert rows[f"Never Traded {suffix}"]["kind"] == "customer"
    # settled up is a fact, not an absence
    assert rows[f"Settled Up {suffix}"]["outstanding"] == "0.00"


async def test_a_party_ledger_runs_a_balance_over_bills_and_payments(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    import datetime
    import decimal
    import uuid as uuid_module

    from backend.models import PurchaseHeader, Supplier
    from backend.services.settlement_service import SettlementService
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    suffix = uuid_module.uuid4().hex[:6]
    async with session_factory() as session:
        supplier = Supplier(org_id=ORG, name=f"Ledger Co {suffix}", created_by=owner.id)
        session.add(supplier)
        await session.flush()
        session.add(
            PurchaseHeader(
                org_id=ORG,
                supplier_id=supplier.id,
                warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                invoice_no=f"LED-{suffix}",
                invoice_date=datetime.date.today(),
                subtotal=decimal.Decimal("10000"),
                grand_total=decimal.Decimal("10000"),
                status="confirmed",
                created_by=owner.id,
            )
        )
        supplier_id = supplier.id
        await session.commit()

    async with session_factory() as session:
        await SettlementService(session).pay_supplier(
            owner, supplier_name=f"Ledger Co {suffix}", amount=decimal.Decimal("4000"), via="cash"
        )

    token = await _token(client, owner)
    response = await client.get(
        f"/api/v1/parties/supplier/{supplier_id}/ledger", headers=_auth(token)
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["balance"] == "6000.00"
    kinds = [row["kind"] for row in payload["data"]]
    assert kinds == ["Purchase", "Payment (cash)"]
    assert payload["data"][-1]["balance"] == "6000.00"


async def test_a_party_ledger_for_the_wrong_kind_is_a_404(client: AsyncClient, owner: User) -> None:
    import uuid as uuid_module

    token = await _token(client, owner)
    response = await client.get(
        f"/api/v1/parties/partner/{uuid_module.uuid4()}/ledger", headers=_auth(token)
    )
    assert response.status_code == 404


# --------------------------------------------------------------------
# documents -- docs/27_Documents.md
# --------------------------------------------------------------------


async def test_a_purchase_serves_its_current_sheet(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Built on request, not stored: a bill whose rate was corrected has
    one current version, and a file written at confirmation time would
    hand back the superseded one."""
    import datetime
    import decimal
    import uuid as uuid_module

    from backend.models import PurchaseHeader, Supplier
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    suffix = uuid_module.uuid4().hex[:5]
    async with session_factory() as session:
        supplier = Supplier(org_id=ORG, name=f"Doc Co {suffix}", created_by=owner.id)
        session.add(supplier)
        await session.flush()
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            invoice_no=f"DOC-{suffix}",
            invoice_date=datetime.date.today(),
            subtotal=decimal.Decimal("5000"),
            grand_total=decimal.Decimal("5000"),
            status="confirmed",
            created_by=owner.id,
        )
        session.add(header)
        await session.commit()
        header_id = header.id

    token = await _token(client, owner)
    response = await client.get(f"/api/v1/purchases/{header_id}/sheet", headers=_auth(token))

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(
        "application/vnd.openxmlformats-officedocument"
    )
    assert f"DOC-{suffix}" in response.headers["content-disposition"]
    assert response.content[:2] == b"PK"  # a real xlsx, not an error page


async def test_a_missing_document_is_a_404_not_a_broken_file(
    client: AsyncClient, owner: User
) -> None:
    import uuid as uuid_module

    token = await _token(client, owner)
    for path in (
        f"/api/v1/purchases/{uuid_module.uuid4()}/sheet",
        f"/api/v1/sales/{uuid_module.uuid4()}/sheet",
        "/api/v1/payments/deadbeef/sheet",
    ):
        assert (await client.get(path, headers=_auth(token))).status_code == 404


async def test_stock_rows_carry_the_id_their_history_needs(
    client: AsyncClient, owner: User
) -> None:
    """The dashboard's stock list is clickable, and a code can't be the
    handle -- it is only unique within a brand."""
    token = await _token(client, owner)
    response = await client.get("/api/v1/inventory", headers=_auth(token))

    assert response.status_code == 200, response.text
    for row in response.json()["items"]:
        assert row["id"], row
        assert "unit" in row


async def test_a_movement_says_which_bill_it_came_from(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """ "-1200" is a number; "Sale · Hanif Pune" is the thing you opened
    the history to find."""
    import datetime
    import decimal
    import uuid as uuid_module

    import sqlalchemy as sa

    from backend.models import (
        Inventory,
        InventoryMovement,
        Product,
        ProductType,
        PurchaseHeader,
        PurchaseLine,
        Supplier,
    )
    from backend.models.enums import MovementType
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    suffix = uuid_module.uuid4().hex[:5]
    async with session_factory() as session:
        product_type = (
            (await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG)))
            .scalars()
            .first()
        )
        assert product_type is not None
        supplier = Supplier(org_id=ORG, name=f"Origin Co {suffix}", created_by=owner.id)
        product = Product(
            org_id=ORG,
            product_type_id=product_type.id,
            code=f"ORG{suffix.upper()}",
            description="Origin Test",
            unit_id=product_type.default_unit_id,
            created_by=owner.id,
        )
        session.add_all([supplier, product])
        await session.flush()
        header = PurchaseHeader(
            org_id=ORG,
            supplier_id=supplier.id,
            warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            invoice_no=f"ORIG-{suffix}",
            invoice_date=datetime.date.today(),
            subtotal=decimal.Decimal("1000"),
            grand_total=decimal.Decimal("1000"),
            status="confirmed",
            created_by=owner.id,
        )
        session.add(header)
        await session.flush()
        line = PurchaseLine(
            org_id=ORG,
            purchase_header_id=header.id,
            line_no=1,
            product_id=product.id,
            qty=decimal.Decimal("10"),
            rate=decimal.Decimal("100"),
            line_total=decimal.Decimal("1000"),
        )
        session.add(line)
        await session.flush()
        session.add_all(
            [
                Inventory(
                    org_id=ORG,
                    product_id=product.id,
                    warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                    qty_on_hand=decimal.Decimal("10"),
                    weighted_avg_cost=decimal.Decimal("100"),
                ),
                InventoryMovement(
                    org_id=ORG,
                    product_id=product.id,
                    warehouse_id=uuid_module.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                    movement_type=MovementType.PURCHASE,
                    qty_delta=decimal.Decimal("10"),
                    unit_cost=decimal.Decimal("100"),
                    resulting_qty_on_hand=decimal.Decimal("10"),
                    resulting_avg_cost=decimal.Decimal("100"),
                    source_type="purchase_line",
                    source_id=line.id,
                    created_by=owner.id,
                ),
            ]
        )
        product_id = product.id
        await session.commit()

    token = await _token(client, owner)
    response = await client.get(f"/api/v1/inventory/{product_id}/movements", headers=_auth(token))

    assert response.status_code == 200, response.text
    movement = response.json()["items"][0]
    assert movement["origin"] == f"Purchase ORIG-{suffix} · Origin Co {suffix}"


# --------------------------------------------------------------------
# analytics
# --------------------------------------------------------------------


async def _sold_product(
    session_factory: async_sessionmaker[AsyncSession],
    owner: User,
    *,
    code: str,
    rate: str,
    cost_then: str,
    qty: str = "10",
    on_hand: str = "100",
) -> None:
    """One product with one sale, at a stated rate against a stated
    cost-at-the-time."""
    import decimal

    from backend.models import (
        Brand,
        Customer,
        Inventory,
        Product,
        ProductType,
        SalesHeader,
        SalesLine,
    )
    from backend.models.enums import SalePaymentType
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    D = decimal.Decimal
    async with session_factory() as session, session.begin():
        ptype = (
            (await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG)))
            .scalars()
            .first()
        )
        assert ptype is not None
        brand = Brand(org_id=ORG, name=f"B{code}")
        customer = Customer(org_id=ORG, name=f"Cust {code}", created_by=owner.id)
        session.add_all([brand, customer])
        await session.flush()
        product = Product(
            org_id=ORG,
            product_type_id=ptype.id,
            code=code,
            description=f"{code} goods",
            unit_id=ptype.default_unit_id,
            brand_id=brand.id,
            created_by=owner.id,
        )
        session.add(product)
        await session.flush()
        session.add(
            Inventory(
                org_id=ORG,
                product_id=product.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                qty_on_hand=D(on_hand),
                # Deliberately different from cost_then: today's average
                # must not be what margin is computed from.
                weighted_avg_cost=D("1"),
            )
        )
        header = SalesHeader(
            org_id=ORG,
            customer_id=customer.id,
            warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
            sale_date=datetime.date.today(),
            payment_type=SalePaymentType.CREDIT,
            subtotal=D(rate) * D(qty),
            grand_total=D(rate) * D(qty),
            created_by=owner.id,
        )
        session.add(header)
        await session.flush()
        session.add(
            SalesLine(
                org_id=ORG,
                sales_header_id=header.id,
                line_no=1,
                product_id=product.id,
                qty=D(qty),
                rate=D(rate),
                line_total=D(rate) * D(qty),
                avg_cost_at_sale_time=D(cost_then),
            )
        )


async def test_product_performance_uses_the_cost_at_the_time_of_sale(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Margin must come from `avg_cost_at_sale_time`, not from today's
    weighted average.

    They differ the moment anything is bought since, and using today's
    figure would silently re-price history -- a product looking more
    profitable because the last purchase of it happened to be cheap. The
    fixture sets today's average to 1, so a margin computed the wrong
    way would be almost 100%.
    """
    await _sold_product(session_factory, owner, code="AAA", rate="200", cost_then="150")
    token = await _token(client, owner)

    response = await client.get("/api/v1/metrics/products", headers=_auth(token))
    assert response.status_code == 200, response.text
    rows = {row["code"]: row for row in response.json()["best_by_profit"]}
    assert "AAA" in rows
    row = rows["AAA"]
    assert row["revenue"] == "2000.00"
    assert row["cost"] == "1500.00"
    assert row["profit"] == "500.00"
    assert row["margin_pct"] == "25.0", (
        "margin was computed against today's average, not the sale's"
    )


async def test_a_product_sold_below_cost_is_flagged_not_hidden(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _sold_product(session_factory, owner, code="BBB", rate="70", cost_then="121")
    token = await _token(client, owner)

    body = (await client.get("/api/v1/metrics/products", headers=_auth(token))).json()
    losing = {row["code"] for row in body["losing"]}
    assert "BBB" in losing


async def test_brand_metrics_group_by_label(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A code means nothing without a brand here -- three products share
    55X on the real books -- so "is this label earning" is a question the
    product view cannot answer."""
    await _sold_product(session_factory, owner, code="CCC", rate="300", cost_then="100")
    token = await _token(client, owner)

    body = (await client.get("/api/v1/metrics/brands", headers=_auth(token))).json()
    brands = {row["brand"]: row for row in body["brands"]}
    assert "BCCC" in brands
    assert brands["BCCC"]["profit"] == "2000.00"
    assert brands["BCCC"]["codes"] == 1


async def test_stock_health_says_how_thin_the_evidence_is(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Every rate-based figure carries the sample it came from.

    A days-of-cover number built on one sale is a different object from
    one built on fifty, and a dashboard that hides the difference is a
    dashboard that lies confidently.
    """
    await _sold_product(session_factory, owner, code="DDD", rate="200", cost_then="150")
    token = await _token(client, owner)

    body = (await client.get("/api/v1/metrics/stock-health", headers=_auth(token))).json()
    rows = body["reorder"] + body["dead_stock"]
    for row in rows:
        assert "sale_count" in row and "sold_over_days" in row


async def test_analytics_are_owner_only(
    client: AsyncClient, staff: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Margin is partner-level information (docs/14 #rbac)."""
    token = await _token(client, staff)
    for path in (
        "/api/v1/metrics/products",
        "/api/v1/metrics/brands",
        "/api/v1/metrics/stock-health",
    ):
        response = await client.get(path, headers=_auth(token))
        assert response.status_code == 403, f"{path} was readable by staff"


async def test_quantities_are_not_rendered_in_scientific_notation(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """2,480 kg is not `2.48E+3`.

    `Decimal.normalize()` produces an exponent whenever a number has
    trailing zeros, which is most weights in kilograms. It is correct
    and it is unreadable beside a rupee figure -- and it went out to the
    live dashboard before anyone looked at the output.
    """
    await _sold_product(session_factory, owner, code="EEE", rate="100", cost_then="50", qty="2480")
    token = await _token(client, owner)

    body = (await client.get("/api/v1/metrics/products", headers=_auth(token))).json()
    row = next(r for r in body["best_by_profit"] if r["code"] == "EEE")
    assert row["qty"] == "2480", f"rendered as {row['qty']!r}"
    assert "E" not in row["qty"].upper()


# --------------------------------------------------------------------
# master control authentication
# --------------------------------------------------------------------


async def _set_control_password(
    session_factory: async_sessionmaker[AsyncSession], user: User, password: str
) -> None:
    async with session_factory() as session, session.begin():
        row = await session.get(User, user.id)
        assert row is not None
        row.control_password_hash = hash_password(password)


async def test_master_control_cannot_be_signed_into_until_a_password_is_set(
    client: AsyncClient, owner: User
) -> None:
    """The danger surface does not exist by default.

    `control_password_hash` is NULL for everyone after the migration, so
    a fresh deployment has no way into Master Control at all -- not a
    weak way, none. It appears when someone deliberately runs
    `set-control-password` on the box.
    """
    response = await client.post(
        "/api/v1/auth/control/login", json={"email": owner.email, "password": PASSWORD}
    )
    assert response.status_code == 401


async def test_a_dashboard_token_is_not_a_control_token(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """The property the whole design rests on.

    Master Control uses a separate token *type*, not a claim on the
    ordinary access token. So a dashboard session cannot be mistaken for
    a control session by a dependency that forgets to check a field --
    the token does not decode as the type at all.
    """
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    dashboard = await _token(client, owner)

    response = await client.get("/api/v1/control/whoami", headers=_auth(dashboard))
    assert response.status_code == 401, "a dashboard token reached Master Control"


async def test_a_control_token_signs_in_and_identifies_itself(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    signin = await client.post(
        "/api/v1/auth/control/login",
        json={"email": owner.email, "password": "a-long-generated-secret"},
    )
    assert signin.status_code == 200, signin.text
    body = signin.json()
    assert "refresh_token" not in body, "control sessions must not be refreshable"
    assert body["expires_in"] <= 30 * 60

    me = await client.get("/api/v1/control/whoami", headers=_auth(body["token"]))
    assert me.status_code == 200
    assert me.json()["full_name"] == owner.full_name


async def test_the_control_password_is_not_the_dashboard_password(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    response = await client.post(
        "/api/v1/auth/control/login", json={"email": owner.email, "password": PASSWORD}
    )
    assert response.status_code == 401, "the dashboard password opened Master Control"


async def test_staff_cannot_reach_master_control_even_with_a_password(
    client: AsyncClient, staff: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """Owner-only (plan.md §11.1), refused at the door rather than
    minting a token that is turned away later."""
    await _set_control_password(session_factory, staff, "a-long-generated-secret")
    response = await client.post(
        "/api/v1/auth/control/login",
        json={"email": staff.email, "password": "a-long-generated-secret"},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------
# master control: writes
# --------------------------------------------------------------------


async def _control(client: AsyncClient, owner: User) -> dict[str, str]:
    signin = await client.post(
        "/api/v1/auth/control/login",
        json={"email": owner.email, "password": "a-long-generated-secret"},
    )
    assert signin.status_code == 200, signin.text
    return _auth(signin.json()["token"])


@pytest.fixture
async def stocked_code(owner: User, session_factory: async_sessionmaker[AsyncSession]) -> str:
    """One supplier, one product, no stock movement yet."""
    import decimal

    from backend.models import Brand, Product, ProductType, Supplier

    code = f"W{uuid.uuid4().hex[:4].upper()}"
    async with session_factory() as session, session.begin():
        ptype = (
            (await session.execute(sa.select(ProductType).where(ProductType.org_id == ORG)))
            .scalars()
            .first()
        )
        assert ptype is not None
        brand = Brand(org_id=ORG, name=f"BR{code}")
        supplier = Supplier(org_id=ORG, name=f"Supp {code}", created_by=owner.id)
        session.add_all([brand, supplier])
        await session.flush()
        session.add(
            Product(
                org_id=ORG,
                product_type_id=ptype.id,
                code=code,
                description="Web entry probe",
                unit_id=ptype.default_unit_id,
                brand_id=brand.id,
                reorder_level=decimal.Decimal("0"),
                created_by=owner.id,
            )
        )
    return code


async def test_a_purchase_can_be_entered_from_the_web(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The form is a second way in, never a second implementation --
    this goes through the same PurchaseService.confirm WhatsApp calls."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"WEB-{uuid.uuid4().hex[:5]}"

    response = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": invoice,
            "invoice_date": "2026-08-14",
            "lines": [
                {
                    "code": stocked_code,
                    "qty": "800",
                    "rate": "120",
                    "pieces": "10",
                    "weight_kg": "80",
                }
            ],
            "charges": {"GST": "1200"},
            "discount": "200",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    # 800 x 120 = 96,000, plus 1,200 GST, less 200 discount
    assert body["grand_total"] == "97000.00"
    assert body["already_existed"] is False


async def test_submitting_the_same_bill_twice_returns_the_first_one(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A form can be submitted twice by a slow connection and an
    impatient thumb; a terminal command cannot.

    A purchase is uniquely a supplier plus an invoice number, so the
    natural key is the idempotency key. The second submission gets the
    bill that already exists rather than an error about a duplicate --
    it is the same bill, and that is what was asked for.
    """
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"WEB-{uuid.uuid4().hex[:5]}"
    payload = {
        "supplier": f"Supp {stocked_code}",
        "invoice_no": invoice,
        "invoice_date": "2026-08-14",
        "lines": [{"code": stocked_code, "qty": "800", "rate": "120"}],
    }

    first = await client.post("/api/v1/control/purchases", headers=headers, json=payload)
    second = await client.post("/api/v1/control/purchases", headers=headers, json=payload)

    assert first.status_code == 201
    assert second.json()["already_existed"] is True
    assert second.json()["purchase_id"] == first.json()["purchase_id"]


async def test_an_ambiguous_code_is_named_not_guessed(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Three products share 55X on the real books, and picking one
    silently is what produced bills 007 and 007B."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)

    response = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": f"WEB-{uuid.uuid4().hex[:5]}",
            "invoice_date": "2026-08-14",
            "lines": [{"code": "NOPE-NOT-A-CODE", "qty": "1", "rate": "1"}],
        },
    )
    assert response.status_code == 422
    assert "NOPE-NOT-A-CODE" in response.text


async def test_writes_need_the_control_credential(
    client: AsyncClient, owner: User, stocked_code: str
) -> None:
    """Entry writes to the books, so it sits behind the strong password
    rather than the dashboard's."""
    dashboard = _auth(await _token(client, owner))
    response = await client.post(
        "/api/v1/control/purchases",
        headers=dashboard,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": "X",
            "invoice_date": "2026-08-14",
            "lines": [{"code": stocked_code, "qty": "1", "rate": "1"}],
        },
    )
    assert response.status_code == 401


# --------------------------------------------------------------------
# master control: preview -> confirm
# --------------------------------------------------------------------


@pytest.fixture
async def two_customers(
    owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> tuple[str, str]:
    """Two customers, one sale on the first."""
    import decimal

    from backend.models import Customer, SalesHeader
    from backend.models.enums import SalePaymentType
    from backend.tests.conftest import SEEDED_MAIN_WAREHOUSE_ID

    tag = uuid.uuid4().hex[:5]
    async with session_factory() as session, session.begin():
        loser = Customer(org_id=ORG, name=f"Yakub {tag}", created_by=owner.id)
        winner = Customer(org_id=ORG, name=f"Asif {tag}", created_by=owner.id)
        session.add_all([loser, winner])
        await session.flush()
        session.add(
            SalesHeader(
                org_id=ORG,
                customer_id=loser.id,
                warehouse_id=uuid.UUID(SEEDED_MAIN_WAREHOUSE_ID),
                sale_date=datetime.date(2026, 8, 1),
                payment_type=SalePaymentType.CREDIT,
                subtotal=decimal.Decimal("5000"),
                grand_total=decimal.Decimal("5000"),
                created_by=owner.id,
            )
        )
    return f"Yakub {tag}", f"Asif {tag}"


async def test_a_preview_computes_by_doing_and_discarding(
    client: AsyncClient,
    owner: User,
    two_customers: tuple[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A preview that estimates can disagree with its own commit.

    This one runs the real merge in a transaction that is rolled back,
    so the figures are the ones the commit would produce -- and the
    party is verifiably still there afterwards.
    """
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    loser, winner = two_customers

    response = await client.post(
        "/api/v1/control/merge/preview",
        headers=headers,
        json={"kind": "customer", "loser": loser, "winner": winner},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transactions"] == 1
    assert body["moved_value"] == "5000.00"
    assert body["dry_run"] is True
    assert body["committed"] is False

    from backend.models import Customer

    async with session_factory() as session:
        still = (
            await session.execute(sa.select(Customer).where(Customer.name == loser))
        ).scalar_one()
        assert still.deleted_at is None, "the preview committed"


async def test_a_merge_needs_the_surviving_name_typed_back(
    client: AsyncClient,
    owner: User,
    two_customers: tuple[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A checkbox is clicked before it is read. This is the request that
    makes one party stop existing."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    loser, winner = two_customers

    wrong = await client.post(
        "/api/v1/control/merge",
        headers=headers,
        json={"kind": "customer", "loser": loser, "winner": winner, "confirm": "something else"},
    )
    assert wrong.status_code == 400
    assert "nothing was changed" in wrong.text


async def test_a_confirmed_merge_moves_the_sales_and_is_reversible(
    client: AsyncClient,
    owner: User,
    two_customers: tuple[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    loser, winner = two_customers

    response = await client.post(
        "/api/v1/control/merge",
        headers=headers,
        json={"kind": "customer", "loser": loser, "winner": winner, "confirm": winner},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["committed"] is True
    assert body["reversal"], "a merge with no manifest cannot be undone"

    from backend.models import Customer, SalesHeader

    async with session_factory() as session:
        gone = (
            await session.execute(sa.select(Customer).where(Customer.name == loser))
        ).scalar_one()
        assert gone.deleted_at is not None
        survivor = (
            await session.execute(sa.select(Customer).where(Customer.name == winner))
        ).scalar_one()
        moved = (
            await session.execute(
                sa.select(sa.func.count())
                .select_from(SalesHeader)
                .where(SalesHeader.customer_id == survivor.id)
            )
        ).scalar_one()
        assert moved == 1


async def test_the_preview_and_the_merge_need_the_control_credential(
    client: AsyncClient, owner: User, two_customers: tuple[str, str]
) -> None:
    dashboard = _auth(await _token(client, owner))
    loser, winner = two_customers
    for path in ("/api/v1/control/merge/preview", "/api/v1/control/merge"):
        response = await client.post(
            path,
            headers=dashboard,
            json={"kind": "customer", "loser": loser, "winner": winner, "confirm": winner},
        )
        assert response.status_code == 401


# --------------------------------------------------------------------
# master control: partner capital, without a second signature
# --------------------------------------------------------------------


async def _control_auth(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> dict[str, str]:
    secret = "a-long-generated-secret"
    await _set_control_password(session_factory, owner, secret)
    signin = await client.post(
        "/api/v1/auth/control/login", json={"email": owner.email, "password": secret}
    )
    assert signin.status_code == 200, signin.text
    return _auth(str(signin.json()["token"]))


async def test_capital_can_be_put_in_from_master_control(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.models import Partner

    async with session_factory() as session, session.begin():
        session.add(
            Partner(
                org_id=uuid.UUID(SEEDED_ORG_ID),
                display_name="Imran",
                profit_share_percent=decimal.Decimal("50"),
                created_by=owner.id,
            )
        )

    headers = await _control_auth(client, owner, session_factory)
    response = await client.post(
        "/api/v1/control/partners/capital",
        headers=headers,
        json={"partner": "Imran", "direction": "in", "amount": "250000", "via": "bank"},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["partner"] == "Imran"
    assert body["balance"] == "250000.00"
    assert body["negative"] is False

    listed = await client.get("/api/v1/control/partners", headers=headers)
    assert listed.status_code == 200
    assert {"name": "Imran", "balance": "250000.00", "negative": False} in listed.json()["items"]


async def test_taking_capital_out_past_zero_is_allowed_and_flagged(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    from backend.models import Partner

    async with session_factory() as session, session.begin():
        session.add(
            Partner(
                org_id=uuid.UUID(SEEDED_ORG_ID),
                display_name="Imran",
                profit_share_percent=decimal.Decimal("50"),
                created_by=owner.id,
            )
        )

    headers = await _control_auth(client, owner, session_factory)
    response = await client.post(
        "/api/v1/control/partners/capital",
        headers=headers,
        json={"partner": "Imran", "direction": "out", "amount": "5000", "via": "cash"},
    )
    assert response.status_code == 201, response.text
    assert response.json()["balance"] == "-5000.00"
    assert response.json()["negative"] is True


async def test_moving_capital_needs_the_control_credential(
    client: AsyncClient, owner: User, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    """A dashboard token must not reach it. This endpoint moves money
    with no approval behind it, so it is exactly the one that must not be
    reachable by the weaker credential."""
    dashboard = _auth(await _token(client, owner))
    response = await client.post(
        "/api/v1/control/partners/capital",
        headers=dashboard,
        json={"partner": "Imran", "direction": "in", "amount": "100", "via": "cash"},
    )
    assert response.status_code == 401


# --------------------------------------------------------------------
# a purged record is hidden everywhere, and still there
# --------------------------------------------------------------------


async def test_a_purged_bill_disappears_from_every_list_but_keeps_its_row(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Soft delete is only worth having if hiding actually hides.

    The promise of `deleted_at` is two-sided: gone from every list,
    report and total, and still on disk so `restore-purged`, the audit
    trail and reconciliation keep working. Nothing tested the first half
    until now -- a query that forgot the filter would have shown a
    removed bill in a total and nobody would have known which.
    """
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"PURGE-{uuid.uuid4().hex[:5]}"

    created = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": invoice,
            "invoice_date": "2026-08-14",
            "lines": [{"code": stocked_code, "qty": "800", "rate": "120"}],
        },
    )
    assert created.status_code == 201, created.text

    listed = await client.get("/api/v1/control/purchases/recent", headers=headers)
    assert invoice in [item["invoice_no"] for item in listed.json()["items"]]

    purged = await client.post(
        "/api/v1/control/purge",
        headers=headers,
        json={"kind": "purchase", "reference": invoice, "confirm": invoice},
    )
    assert purged.status_code == 200, purged.text

    # 1. gone from the list it was just in
    after = await client.get("/api/v1/control/purchases/recent", headers=headers)
    assert invoice not in [item["invoice_no"] for item in after.json()["items"]]

    # 2. gone from the dashboard's figures
    dashboard = await client.get("/api/v1/dashboard", headers=_auth(await _token(client, owner)))
    assert dashboard.status_code == 200
    body = dashboard.json()
    assert decimal.Decimal(body["inventory"]["value"]) == decimal.Decimal("0")

    # 3. gone from the stock its lines had created
    stock = await client.get(
        "/api/v1/inventory?limit=200", headers=_auth(await _token(client, owner))
    )
    rows = [row for row in stock.json()["items"] if row["code"] == stocked_code]
    assert all(decimal.Decimal(row["qty_on_hand"]) == decimal.Decimal("0") for row in rows)

    # 4. and still on disk, which is what makes it restorable
    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "select deleted_at is not null from purchase_headers where invoice_no = :i"
                ),
                {"i": invoice},
            )
        ).scalar_one()
    assert row is True, "the row must survive; hiding is not deleting"


async def test_a_reversed_payment_is_left_out_of_the_list_unless_asked_for(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every correction leaves a reversed payment behind. Showing them by
    default fills the list with pairs where only one half can be acted
    on -- and correcting a reversed one is refused anyway."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"REV-{uuid.uuid4().hex[:5]}"

    created = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": invoice,
            "invoice_date": "2026-08-14",
            "lines": [{"code": stocked_code, "qty": "800", "rate": "120"}],
        },
    )
    assert created.status_code == 201, created.text

    paid = await client.post(
        "/api/v1/control/pay",
        headers=headers,
        json={"party": f"Supp {stocked_code}", "amount": "5000", "via": "cash"},
    )
    assert paid.status_code == 201, paid.text
    reference = paid.json()["reference"]

    reversed_response = await client.post(
        f"/api/v1/control/payments/{reference}/reverse", headers=headers
    )
    assert reversed_response.status_code == 200, reversed_response.text

    hidden = await client.get("/api/v1/control/payments/recent", headers=headers)
    assert reference not in [item["reference"] for item in hidden.json()["items"]]

    shown = await client.get(
        "/api/v1/control/payments/recent?include_reversed=true", headers=headers
    )
    match = [item for item in shown.json()["items"] if item["reference"] == reference]
    assert match and match[0]["reversed"] is True


# --------------------------------------------------------------------
# editing a whole bill from the form
# --------------------------------------------------------------------


async def _confirmed_bill(
    client: AsyncClient, headers: dict[str, str], stocked_code: str, invoice: str
) -> None:
    created = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": invoice,
            "invoice_date": "2026-08-14",
            "lines": [
                {
                    "code": stocked_code,
                    "qty": "800",
                    "rate": "120",
                    "pieces": "10",
                    "weight_kg": "80",
                },
                {"code": stocked_code, "qty": "400", "rate": "120"},
            ],
        },
    )
    assert created.status_code == 201, created.text


async def test_a_bill_reopens_with_what_was_typed(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The form has to show bales and kg per bale, not only the total --
    reopening a bill that says 800 KG when it was typed as 10 bales makes
    the person do the arithmetic backwards to correct it."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"EDIT-{uuid.uuid4().hex[:5]}"
    await _confirmed_bill(client, headers, stocked_code, invoice)

    detail = await client.get(f"/api/v1/control/purchases/{invoice}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["invoice_no"] == invoice
    assert body["supplier"] == f"Supp {stocked_code}"
    assert len(body["lines"]) == 2
    first = body["lines"][0]
    assert first["code"] == stocked_code
    assert decimal.Decimal(first["qty"]) == decimal.Decimal("800")
    assert first["pieces"] == "10" or decimal.Decimal(first["pieces"]) == decimal.Decimal("10")
    assert decimal.Decimal(first["weight_kg"]) == decimal.Decimal("80")


async def test_previewing_an_edit_changes_nothing(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A preview that writes is worse than no preview, because it is
    trusted -- the same lesson `--dry-run` taught."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"EDIT-{uuid.uuid4().hex[:5]}"
    await _confirmed_bill(client, headers, stocked_code, invoice)

    preview = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit/preview",
        headers=headers,
        json={
            "lines": [
                {"line_no": 1, "code": stocked_code, "qty": "900", "rate": "125"},
                {"line_no": 2, "code": stocked_code, "qty": "400", "rate": "120"},
            ]
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["committed"] is False
    assert body["dry_run"] is True
    assert any("qty" in change for change in body["changes"])

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
    assert qty == decimal.Decimal("800.000"), "the preview saved the edit"


async def test_several_edits_at_once_are_applied_together(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The point of the screen: a quantity, a rate and a removal in one
    save, each through the repair that carries its stock and cost."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"EDIT-{uuid.uuid4().hex[:5]}"
    await _confirmed_bill(client, headers, stocked_code, invoice)

    applied = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=headers,
        json={
            "invoice_date": "2026-08-15",
            "lines": [
                {"line_no": 1, "code": stocked_code, "qty": "1040", "rate": "120"},
                {"line_no": 2, "code": stocked_code, "qty": "400", "rate": "120", "removed": True},
            ],
        },
    )
    assert applied.status_code == 200, applied.text
    body = applied.json()
    assert body["committed"] is True

    async with session_factory() as session:
        rows = (
            await session.execute(
                sa.text(
                    "SELECT pl.line_no, pl.qty FROM purchase_lines pl JOIN purchase_headers ph "
                    "ON ph.id = pl.purchase_header_id WHERE ph.invoice_no = :i "
                    "ORDER BY pl.line_no"
                ),
                {"i": invoice},
            )
        ).all()
        header = (
            await session.execute(
                sa.text(
                    "SELECT invoice_date, subtotal, grand_total FROM purchase_headers "
                    "WHERE invoice_no = :i"
                ),
                {"i": invoice},
            )
        ).one()
    assert [(r.line_no, r.qty) for r in rows] == [(1, decimal.Decimal("1040.000"))]
    assert header.invoice_date == datetime.date(2026, 8, 15)
    # 1040 × 120, with the removed line gone from the total
    assert header.subtotal == decimal.Decimal("124800.00")
    assert header.grand_total == decimal.Decimal("124800.00")


async def test_a_line_cannot_be_added_to_a_confirmed_bill(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """It would have no stock movement behind it, so the bill's history
    would not explain its own stock."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"EDIT-{uuid.uuid4().hex[:5]}"
    await _confirmed_bill(client, headers, stocked_code, invoice)

    response = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=headers,
        json={"lines": [{"line_no": None, "code": stocked_code, "qty": "100", "rate": "120"}]},
    )
    assert response.status_code == 400  # a domain refusal, not a schema one
    assert "its own bill" in response.text


async def test_undoing_a_removal_puts_the_line_and_its_stock_back(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The repair that today's mistake needed: a line removed by an edit
    goes back, with the stock movement it brought in -- read from the
    audit row the removal wrote, not from a backup."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    invoice = f"UNDO-{uuid.uuid4().hex[:5]}"
    await _confirmed_bill(client, headers, stocked_code, invoice)

    async with session_factory() as session:
        before_stock = (
            await session.execute(
                sa.text(
                    "SELECT qty_on_hand FROM inventory i JOIN products p ON p.id = i.product_id "
                    "WHERE p.code = :c"
                ),
                {"c": stocked_code},
            )
        ).scalar_one()

    removed = await client.post(
        f"/api/v1/control/purchases/{invoice}/edit",
        headers=headers,
        json={
            "lines": [
                {"line_no": 1, "code": stocked_code, "qty": "800", "rate": "120"},
                {"line_no": 2, "code": stocked_code, "qty": "400", "rate": "120", "removed": True},
            ]
        },
    )
    assert removed.status_code == 200, removed.text

    listed = await client.get("/api/v1/control/activity", headers=headers)
    assert listed.status_code == 200, listed.text
    edit = next(i for i in listed.json()["items"] if i["action"] == "purchase.edited")
    assert edit["undo"] == "restore_lines", "a removal must be offered as undoable"

    undone = await client.post(f"/api/v1/control/activity/{edit['id']}/undo", headers=headers)
    assert undone.status_code == 200, undone.text

    async with session_factory() as session:
        lines = (
            await session.execute(
                sa.text(
                    "SELECT pl.line_no, pl.qty FROM purchase_lines pl JOIN purchase_headers ph "
                    "ON ph.id = pl.purchase_header_id WHERE ph.invoice_no = :i ORDER BY pl.line_no"
                ),
                {"i": invoice},
            )
        ).all()
        after_stock = (
            await session.execute(
                sa.text(
                    "SELECT qty_on_hand FROM inventory i JOIN products p ON p.id = i.product_id "
                    "WHERE p.code = :c"
                ),
                {"c": stocked_code},
            )
        ).scalar_one()

    # the line is back, in its original numbered place
    assert [(r.line_no, r.qty) for r in lines] == [
        (1, decimal.Decimal("800.000")),
        (2, decimal.Decimal("400.000")),
    ]
    # and so is the stock it brought in
    assert after_stock == before_stock


# --------------------------------------------------------------------
# editing a sale: the payment stays where it is
# --------------------------------------------------------------------


async def test_editing_a_paid_sale_leaves_the_money_and_reports_the_gap(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Editing a sale down below what the customer paid does not hand
    their money back -- it makes them overpaid, and says so. Silently
    re-allocating a payment is how a business loses track of who owes
    what."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)

    # stock to sell, a customer to sell it to, then a sale paid in full
    await _confirmed_bill(client, headers, stocked_code, f"SB-{uuid.uuid4().hex[:5]}")
    made = await client.post(
        "/api/v1/control/parties",
        headers=headers,
        json={"kind": "customer", "name": f"Cust {stocked_code}"},
    )
    assert made.status_code in (200, 201), made.text
    sale = await client.post(
        "/api/v1/control/sales",
        headers=headers,
        json={
            "customer": f"Cust {stocked_code}",
            "lines": [{"code": stocked_code, "qty": "100", "rate": "200"}],
            # cash means paid in full already; paid_now is for credit
            "payment_type": "cash",
        },
    )
    assert sale.status_code == 201, sale.text
    reference = sale.json()["sale_id"][:8]

    detail = await client.get(f"/api/v1/control/sales/{reference}", headers=headers)
    assert detail.status_code == 200, detail.text
    assert decimal.Decimal(detail.json()["amount_paid"]) == decimal.Decimal("20000")

    preview = await client.post(
        f"/api/v1/control/sales/{reference}/edit/preview",
        headers=headers,
        json={"lines": [{"line_no": 1, "code": stocked_code, "qty": "80", "rate": "200"}]},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["committed"] is False
    assert body["payment_moved"] is False
    # 80 x 200 = 16,000 against 20,000 paid -> 4,000 overpaid
    assert decimal.Decimal(body["grand_total"]) == decimal.Decimal("16000.00")
    assert decimal.Decimal(body["overpaid"]) == decimal.Decimal("4000.00")

    applied = await client.post(
        f"/api/v1/control/sales/{reference}/edit",
        headers=headers,
        json={"lines": [{"line_no": 1, "code": stocked_code, "qty": "80", "rate": "200"}]},
    )
    assert applied.status_code == 200, applied.text

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT amount_paid, grand_total, payment_status FROM sales_headers "
                    "WHERE cast(id as text) LIKE :r"
                ),
                {"r": f"{reference}%"},
            )
        ).one()
    assert row.amount_paid == decimal.Decimal("20000.00"), "the payment must not move"
    assert row.grand_total == decimal.Decimal("16000.00")
    assert row.payment_status == "paid"


async def test_a_sale_edit_saves_the_charges_and_date_it_was_given(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """What happened on 2026-10-31: labour typed under the GST and a
    date changed on a credit sale, and the save kept neither -- while
    reporting the customer as changed to the customer it already had.
    Records then disagreed with the form, and the form with itself."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    await _confirmed_bill(client, headers, stocked_code, f"SB-{uuid.uuid4().hex[:5]}")
    customer = f"Cust {stocked_code}"
    made = await client.post(
        "/api/v1/control/parties", headers=headers, json={"kind": "customer", "name": customer}
    )
    assert made.status_code in (200, 201), made.text
    sale = await client.post(
        "/api/v1/control/sales",
        headers=headers,
        json={
            "customer": customer,
            "lines": [{"code": stocked_code, "qty": "100", "rate": "200"}],
            "payment_type": "credit",
        },
    )
    assert sale.status_code == 201, sale.text
    reference = sale.json()["sale_id"][:8]
    line = [{"line_no": 1, "code": stocked_code, "qty": "100", "rate": "200"}]
    back_then = datetime.date.today() - datetime.timedelta(days=40)

    gst = await client.post(
        f"/api/v1/control/sales/{reference}/edit",
        headers=headers,
        json={"supplier": customer, "lines": line, "charges": {"GST": "1000"}},
    )
    assert gst.status_code == 200, gst.text
    assert gst.json()["changes"] == ["charges 0.00 → 1000.00"], "same customer is not a change"

    # the form reopens carrying the GST, and labour goes under it
    detail = (await client.get(f"/api/v1/control/sales/{reference}", headers=headers)).json()
    assert decimal.Decimal(detail["other_charges"]) == decimal.Decimal("1000.00")
    labour = await client.post(
        f"/api/v1/control/sales/{reference}/edit",
        headers=headers,
        json={
            "supplier": customer,
            "invoice_date": back_then.isoformat(),
            "lines": line,
            "charges": {"CHARGES": "1000", "labour": "500"},
        },
    )
    assert labour.status_code == 200, labour.text
    assert decimal.Decimal(labour.json()["grand_total"]) == decimal.Decimal("21500.00")

    async with session_factory() as session:
        row = (
            await session.execute(
                sa.text(
                    "SELECT id, sale_date, other_charges, grand_total FROM sales_headers "
                    "WHERE cast(id as text) LIKE :r"
                ),
                {"r": f"{reference}%"},
            )
        ).one()
        dates = set(
            (
                await session.execute(
                    sa.text(
                        "SELECT entry_date FROM journal WHERE source_id = :id "
                        "AND source_type IN ('sales_header', 'sales_header_charge')"
                    ),
                    {"id": row.id},
                )
            ).scalars()
        )
    assert row.sale_date == back_then
    assert row.other_charges == decimal.Decimal("1500.00")
    assert row.grand_total == decimal.Decimal("21500.00")
    assert dates == {back_then}, "profit reads the journal, so its entries move with the sale"

    # and a charge can come back off a sale, down to none
    cleared = await client.post(
        f"/api/v1/control/sales/{reference}/edit",
        headers=headers,
        json={"supplier": customer, "lines": line, "charges": {}},
    )
    assert cleared.status_code == 200, cleared.text
    assert decimal.Decimal(cleared.json()["grand_total"]) == decimal.Decimal("20000.00")


# --------------------------------------------------------------------
# selling more than is on hand: refused with a reason, then allowed
# --------------------------------------------------------------------


async def test_short_stock_is_refused_by_name_and_records_on_override(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Goods leave the yard before the bill that brought them in is
    typed. The form refuses first and says which code is short -- in the
    error envelope the dashboard actually reads -- and records the sale
    when the person says to, exactly as WhatsApp does."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)

    await _confirmed_bill(client, headers, stocked_code, f"SS-{uuid.uuid4().hex[:5]}")
    await client.post(
        "/api/v1/control/parties",
        headers=headers,
        json={"kind": "customer", "name": f"Short {stocked_code}"},
    )
    body = {
        "customer": f"Short {stocked_code}",
        # far more than the bill above brought in
        "lines": [{"code": stocked_code, "qty": "999999", "rate": "200"}],
        "payment_type": "credit",
    }

    refused = await client.post("/api/v1/control/sales", headers=headers, json=body)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "insufficient_stock"
    # the message names the code, so the dashboard can show which line
    assert stocked_code in error["message"]

    allowed = await client.post(
        "/api/v1/control/sales",
        headers=headers,
        json={**body, "allow_negative_stock": True},
    )
    assert allowed.status_code == 201, allowed.text
    assert allowed.json()["already_existed"] is False


# --------------------------------------------------------------------
# money moves on its own day, not the day it was typed
# --------------------------------------------------------------------


async def test_money_in_out_and_expenses_take_the_day_they_happened(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A notebook is copied out days after the money moved. Every money
    form takes the day it happened -- the form sends `on` in the format
    a date input produces -- and blank still means today."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)

    await _confirmed_bill(client, headers, stocked_code, f"DT-{uuid.uuid4().hex[:5]}")
    paid = await client.post(
        "/api/v1/control/pay",
        headers=headers,
        # <input type="date"> emits exactly this shape
        json={"party": f"Supp {stocked_code}", "amount": "5000", "via": "cash", "on": "2026-08-17"},
    )
    assert paid.status_code == 201, paid.text

    spent = await client.post(
        "/api/v1/control/expenses",
        headers=headers,
        json={"category": "freight", "amount": "1200", "via": "cash", "on": "2026-08-17"},
    )
    assert spent.status_code == 201, spent.text

    listed = await client.get("/api/v1/control/payments/recent", headers=headers)
    assert listed.status_code == 200, listed.text
    dated = [i for i in listed.json()["items"] if i["reference"] == paid.json()["reference"]]
    assert dated and dated[0]["date"] == "2026-08-17", dated

    # a day that has not happened yet is refused, not filed under today
    future = await client.post(
        "/api/v1/control/pay",
        headers=headers,
        json={"party": f"Supp {stocked_code}", "amount": "10", "via": "cash", "on": "2099-01-01"},
    )
    assert future.status_code == 400, future.text
    assert future.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------
# one person on both sides of the book
# --------------------------------------------------------------------


async def test_a_party_who_buys_and_sells_nets_and_can_be_set_off(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Afzal sells us fabric and buys fabric from us. Linked, his two
    balances become one position: the payables list shows him net, the
    receivables list stops showing him twice over, and a set-off clears
    the smaller side against the larger without a rupee of cash moving."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    api = {"Authorization": f"Bearer {await _token(client, owner)}"}

    supplier_name = f"Supp {stocked_code}"
    customer_name = f"Cust {stocked_code}"

    # we owe him 1,44,000 on a bill; he owes us 20,000 on a sale
    await _confirmed_bill(client, headers, stocked_code, f"AF-{uuid.uuid4().hex[:5]}")
    await client.post(
        "/api/v1/control/parties",
        headers=headers,
        json={"kind": "customer", "name": customer_name},
    )
    sale = await client.post(
        "/api/v1/control/sales",
        headers=headers,
        json={
            "customer": customer_name,
            "lines": [{"code": stocked_code, "qty": "100", "rate": "200"}],
            "payment_type": "credit",
        },
    )
    assert sale.status_code == 201, sale.text

    payable_before = await _balance(client, api, "/api/v1/payables", supplier_name)
    receivable_before = await _balance(client, api, "/api/v1/receivables", customer_name)
    assert payable_before > ZERO and receivable_before > ZERO

    # unlinked, he is two strangers who happen to share a bank account
    linked = await client.post(
        "/api/v1/control/links",
        headers=headers,
        json={"supplier": supplier_name, "customer": customer_name},
    )
    assert linked.status_code == 201, linked.text
    position = linked.json()
    assert decimal.Decimal(position["net"]) == payable_before - receivable_before
    assert position["we_owe"] is True

    # the payables list now shows him net, and he is off the receivables
    # list entirely -- he is not a debtor, he is a smaller creditor
    payable_net = await _balance(client, api, "/api/v1/payables", supplier_name)
    assert payable_net == payable_before - receivable_before
    assert await _balance(client, api, "/api/v1/receivables", customer_name) == ZERO

    # setting off clears the whole overlap: the receivable goes, the
    # payable falls by the same amount, and no cash entry is made
    before_rows = await _ledger_rows(session_factory)
    done = await client.post(
        "/api/v1/control/setoff",
        headers=headers,
        json={"supplier": supplier_name, "on": "2026-08-21"},
    )
    assert done.status_code == 201, done.text
    result = done.json()
    assert decimal.Decimal(result["amount"]) == receivable_before
    assert decimal.Decimal(result["receivable_after"]) == ZERO
    assert decimal.Decimal(result["payable_after"]) == payable_before - receivable_before
    assert result["bills_cleared"] and result["sales_cleared"]
    # no cash moved, so the cash and bank books must be untouched --
    # a set-off recorded as a payment would put money through a drawer
    # nobody opened
    assert await _ledger_rows(session_factory) == before_rows
    # and the books still balance afterwards
    health = await client.get("/api/v1/control/health", headers=headers)
    assert health.status_code == 200, health.text
    assert health.json()["ok"] is True, health.text

    # and there is nothing left to set off a second time
    again = await client.post(
        "/api/v1/control/setoff", headers=headers, json={"supplier": supplier_name}
    )
    assert again.status_code == 400, again.text

    # his statement is now one timeline covering both sides, closing on
    # the net -- not two sheets that have to be read against each other
    supplier_id = await _party_id(client, api, "/api/v1/payables", supplier_name)
    sheet = await _statement_sheet(client, api, kind="supplier", party_id=supplier_id)
    # the sale to him appears on the supplier's own statement, which is
    # the whole point of linking the two rows
    assert str(sale.json()["sale_id"])[:8] in sheet


async def test_setting_off_an_unlinked_supplier_is_refused(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Netting two balances is only meaningful once someone has said
    they belong to one person. Guessing from a matching spelling is how
    two different Afzals become one."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    await _confirmed_bill(client, headers, stocked_code, f"NL-{uuid.uuid4().hex[:5]}")

    refused = await client.post(
        "/api/v1/control/setoff", headers=headers, json={"supplier": f"Supp {stocked_code}"}
    )
    assert refused.status_code == 400, refused.text
    assert "not linked" in refused.json()["error"]["message"]


async def _balance(
    client: AsyncClient, headers: dict[str, str], path: str, name: str
) -> decimal.Decimal:
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    for row in response.json()["data"]:
        if row["name"] == name:
            return decimal.Decimal(row["outstanding"])
    return ZERO


async def _party_id(client: AsyncClient, headers: dict[str, str], path: str, name: str) -> str:
    response = await client.get(path, headers=headers)
    assert response.status_code == 200, response.text
    for row in response.json()["data"]:
        if row["name"] == name:
            return str(row["id"])
    raise AssertionError(f"{name} not on {path}")


async def _statement_sheet(
    client: AsyncClient, headers: dict[str, str], *, kind: str, party_id: str
) -> str:
    """Every cell of the statement, flattened -- the test cares which
    events are on it, not where they landed."""
    import io

    from openpyxl import load_workbook

    response = await client.get(
        f"/api/v1/exports/statement.xlsx?kind={kind}&party_id={party_id}", headers=headers
    )
    assert response.status_code == 200, response.text
    sheet = load_workbook(io.BytesIO(response.content)).worksheets[0]
    return " ".join(str(c.value) for row in sheet.iter_rows() for c in row if c.value is not None)


async def _ledger_rows(session_factory: async_sessionmaker[AsyncSession]) -> int:
    """How many cash/bank rows exist. A set-off must not add one."""
    from sqlalchemy import func, select

    from backend.models import BankLedger, CashLedger

    async with session_factory() as session:
        cash = (await session.execute(select(func.count()).select_from(CashLedger))).scalar_one()
        bank = (await session.execute(select(func.count()).select_from(BankLedger))).scalar_one()
    return int(cash) + int(bank)


# --------------------------------------------------------------------
# a corrected payment is one line, not three
# --------------------------------------------------------------------


async def test_a_reversed_payment_and_its_reversal_leave_the_statement(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Correcting a payment reverses and re-records it, which is the
    honest ledger and terrible reading: one payment becomes three lines
    that the supplier has to net out by hand. Both halves of the
    cancelled pair come off the statement, and because they summed to
    nothing, the closing balance does not move."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    api = {"Authorization": f"Bearer {await _token(client, owner)}"}
    supplier_name = f"Supp {stocked_code}"

    await _confirmed_bill(client, headers, stocked_code, f"RV-{uuid.uuid4().hex[:5]}")
    paid = await client.post(
        "/api/v1/control/pay",
        headers=headers,
        json={"party": supplier_name, "amount": "5000", "via": "cash"},
    )
    assert paid.status_code == 201, paid.text

    supplier_id = await _party_id(client, api, "/api/v1/payables", supplier_name)
    before = await _statement_sheet(client, api, kind="supplier", party_id=supplier_id)
    owed_before = await _balance(client, api, "/api/v1/payables", supplier_name)

    # the date was wrong; correcting it reverses and re-records
    fixed = await client.post(
        f"/api/v1/control/payments/{paid.json()['reference']}/edit",
        headers=headers,
        json={"on": "2026-08-17"},
    )
    assert fixed.status_code == 200, fixed.text

    after = await _statement_sheet(client, api, kind="supplier", party_id=supplier_id)
    # no reversal line, and the payment still appears exactly once
    assert "Reversal" not in after, after
    assert after.count("5000") <= before.count("5000") + 1
    # the balance is untouched: the pair that was hidden summed to zero
    assert await _balance(client, api, "/api/v1/payables", supplier_name) == owed_before


async def test_the_party_picker_shows_both_sides_for_a_linked_party(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Afzal Bhai read "₹0.00 open" in the customer field while ₹64,000
    of his purchases sat on the supplier row. True of that column, and
    read by everyone as "we are square with Afzal"."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    supplier_name = f"Supp {stocked_code}"
    customer_name = f"Cust {stocked_code}"

    await _confirmed_bill(client, headers, stocked_code, f"PK-{uuid.uuid4().hex[:5]}")
    await client.post(
        "/api/v1/control/parties",
        headers=headers,
        json={"kind": "customer", "name": customer_name},
    )

    async def pick(kind: str, name: str) -> dict[str, Any]:
        found = await client.get(
            f"/api/v1/control/parties?q={name.split()[0]}&kind={kind}", headers=headers
        )
        assert found.status_code == 200, found.text
        rows = [i for i in found.json()["items"] if i["name"] == name]
        assert rows, found.text
        return dict(rows[0])

    # unlinked: the customer side knows nothing of the purchases
    assert decimal.Decimal((await pick("customer", customer_name))["outstanding"]) == ZERO
    owed = decimal.Decimal((await pick("supplier", supplier_name))["outstanding"])
    assert owed > ZERO
    # different names, so nothing to point at yet
    assert (await pick("customer", customer_name))["same_name_other_side"] is None

    linked = await client.post(
        "/api/v1/control/links",
        headers=headers,
        json={"supplier": supplier_name, "customer": customer_name},
    )
    assert linked.status_code == 201, linked.text

    # linked: picking him as a customer shows what is really open with
    # him -- negative, because the balance runs the other way
    row = await pick("customer", customer_name)
    assert row["linked"] is True
    assert decimal.Decimal(row["outstanding"]) == -owed
    assert decimal.Decimal(row["other_side"]) == owed


async def test_the_picker_names_a_same_named_row_on_the_other_side(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Afzal Bhai exists as both, spelled identically, and the customer
    field showed ₹0.00 with no hint that ₹64,000 of his purchases sat on
    the other row. The zero is right; the silence is not.

    It is still not netted -- two businesses can share a name, and only
    a person can say these two rows are one. The picker says what is
    there and where to go."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    # one name, both sides -- the Afzal case exactly
    shared = f"Supp {stocked_code}"

    await _confirmed_bill(client, headers, stocked_code, f"TW-{uuid.uuid4().hex[:5]}")
    made = await client.post(
        "/api/v1/control/parties", headers=headers, json={"kind": "customer", "name": shared}
    )
    assert made.status_code in (200, 201), made.text

    found = await client.get(
        f"/api/v1/control/parties?q={shared.split()[0]}&kind=customer", headers=headers
    )
    assert found.status_code == 200, found.text
    row = next(i for i in found.json()["items"] if i["name"] == shared)

    # his own side really is zero, and is reported as zero -- not netted
    assert decimal.Decimal(row["outstanding"]) == ZERO
    assert row["linked"] is False
    # but the other side is named, with its balance
    assert row["other_kind"] == "supplier"
    assert decimal.Decimal(row["same_name_other_side"]) > ZERO


# --------------------------------------------------------------------
# drafts: a half-entered bill that nobody has committed to
# --------------------------------------------------------------------


async def test_a_draft_survives_autosave_and_becomes_a_record_by_the_front_door(
    client: AsyncClient,
    owner: User,
    stocked_code: str,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Autosave writes one row however often it fires, a draft holds
    what a real record could not, and it becomes a record only by going
    through the ordinary create endpoint -- which validates it exactly
    as it validates anything typed fresh."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    key = uuid.uuid4().hex

    # a draft may name a supplier who does not exist and a code that
    # resolves to nothing: refusing that would defeat the point
    half = {
        "key": key,
        "kind": "purchase",
        "payload": {
            "party": "Nobody In Particular",
            "invoice": "SCRATCH-1",
            "lines": [{"code": "NOT-A-REAL-CODE", "qty": "5", "rate": "10"}],
        },
    }
    first = await client.put("/api/v1/control/drafts", headers=headers, json=half)
    assert first.status_code == 200, first.text

    # autosave fires on every keystroke; it must not leave a trail
    for _ in range(4):
        again = await client.put("/api/v1/control/drafts", headers=headers, json=half)
        assert again.status_code == 200, again.text
    listed = await client.get("/api/v1/control/drafts", headers=headers)
    assert listed.status_code == 200, listed.text
    mine = [d for d in listed.json()["items"] if d["key"] == key]
    assert len(mine) == 1, listed.text
    assert mine[0]["lines"] == 1
    assert mine[0]["label"].startswith("Nobody In Particular")
    assert mine[0]["author"] == owner.full_name

    # nothing of it reached the books
    bills = await client.get("/api/v1/control/purchases/recent", headers=headers)
    assert "SCRATCH-1" not in bills.text

    # the payload comes back exactly as the form left it
    read = await client.get(f"/api/v1/control/drafts/{key}", headers=headers)
    assert read.status_code == 200, read.text
    assert read.json()["payload"]["invoice"] == "SCRATCH-1"

    # made real through the front door, with the real code this time
    invoice = f"DR-{uuid.uuid4().hex[:5]}"
    created = await client.post(
        "/api/v1/control/purchases",
        headers=headers,
        json={
            "supplier": f"Supp {stocked_code}",
            "invoice_no": invoice,
            "invoice_date": "2026-08-20",
            "lines": [{"code": stocked_code, "qty": "100", "rate": "120"}],
        },
    )
    assert created.status_code == 201, created.text

    gone = await client.delete(f"/api/v1/control/drafts/{key}", headers=headers)
    assert gone.status_code == 200, gone.text
    missing = await client.get(f"/api/v1/control/drafts/{key}", headers=headers)
    assert missing.status_code == 404, missing.text
    after = await client.get("/api/v1/control/drafts", headers=headers)
    assert not [d for d in after.json()["items"] if d["key"] == key]


async def test_a_draft_says_whether_it_is_ready_to_record(
    client: AsyncClient,
    owner: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The list offers a one-click record only where one would work.
    Advisory: the create endpoint still validates for itself."""
    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)

    async def save(payload: dict[str, Any]) -> dict[str, Any]:
        key = uuid.uuid4().hex
        response = await client.put(
            "/api/v1/control/drafts",
            headers=headers,
            json={"key": key, "kind": "sale", "payload": payload},
        )
        assert response.status_code == 200, response.text
        return dict(response.json())

    # no party yet
    assert (await save({"lines": [{"code": "X", "qty": "1", "rate": "5"}]}))["ready"] is False
    # no lines yet
    assert (await save({"party": "Someone", "lines": []}))["ready"] is False
    # a line with no quantity is not an entry, it is a row that exists
    assert (await save({"party": "S", "lines": [{"code": "X", "qty": "0"}]}))["ready"] is False

    whole = await save(
        {
            "party": "Someone",
            "lines": [{"code": "X", "qty": "10", "rate": "20"}],
            "freight": "100",
            "charges": {"packing": "50"},
            "discount": "25",
        }
    )
    assert whole["ready"] is True
    # 10*20 + 100 + 50 - 25
    assert decimal.Decimal(whole["total"]) == decimal.Decimal("325")


async def test_stale_drafts_are_actually_dropped(
    client: AsyncClient,
    owner: User,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The Drafts page tells people untouched drafts go after 30 days.
    A page that says so while they pile up forever is worse than one
    that says nothing."""
    import datetime as dt

    from sqlalchemy import update

    from backend.models import Draft
    from backend.services.draft_service import DraftService

    await _set_control_password(session_factory, owner, "a-long-generated-secret")
    headers = await _control(client, owner)
    old_key, fresh_key = uuid.uuid4().hex, uuid.uuid4().hex
    for key in (old_key, fresh_key):
        saved = await client.put(
            "/api/v1/control/drafts",
            headers=headers,
            json={"key": key, "kind": "sale", "payload": {"party": "Someone"}},
        )
        assert saved.status_code == 200, saved.text

    # age one of them past the cutoff
    async with session_factory() as session, session.begin():
        await session.execute(
            update(Draft)
            .where(Draft.client_key == old_key)
            .values(updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=31))
        )

    async with session_factory() as session, session.begin():
        dropped = await DraftService(session).purge_stale(ORG)
    assert dropped == 1

    left = await client.get("/api/v1/control/drafts", headers=headers)
    keys = [d["key"] for d in left.json()["items"]]
    assert fresh_key in keys and old_key not in keys
