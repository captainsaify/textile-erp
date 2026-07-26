"""Pydantic models of the Meta WhatsApp Business Cloud API webhook
payload -- the subset this system consumes. Extra fields are ignored,
never errors: Meta adds payload fields without notice and an inbound
webhook must not start failing because of one."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _WebhookModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class WebhookTextBody(_WebhookModel):
    body: str


class WebhookMedia(_WebhookModel):
    """Meta sends a media *id*, not bytes -- the file is fetched from the
    Graph API in a second step (docs/07_OCR.md media path)."""

    id: str
    mime_type: str = ""
    sha256: str = ""
    filename: str | None = None


class WebhookMessage(_WebhookModel):
    id: str
    from_number: str = Field(alias="from")
    timestamp: str = ""
    type: str
    text: WebhookTextBody | None = None
    image: WebhookMedia | None = None
    document: WebhookMedia | None = None

    @property
    def media(self) -> WebhookMedia | None:
        return self.image or self.document


class WebhookMetadata(_WebhookModel):
    display_phone_number: str = ""
    phone_number_id: str = ""


class WebhookValue(_WebhookModel):
    messaging_product: str = ""
    metadata: WebhookMetadata | None = None
    messages: list[WebhookMessage] = []
    # delivery/read receipts -- acknowledged, never processed
    statuses: list[dict[str, object]] = []


class WebhookChange(_WebhookModel):
    value: WebhookValue
    field: str = ""


class WebhookEntry(_WebhookModel):
    id: str = ""
    changes: list[WebhookChange] = []


class WebhookPayload(_WebhookModel):
    object: str = ""
    entry: list[WebhookEntry] = []


class BridgeInboundMedia(_WebhookModel):
    """A photo/PDF relayed by the bridge, base64-encoded. Kept separate
    from text messages because the OCR path is asynchronous: it acks
    immediately and replies when the sheet is parsed."""

    message_id: str
    chat_id: str
    sender: str
    is_group: bool = False
    mime_type: str
    filename: str | None = None
    data_base64: str


class BridgeInboundMessage(_WebhookModel):
    """One message relayed by the whatsapp-web.js bridge.

    `chat_id` is where the conversation lives (`...@c.us` for 1:1,
    `...@g.us` for a group) and is where the reply goes; `sender` is the
    JID of the individual person who wrote it (equals chat_id in 1:1).
    `kind` is web.js's message type -- "chat" means text.
    """

    message_id: str
    chat_id: str
    sender: str
    is_group: bool = False
    kind: str
    body: str | None = None
