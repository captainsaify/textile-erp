"""Webhook signature verification -- docs/14_Security.md §5.

JWT auth for the dashboard/API arrives with the API phase; this module
starts with what the WhatsApp transport needs.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import hmac
import uuid

import argon2
import jwt

from backend.core.config import get_settings
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


# --- dashboard/API authentication (docs/10_API.md §3) -----------------
#
# Deliberately separate from the WhatsApp path above: a users row may
# have a whatsapp_number and no password (staff who only use WhatsApp),
# a password and no number (an accountant with read-only dashboard
# access), or both. Neither mechanism implies the other.

_ARGON2 = argon2.PasswordHasher()

ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
#: Master Control. A separate type, not a claim on the access token, so
#: a dashboard session can never be mistaken for a control session by a
#: dependency that forgets to check one field.
CONTROL_TOKEN_TYPE = "control"


def hash_password(plain: str) -> str:
    """argon2, per §3 -- never bcrypt, whose silent 72-byte truncation
    turns a long passphrase into a short one without telling anyone."""
    return _ARGON2.hash(plain)


def verify_password(plain: str, hashed: str | None) -> bool:
    """False rather than raising for a user with no password set, so a
    WhatsApp-only account can't be probed by the shape of the error."""
    if not hashed:
        return False
    try:
        return _ARGON2.verify(hashed, plain)
    except argon2.exceptions.VerificationError:
        return False
    except argon2.exceptions.InvalidHashError:
        return False


def create_token(
    *,
    user_id: uuid.UUID,
    org_id: uuid.UUID,
    role: UserRole,
    token_type: str,
    expires_in: datetime.timedelta,
) -> str:
    now = datetime.datetime.now(datetime.UTC)
    payload = {
        "sub": str(user_id),
        "org_id": str(org_id),
        "role": role.value,
        "type": token_type,
        "iat": int(now.timestamp()),
        "exp": int((now + expires_in).timestamp()),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, get_settings().jwt_signing_key, algorithm="HS256")


@dataclasses.dataclass(frozen=True)
class TokenClaims:
    user_id: uuid.UUID
    org_id: uuid.UUID
    role: UserRole
    token_type: str
    jti: str


class TokenError(Exception):
    """Malformed, expired, or signed with the wrong key."""


def decode_token(token: str, *, expected_type: str) -> TokenClaims:
    try:
        payload = jwt.decode(token, get_settings().jwt_signing_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise TokenError("token has expired") from None
    except jwt.InvalidTokenError:
        raise TokenError("token is not valid") from None

    if payload.get("type") != expected_type:
        # a refresh token must not be usable as an access token: it
        # lives far longer, so accepting one here would quietly extend
        # every session to the refresh lifetime
        raise TokenError(f"expected a {expected_type} token")
    try:
        return TokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            org_id=uuid.UUID(payload["org_id"]),
            role=UserRole(payload["role"]),
            token_type=payload["type"],
            jti=payload["jti"],
        )
    except (KeyError, ValueError):
        raise TokenError("token is missing required claims") from None
