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


class WebhookMessage(_WebhookModel):
    id: str
    from_number: str = Field(alias="from")
    timestamp: str = ""
    type: str
    text: WebhookTextBody | None = None


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
