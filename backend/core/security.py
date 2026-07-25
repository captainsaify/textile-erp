"""Webhook signature verification -- docs/14_Security.md §5.

JWT auth for the dashboard/API arrives with the API phase; this module
starts with what the WhatsApp transport needs.
"""

from __future__ import annotations

import hashlib
import hmac

from backend.models.enums import UserRole

_ROLE_RANK: dict[UserRole, int] = {UserRole.VIEWER: 0, UserRole.STAFF: 1, UserRole.OWNER: 2}


def role_at_least(actual: UserRole, required: UserRole) -> bool:
    """RBAC comparison -- docs/14_Security.md §1. owner > staff > viewer."""
    return _ROLE_RANK[actual] >= _ROLE_RANK[required]


def verify_webhook_signature(
    app_secret: str, raw_body: bytes, signature_header: str | None
) -> bool:
    """HMAC-SHA256 check of Meta's X-Hub-Signature-256 header.

    Constant-time comparison; an absent/malformed header is simply
    invalid, never an exception -- the caller turns False into a 401.
    """
    if not app_secret or not signature_header:
        return False
    prefix, _, received_hex = signature_header.partition("=")
    if prefix != "sha256" or not received_hex:
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_hex)


def normalize_whatsapp_number(raw: str) -> str:
    """Meta sends `from` as bare digits (`919876543210`); users store
    E.164 (`+919876543210`) -- docs/08_WhatsApp.md §2."""
    cleaned = raw.strip().replace(" ", "").replace("-", "")
    if cleaned and not cleaned.startswith("+"):
        cleaned = f"+{cleaned}"
    return cleaned
