"""Auth endpoints -- docs/10_API.md §3."""

from __future__ import annotations

from fastapi import APIRouter, status
from pydantic import BaseModel, EmailStr, Field

from backend.api.deps import CurrentUser, Session
from backend.services.auth_service import AuthService

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str
    full_name: str


class ControlTokenResponse(BaseModel):
    """One token, no refresh. A refresh token exists so a dashboard can
    stay open for a week; Master Control should not have that property."""

    token: str
    expires_in: int
    full_name: str


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, session: Session) -> TokenResponse:
    pair = await AuthService(session).login(body.email, body.password)
    return TokenResponse(**pair.__dict__)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, session: Session) -> TokenResponse:
    pair = await AuthService(session).refresh(body.refresh_token)
    return TokenResponse(**pair.__dict__)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(body: RefreshRequest, session: Session) -> None:
    """Revokes the refresh token so it can't mint further access tokens.
    Already-issued access tokens run out on their own 15-minute clock --
    that short lifetime is what makes this acceptable without a
    per-request deny-list check on the hot path."""
    await AuthService(session).logout(body.refresh_token)


class MeResponse(BaseModel):
    id: str
    full_name: str
    email: str | None
    role: str
    org_id: str


@router.post("/control/login", response_model=ControlTokenResponse)
async def control_login(body: LoginRequest, session: Session) -> ControlTokenResponse:
    """Sign in to Master Control with its own password.

    Separate from `/login` in every respect that matters: a different
    credential, a 30-minute session rather than seven days, no refresh
    token, and a per-account lockout on top of nginx's per-IP limit.
    """
    result = await AuthService(session).control_login(body.email, body.password)
    return ControlTokenResponse(
        token=result.token, expires_in=result.expires_in, full_name=result.full_name
    )


@router.get("/me", response_model=MeResponse)
async def me(user: CurrentUser) -> MeResponse:
    return MeResponse(
        id=str(user.id),
        full_name=user.full_name,
        email=user.email,
        role=user.role.value,
        org_id=str(user.org_id),
    )
