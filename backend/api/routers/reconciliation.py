"""Reconciliation runs -- docs/10_API.md §4, docs/03_Inventory.md §6.

The nightly job records what it found and **never repairs it**: a job
that quietly corrected `qty_on_hand` would destroy the only evidence
that something upstream is broken. So a mismatch stays visible until a
human says they have looked at it, which is what
`reconciliation_runs.acknowledged_at` is for. The column was written for
this and, until now, nothing ever set it.

Acknowledging is not fixing. It records that a person saw the
discrepancy, and by whom -- the correction itself is still a
`stock adjust` on WhatsApp, so it goes through the same movement rows
and the same audit trail as any other inventory change.
"""

from __future__ import annotations

import datetime
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select

from backend.api.deps import CurrentUser, OwnerUser, Paging, Session
from backend.models import ReconciliationRun

router = APIRouter(prefix="/api/v1", tags=["reconciliation"])


def _serialise(run: ReconciliationRun) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "kind": run.kind,
        "status": run.status,
        "checked_count": run.checked_count,
        "mismatch_count": run.mismatch_count,
        "details": run.details or [],
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "acknowledged_at": run.acknowledged_at.isoformat() if run.acknowledged_at else None,
        "acknowledged_by": str(run.acknowledged_by) if run.acknowledged_by else None,
    }


@router.get("/inventory/reconciliations")
async def list_reconciliations(
    user: CurrentUser,
    session: Session,
    paging: Paging,
    unacknowledged: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Most recent first. `unacknowledged=true` is the dashboard's
    "needs attention" list -- runs that found something nobody has
    confirmed seeing yet."""
    stmt = (
        select(ReconciliationRun)
        .where(ReconciliationRun.org_id == user.org_id)
        .order_by(ReconciliationRun.started_at.desc())
        .limit(paging.limit)
    )
    after = paging.decode_after()
    if after is not None:
        stmt = stmt.where(ReconciliationRun.started_at < after)
    if unacknowledged:
        stmt = stmt.where(
            ReconciliationRun.mismatch_count > 0,
            ReconciliationRun.acknowledged_at.is_(None),
        )
    runs = list((await session.execute(stmt)).scalars())
    return {"data": [_serialise(run) for run in runs]}


@router.post("/inventory/reconcile/{run_id}/acknowledge")
async def acknowledge_reconciliation(
    run_id: uuid.UUID,
    user: OwnerUser,
    session: Session,
) -> dict[str, Any]:
    """Owner-only, and audited: this is someone taking responsibility for
    having looked at a discrepancy, so it needs to be attributable.

    Idempotent -- acknowledging twice keeps the first timestamp rather
    than moving it, because when it was *first* seen is the useful fact.
    """
    from backend.services.audit_service import AuditService

    # No `async with session.begin()` here: authenticating this request
    # already read the user through the same session, so SQLAlchemy has
    # autobegun and entering begin() raises "A transaction is already
    # begun" (HANDOFF.md §5). Inside a request, commit the ambient
    # transaction instead of opening one.
    run = await session.get(ReconciliationRun, run_id)
    if run is None or run.org_id != user.org_id:
        raise HTTPException(status_code=404, detail="reconciliation run not found")

    already = run.acknowledged_at
    if already is None:
        run.acknowledged_at = datetime.datetime.now(datetime.UTC)
        run.acknowledged_by = user.id
        await AuditService(session).record(
            user.org_id,
            user.id,
            action="reconciliation.acknowledged",
            entity_type="reconciliation_runs",
            entity_id=run.id,
            channel="api",
            after_state={
                "kind": run.kind,
                "mismatch_count": run.mismatch_count,
                "acknowledged_at": run.acknowledged_at.isoformat(),
            },
        )
        await session.commit()

    return {"data": _serialise(run), "already_acknowledged": already is not None}
