"""Meta Cloud API media path: photos arrive as a media *id* that must be
fetched from the Graph API before OCR can run. Regression guard -- the
first build routed images down the text path and told users "I can only
read text commands"."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.api.whatsapp_dispatcher import WhatsAppDispatcher
from backend.models import User
from backend.ocr.engines import TesseractEngine
from backend.schemas.whatsapp import WebhookPayload
from backend.tests.api.conftest import FakeSender
from backend.tests.conftest import purge_business_rows
from backend.tests.ocr.fixtures import sheet_bytes


class FakeMetaClient(FakeSender):
    """Sender that also serves media by id, like the real Meta client."""

    def __init__(self, media: dict[str, tuple[bytes, str]] | None = None) -> None:
        super().__init__()
        self.media = media or {}
        self.fetched: list[str] = []

    async def fetch_media(self, media_id: str) -> tuple[bytes, str] | None:
        self.fetched.append(media_id)
        return self.media.get(media_id)


def media_payload(from_number: str, media_id: str, kind: str = "image") -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "123",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "from": from_number.lstrip("+"),
                                    "timestamp": "1753500000",
                                    "type": kind,
                                    kind: {
                                        "id": media_id,
                                        "mime_type": "image/png",
                                        "sha256": "abc",
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


@pytest.fixture(autouse=True)
async def clean(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIterator[None]:
    if not TesseractEngine().available():
        pytest.skip("tesseract not installed")
    yield
    await purge_business_rows(session_factory)


@pytest.fixture(autouse=True)
def local_ocr(tmp_path: object, monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.core.config import get_settings

    monkeypatch.setenv("OCR_PRIMARY_ENGINE", "tesseract")
    monkeypatch.setenv("ATTACHMENTS_DIR", str(tmp_path))
    get_settings.cache_clear()


async def test_photo_is_fetched_and_parsed(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    staff_user: User,
) -> None:
    assert staff_user.whatsapp_number is not None
    client = FakeMetaClient({"MEDIA123": (sheet_bytes(), "image/png")})
    dispatcher = WhatsAppDispatcher(
        session_factory=session_factory, redis=redis_client, client=client
    )

    await dispatcher.process_webhook(
        WebhookPayload.model_validate(media_payload(staff_user.whatsapp_number, "MEDIA123"))
    )

    assert client.fetched == ["MEDIA123"]
    bodies = [body for _, body in client.sent]
    # OCR has not run yet -- the photo is stored and its purpose asked
    # first, so a mis-sent picture never spends a vision call (docs/20 §2)
    assert any("What is it?" in body for body in bodies), bodies
    assert not any("Read 3 items" in body for body in bodies), bodies
    # never the text-only brush-off
    assert not any("only read text commands" in body for body in bodies)


async def test_failed_media_fetch_tells_the_user(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    staff_user: User,
) -> None:
    assert staff_user.whatsapp_number is not None
    client = FakeMetaClient({})  # id resolves to nothing
    dispatcher = WhatsAppDispatcher(
        session_factory=session_factory, redis=redis_client, client=client
    )

    await dispatcher.process_webhook(
        WebhookPayload.model_validate(media_payload(staff_user.whatsapp_number, "MISSING"))
    )

    assert len(client.sent) == 1
    assert "couldn't download that file" in client.sent[0][1]


async def test_documents_take_the_same_path(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: Any,
    staff_user: User,
) -> None:
    assert staff_user.whatsapp_number is not None
    client = FakeMetaClient({"DOC1": (sheet_bytes(), "image/png")})
    dispatcher = WhatsAppDispatcher(
        session_factory=session_factory, redis=redis_client, client=client
    )
    await dispatcher.process_webhook(
        WebhookPayload.model_validate(
            media_payload(staff_user.whatsapp_number, "DOC1", kind="document")
        )
    )
    assert client.fetched == ["DOC1"]
