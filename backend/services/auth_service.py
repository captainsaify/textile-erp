"""Dashboard/API authentication -- docs/10_API.md §3.

Access tokens are deliberately short (15 min) because this is a
financial system; refresh tokens last 7 days and are revocable through
a Redis deny-list, which is what makes "log out everywhere" mean
something rather than being a client-side gesture.
"""

from __future__ import annotations

import dataclasses
import datetime
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config import get_settings
from backend.core.logging import get_logger
from backend.core.redis import get_redis
from backend.core.security import (
    ACCESS_TOKEN_TYPE,
    REFRESH_TOKEN_TYPE,
    TokenClaims,
    TokenError,
    create_token,
    decode_token,
    verify_password,
)
from backend.models import User

logger = get_logger(__name__)

_REVOKED_PREFIX = "auth:revoked:"


class AuthError(Exception):
    """Wrong credentials, or a token that can't be trusted. The message
    is deliberately identical for "no such user" and "wrong password" --
    a different answer for each would confirm which accounts exist."""


@dataclasses.dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str
    expires_in: int
    role: str
    full_name: str


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._settings = get_settings()

    async def _issue(self, user: User) -> TokenPair:
        access_ttl = datetime.timedelta(minutes=self._settings.jwt_access_token_expire_minutes)
        refresh_ttl = datetime.timedelta(days=self._settings.jwt_refresh_token_expire_days)
        return TokenPair(
            access_token=create_token(
                user_id=user.id,
                org_id=user.org_id,
                role=user.role,
                token_type=ACCESS_TOKEN_TYPE,
                expires_in=access_ttl,
            ),
            refresh_token=create_token(
                user_id=user.id,
                org_id=user.org_id,
                role=user.role,
                token_type=REFRESH_TOKEN_TYPE,
                expires_in=refresh_ttl,
            ),
            expires_in=int(access_ttl.total_seconds()),
            role=user.role.value,
            full_name=user.full_name,
        )

    async def login(self, email: str, password: str) -> TokenPair:
        user = (
            (
                await self._session.execute(
                    select(User).where(
                        func.lower(User.email) == email.strip().lower(),
                        User.deleted_at.is_(None),
                        User.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .first()
        )

        # verify_password on a missing user still costs an argon2 verify
        # against a dummy hash, so response time doesn't reveal whether
        # the address exists
        stored = user.password_hash if user else None
        if not verify_password(password, stored) or user is None:
            logger.warning("login_failed", email=email.strip().lower())
            raise AuthError("Email or password is incorrect.")
        logger.info("login_succeeded", user_id=str(user.id))
        return await self._issue(user)

    async def refresh(self, refresh_token: str) -> TokenPair:
        try:
            claims = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except TokenError as exc:
            raise AuthError(str(exc)) from None
        if await self.is_revoked(claims.jti):
            raise AuthError("This session has been logged out.")

        user = await self._session.get(User, claims.user_id)
        if user is None or user.deleted_at is not None or not user.is_active:
            raise AuthError("This account is no longer active.")
        return await self._issue(user)

    async def logout(self, refresh_token: str) -> None:
        """Revoking is best-effort by design: a token we can't decode is
        already unusable, so failing the request would only confuse
        someone trying to sign out."""
        try:
            claims = decode_token(refresh_token, expected_type=REFRESH_TOKEN_TYPE)
        except TokenError:
            return
        ttl = datetime.timedelta(days=self._settings.jwt_refresh_token_expire_days)
        # the entry only needs to outlive the token itself
        await get_redis().set(f"{_REVOKED_PREFIX}{claims.jti}", "1", ex=int(ttl.total_seconds()))

    @staticmethod
    async def is_revoked(jti: str) -> bool:
        return bool(await get_redis().exists(f"{_REVOKED_PREFIX}{jti}"))

    async def user_for_claims(self, claims: TokenClaims) -> User:
        user = await self._session.get(User, claims.user_id)
        if user is None or user.deleted_at is not None or not user.is_active:
            raise AuthError("This account is no longer active.")
        if user.org_id != claims.org_id:
            # the token's org must match the row's; a mismatch means a
            # token minted before a move, or a forged claim
            raise AuthError("Token does not match this account.")
        return user


async def set_password(session: AsyncSession, email: str, password: str) -> User:
    """Used by the CLI: a WhatsApp-only account gains dashboard access
    without changing anything about how it uses WhatsApp."""
    from backend.core.security import hash_password

    user = (
        (await session.execute(select(User).where(func.lower(User.email) == email.strip().lower())))
        .scalars()
        .first()
    )
    if user is None:
        raise AuthError(f"No user with email {email!r}.")
    user.password_hash = hash_password(password)
    return user


def new_user_id() -> uuid.UUID:  # pragma: no cover -- convenience for scripts
    return uuid.uuid4()
