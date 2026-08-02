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


class WebhookInteractiveReply(_WebhookModel):
    id: str = ""
    title: str = ""
    description: str = ""


class WebhookInteractive(_WebhookModel):
    """A tapped button or picked list row -- docs/19 §7. Both shapes
    carry an `id`, which is the string the user would have typed."""

    type: str = ""
    button_reply: WebhookInteractiveReply | None = None
    list_reply: WebhookInteractiveReply | None = None

    @property
    def choice_id(self) -> str | None:
        reply = self.button_reply or self.list_reply
        return reply.id or None if reply else None


class WebhookMessage(_WebhookModel):
    id: str
    from_number: str = Field(alias="from")
    timestamp: str = ""
    type: str
    text: WebhookTextBody | None = None
    image: WebhookMedia | None = None
    document: WebhookMedia | None = None
    interactive: WebhookInteractive | None = None

    @property
    def media(self) -> WebhookMedia | None:
        return self.image or self.document

    @property
    def choice_id(self) -> str | None:
        """The tapped option's id, if this was an interactive reply."""
        return self.interactive.choice_id if self.interactive else None


class WebhookMetadata(_WebhookModel):
    display_phone_number: str = ""
    phone_number_id: str = ""


class WebhookStatusError(_WebhookModel):
    code: int = 0
    title: str = ""
    message: str = ""

    @property
    def detail(self) -> str:
        extra = self.details if isinstance(self.details, str) else ""
        return extra or self.message or self.title

    details: str = ""


class WebhookStatus(_WebhookModel):
    """A delivery receipt for a message *we* sent.

    Parsed rather than ignored because "Meta accepted it" and "the
    partner received it" are different facts, and the gap between them
    is invisible without this: a send to a number the test sender may
    not reach is accepted with a message id and then fails here, with
    nothing in between to notice.
    """

    id: str = ""
    #: E.164 without the plus, as Meta sends it
    recipient_id: str = ""
    status: str = ""
    errors: list[WebhookStatusError] = []

    @property
    def failed(self) -> bool:
        return self.status == "failed" or bool(self.errors)


class WebhookValue(_WebhookModel):
    messaging_product: str = ""
    metadata: WebhookMetadata | None = None
    messages: list[WebhookMessage] = []
    statuses: list[WebhookStatus] = []


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
