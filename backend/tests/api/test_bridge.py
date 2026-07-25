"""whatsapp-web.js bridge endpoint: shared-secret gate and the group
flow -- sender resolved as the individual author, reply addressed to
the group chat."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.whatsapp_dispatcher import WhatsAppDispatcher, get_dispatcher
from backend.core.config import get_settings
from backend.main import create_app
from backend.models import User
from backend.tests.api.conftest import FakeSender

BRIDGE_SECRET = "test-bridge-secret"


@pytest.fixture
def bridge_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BRIDGE_SHARED_SECRET", BRIDGE_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(bridge_env: None, dispatcher: WhatsAppDispatcher) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def bridge_message(
    sender_jid: str,
    body: str,
    *,
    chat_id: str | None = None,
    kind: str = "chat",
) -> dict[str, Any]:
    return {
        "message_id": f"true_{uuid.uuid4().hex}",
        "chat_id": chat_id or sender_jid,
        "sender": sender_jid,
        "is_group": bool(chat_id and chat_id.endswith("@g.us")),
        "kind": kind,
        "body": body if kind == "chat" else None,
    }


async def test_missing_or_wrong_secret_is_401(client: AsyncClient) -> None:
    message = bridge_message("919876543210@c.us", "help")
    response = await client.post("/internal/whatsapp-bridge/messages", json=message)
    assert response.status_code == 401
    response = await client.post(
        "/internal/whatsapp-bridge/messages",
        json=message,
        headers={"X-Bridge-Secret": "wrong"},
    )
    assert response.status_code == 401


async def test_direct_message_replies_to_sender(
    client: AsyncClient, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    jid = staff_user.whatsapp_number.lstrip("+") + "@c.us"
    response = await client.post(
        "/internal/whatsapp-bridge/messages",
        json=bridge_message(jid, "help"),
        headers={"X-Bridge-Secret": BRIDGE_SECRET},
    )
    assert response.status_code == 200
    assert len(fake_sender.sent) == 1
    to, body = fake_sender.sent[0]
    assert to == jid
    assert "Available commands" in body


async def test_group_message_resolves_author_and_replies_to_group(
    client: AsyncClient, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    group = "120363000000000001@g.us"
    # device-suffixed author JID, as web.js produces under multi-device
    author = staff_user.whatsapp_number.lstrip("+") + ":7@c.us"
    response = await client.post(
        "/internal/whatsapp-bridge/messages",
        json=bridge_message(author, "help", chat_id=group),
        headers={"X-Bridge-Secret": BRIDGE_SECRET},
    )
    assert response.status_code == 200
    assert len(fake_sender.sent) == 1
    to, body = fake_sender.sent[0]
    assert to == group
    assert "Available commands" in body


async def test_group_message_from_stranger_is_silently_dropped(
    client: AsyncClient, fake_sender: FakeSender
) -> None:
    group = "120363000000000001@g.us"
    stranger = f"9990{uuid.uuid4().int % 10**8:08d}@c.us"
    response = await client.post(
        "/internal/whatsapp-bridge/messages",
        json=bridge_message(stranger, "help", chat_id=group),
        headers={"X-Bridge-Secret": BRIDGE_SECRET},
    )
    assert response.status_code == 200
    assert fake_sender.sent == []


async def test_non_text_kind_gets_polite_rejection(
    client: AsyncClient, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    jid = staff_user.whatsapp_number.lstrip("+") + "@c.us"
    response = await client.post(
        "/internal/whatsapp-bridge/messages",
        json=bridge_message(jid, "", kind="image"),
        headers={"X-Bridge-Secret": BRIDGE_SECRET},
    )
    assert response.status_code == 200
    assert len(fake_sender.sent) == 1
    assert "text commands" in fake_sender.sent[0][1]
