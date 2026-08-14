"""Request dependencies for the REST API -- docs/10_API.md §2, §3.

The rule this module exists to enforce: **`org_id` comes from the
token, never from the request.** Every repository query already filters
by org, so this is defence in depth -- but it is the layer that makes
"change the id in the URL" structurally impossible rather than merely
unlikely.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.db import get_session_factory
from backend.core.security import ACCESS_TOKEN_TYPE, CONTROL_TOKEN_TYPE, TokenError, decode_token
from backend.models import User
from backend.models.enums import UserRole
from backend.services.auth_service import AuthError, AuthService

_bearer = HTTPBearer(auto_error=False)

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_session_factory()() as session:
        yield session


async def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(db_session)],
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(credentials.credentials, expected_type=ACCESS_TOKEN_TYPE)
        return await AuthService(session).user_for_claims(claims)
    except (TokenError, AuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


CurrentUser = Annotated[User, Depends(current_user)]
Session = Annotated[AsyncSession, Depends(db_session)]


async def owner_only(user: CurrentUser) -> User:
    """docs/10_API.md marks several endpoints owner-only; profit and
    partner capital are partner-level information (docs/14_Security.md
    #rbac), not something staff see."""
    if user.role is not UserRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this needs an owner account"
        )
    return user


OwnerUser = Annotated[User, Depends(owner_only)]


async def control_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[AsyncSession, Depends(db_session)],
) -> User:
    """Master Control, and nothing else, reaches this.

    A separate token *type*, not a claim on the ordinary access token.
    That is the whole point: a dashboard session cannot be mistaken for
    a control session by a dependency that forgets to check one field,
    because the token will not decode as the type at all.

    Owner-only is enforced here as well as at login (plan.md §11.1) --
    a role can change after a token is minted, and the token outlives
    the change by up to half an hour.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Master Control needs its own sign-in",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        claims = decode_token(credentials.credentials, expected_type=CONTROL_TOKEN_TYPE)
        user = await AuthService(session).user_for_claims(claims)
    except (TokenError, AuthError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from None
    if user.role is not UserRole.OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="this needs an owner account"
        )
    return user


ControlUser = Annotated[User, Depends(control_user)]


@dataclasses.dataclass(frozen=True)
class Page:
    """Cursor pagination -- §2. Offset pagination is unstable while rows
    are being inserted (a page boundary shifts and a row is seen twice
    or never); the cursor encodes the sort key it left off at."""

    limit: int
    cursor: str | None

    def decode_after(self) -> datetime.datetime | None:
        if not self.cursor:
            return None
        try:
            raw = base64.urlsafe_b64decode(self.cursor.encode()).decode()
            return datetime.datetime.fromisoformat(raw)
        except (ValueError, binascii.Error, UnicodeDecodeError):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="cursor is not valid"
            ) from None

    @staticmethod
    def encode(value: datetime.datetime) -> str:
        return base64.urlsafe_b64encode(value.isoformat().encode()).decode()


def page_params(
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
    cursor: Annotated[str | None, Query()] = None,
) -> Page:
    return Page(limit=limit, cursor=cursor)


Paging = Annotated[Page, Depends(page_params)]
