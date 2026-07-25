"""Signature verification, number normalization, RBAC ordering."""

from __future__ import annotations

import hashlib
import hmac

from backend.core.security import (
    normalize_whatsapp_number,
    role_at_least,
    verify_webhook_signature,
)
from backend.models.enums import UserRole

SECRET = "test-secret"
BODY = b'{"object":"whatsapp_business_account"}'


def _sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_accepted() -> None:
    assert verify_webhook_signature(SECRET, BODY, _sign(SECRET, BODY))


def test_wrong_secret_rejected() -> None:
    assert not verify_webhook_signature(SECRET, BODY, _sign("other-secret", BODY))


def test_tampered_body_rejected() -> None:
    assert not verify_webhook_signature(SECRET, BODY + b" ", _sign(SECRET, BODY))


def test_missing_or_malformed_header_rejected() -> None:
    assert not verify_webhook_signature(SECRET, BODY, None)
    assert not verify_webhook_signature(SECRET, BODY, "")
    assert not verify_webhook_signature(SECRET, BODY, "sha1=abc")
    assert not verify_webhook_signature(SECRET, BODY, "garbage")


def test_empty_secret_never_verifies() -> None:
    # unconfigured secret must fail closed, not open
    assert not verify_webhook_signature("", BODY, _sign("", BODY))


def test_normalize_whatsapp_number() -> None:
    assert normalize_whatsapp_number("919876543210") == "+919876543210"
    assert normalize_whatsapp_number("+91 98765 43210") == "+919876543210"
    assert normalize_whatsapp_number("+91-98765-43210") == "+919876543210"
    assert normalize_whatsapp_number("") == ""


def test_jid_to_e164() -> None:
    from backend.core.security import jid_to_e164

    assert jid_to_e164("919876543210@c.us") == "+919876543210"
    assert jid_to_e164("919876543210:7@c.us") == "+919876543210"
    assert jid_to_e164("919876543210") == "+919876543210"


def test_shared_secret_verification() -> None:
    from backend.core.security import verify_shared_secret

    assert verify_shared_secret("s3cret", "s3cret")
    assert not verify_shared_secret("s3cret", "wrong")
    assert not verify_shared_secret("s3cret", None)
    assert not verify_shared_secret("", "")  # unconfigured fails closed


def test_role_ordering() -> None:
    assert role_at_least(UserRole.OWNER, UserRole.STAFF)
    assert role_at_least(UserRole.STAFF, UserRole.STAFF)
    assert not role_at_least(UserRole.STAFF, UserRole.OWNER)
    assert not role_at_least(UserRole.VIEWER, UserRole.STAFF)
    assert role_at_least(UserRole.VIEWER, UserRole.VIEWER)
