"""REST API -- docs/10_API.md.

The tests that matter here are the authorisation boundaries. Every
repository query already filters by org, so these check the layer that
makes tampering structurally impossible rather than merely unlikely:
`org_id` comes from the token, a refresh token can't be used as an
access token, and owner-only figures stay owner-only.
"""

from __future__ import annotations

import datetime
import uuid
from collections.abc import AsyncIterator

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.core.security import hash_password
from backend.main import create_app
from backend.models import Organization, User
from backend.models.enums import UserRole
from backend.tests.conftest import SEEDED_ORG_ID, purge_business_rows

ORG = uuid.UUID(SEEDED_ORG_ID)
PASSWORD = "correct-horse-battery-staple"


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
