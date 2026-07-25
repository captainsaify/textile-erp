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


def jid_to_e164(jid: str) -> str:
    """whatsapp-web.js identifies people as JIDs -- `919876543210@c.us`,
    sometimes with a device suffix (`919876543210:3@c.us`). Extract the
    number and normalize to E.164 for users.whatsapp_number lookup."""
    local = jid.split("@", 1)[0].split(":", 1)[0]
    return normalize_whatsapp_number(local)


def verify_shared_secret(configured: str, presented: str | None) -> bool:
    """Constant-time check for the bridge's X-Bridge-Secret header.
    An unconfigured secret fails closed."""
    if not configured or not presented:
        return False
    return hmac.compare_digest(configured.encode(), presented.encode())
