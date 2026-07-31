"""Attaching a transaction's document to whatever just happened.

docs/27_Documents.md. Every confirmation, correction and settlement
carries the current sheet for what it touched, so the partners never
have to ask for one -- and so a corrected bill's new version arrives in
the same chat as the correction.
"""

from __future__ import annotations

import uuid

from backend.api.command_types import CommandResult, RequestContext
from backend.core.logging import get_logger

logger = get_logger(__name__)


async def attach_document(
    result: CommandResult,
    ctx: RequestContext,
    *,
    kind: str,
    reference: str,
) -> CommandResult:
    """Put the transaction's own sheet on a confirmation.

    Built here rather than at save time, from the row rather than from
    the reply, so a bill whose rate was later corrected produces the
    corrected sheet -- and never fails the command it is decorating: a
    document that could not be built is a missing attachment, never a
    purchase that did not save.
    """
    import dataclasses as _dataclasses

    from backend.services.document_service import DocumentService

    try:
        async with ctx.session_factory() as session:
            service = DocumentService(session)
            if kind == "purchase":
                document = await service.purchase(ctx.user.org_id, uuid.UUID(reference))
            elif kind == "sale":
                document = await service.sale(ctx.user.org_id, uuid.UUID(reference))
            else:
                document = await service.payment(ctx.user.org_id, reference)
    except Exception:  # noqa: BLE001 -- see the docstring
        logger.warning("document_build_failed", kind=kind, reference=reference, exc_info=True)
        return result
    return _dataclasses.replace(
        result,
        attachment=str(document.path),
        attachment_caption=document.caption,
    )
