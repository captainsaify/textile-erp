"""Master Control — plan.md.

Everything here is reached with a *control* token, which is a separate
credential and a separate token type from the dashboard's. The router
takes `ControlUser` on every route rather than checking inside handlers,
so a new endpoint cannot be added without the check.

Nothing in here mutates yet. This is the shell the guarded write
endpoints get built into.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from backend.api.deps import ControlUser, Session
from backend.services.reconciliation_service import ReconciliationService

router = APIRouter(prefix="/api/v1/control", tags=["control"])


@router.get("/whoami")
async def whoami(user: ControlUser) -> dict[str, Any]:
    """Who is signed in, and to which books.

    Exists so the shell can prove a control session is live before it
    renders anything -- and so the token type has one endpoint that
    tests can point at without touching data.
    """
    return {
        "user_id": str(user.id),
        "full_name": user.full_name,
        "org_id": str(user.org_id),
        "role": user.role.value,
    }


@router.get("/health")
async def books_health(user: ControlUser, session: Session) -> dict[str, Any]:
    """Do the books balance? The same check `erp check` runs.

    First thing on the Master Control screen, because every repair below
    it is only trustworthy if this is green -- and because a person who
    opens this page usually opens it *because* something looks wrong.
    """
    service = ReconciliationService(session)
    outcomes = [
        await service.check_inventory(user.org_id),
        await service.check_ledgers(user.org_id),
    ]
    return {
        "ok": all(outcome.ok for outcome in outcomes),
        "checks": [
            {
                "kind": outcome.kind,
                "checked": outcome.checked,
                "ok": outcome.ok,
                "discrepancies": [d.as_dict() for d in outcome.discrepancies],
            }
            for outcome in outcomes
        ],
    }
