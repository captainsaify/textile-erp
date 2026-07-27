"""HTTP-level webhook behaviour: Meta handshake and signature gate --
docs/08_WhatsApp.md §1, docs/14_Security.md §5."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator, Iterator

import pytest
from httpx import ASGITransport, AsyncClient

from backend.api.whatsapp_dispatcher import WhatsAppDispatcher, get_dispatcher
from backend.core.config import get_settings
from backend.main import create_app
from backend.models import User
from backend.tests.api.conftest import FakeSender, meta_payload, text_message

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"


@pytest.fixture
def whatsapp_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("WHATSAPP_APP_SECRET", APP_SECRET)
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", VERIFY_TOKEN)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
async def client(whatsapp_env: None, dispatcher: WhatsAppDispatcher) -> AsyncIterator[AsyncClient]:
    app = create_app()
    app.dependency_overrides[get_dispatcher] = lambda: dispatcher
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()


async def test_handshake_echoes_challenge(client: AsyncClient) -> None:
    response = await client.get(
        "/webhooks/whatsapp",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "1158201444",
        },
    )
    assert response.status_code == 200
    assert response.text == "1158201444"


async def test_handshake_rejects_wrong_token(client: AsyncClient) -> None:
    response = await client.get(
        "/webhooks/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong", "hub.challenge": "x"},
    )
    assert response.status_code == 403


async def test_post_without_signature_is_401(client: AsyncClient) -> None:
    response = await client.post("/webhooks/whatsapp", content=b"{}")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


async def test_post_with_bad_signature_is_401(client: AsyncClient) -> None:
    response = await client.post(
        "/webhooks/whatsapp",
        content=b"{}",
        headers={"X-Hub-Signature-256": "sha256=" + "0" * 64},
    )
    assert response.status_code == 401


async def test_signed_message_end_to_end_reply(
    client: AsyncClient, fake_sender: FakeSender, staff_user: User
) -> None:
    assert staff_user.whatsapp_number is not None
    body = json.dumps(meta_payload(text_message(staff_user.whatsapp_number, "help"))).encode()
    response = await client.post(
        "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "received"}
    # ASGITransport runs background tasks before returning
    assert "Available commands" in fake_sender.sent[0][1]


async def test_signed_garbage_is_acknowledged_not_retried(client: AsyncClient) -> None:
    body = b'{"object": 42, "entry": "nope"}'
    response = await client.post(
        "/webhooks/whatsapp", content=body, headers={"X-Hub-Signature-256": _sign(body)}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}


async def test_healthz(client: AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
