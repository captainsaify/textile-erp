"""Domain exception hierarchy.

Per docs/17_CodingStandards.md §7 and docs/01_Architecture.md §10:
these represent *expected* business-rule outcomes, not bugs -- caught
at the API/WhatsApp boundary and translated into the user-facing
responses documented per-command in docs/08_WhatsApp.md and into the
error envelope in docs/10_API.md §5. Logged at INFO, never ERROR.

Naming convention: `*Error` blocks until resolved; `*Warning` requires
a confirmation but proceeds on override. This distinction is meaningful
and checked in review, not decorative.

This module grows as later phases add the business logic each
exception belongs to (e.g. DuplicateSaleError arrives with the Sales
phase). Only the foundation-level types needed by Phase 1 (the
generic base classes and not-found/permission errors every future
service will reuse) are defined here.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID


class DomainError(Exception):
    """Base for all expected business-rule outcomes."""

    #: stable, machine-readable identifier -- matches docs/10_API.md §5's
    #: error envelope `code` field. Subclasses override this.
    code: str = "domain_error"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    """A referenced entity does not exist (or is soft-deleted)."""

    code = "not_found"

    def __init__(self, entity_type: str, entity_id: UUID | str) -> None:
        super().__init__(
            f"{entity_type} {entity_id} not found",
            details={"entity_type": entity_type, "entity_id": str(entity_id)},
        )


class UnauthorizedRoleError(DomainError):
    """Actor's role does not permit this action. See docs/14_Security.md §1."""

    code = "unauthorized_role"

    def __init__(self, required_role: str, actual_role: str) -> None:
        super().__init__(
            f"requires role '{required_role}' or higher, actor has '{actual_role}'",
            details={"required_role": required_role, "actual_role": actual_role},
        )


class ValidationError(DomainError):
    """Input failed a domain validation rule (distinct from Pydantic's
    request-shape validation, which never reaches the service layer)."""

    code = "validation_error"


class DuplicateInvoiceError(DomainError):
    code = "duplicate_invoice"


class ExactDuplicateInvoiceError(DuplicateInvoiceError):
    """Same supplier + invoice number already confirmed -- always a hard
    stop (docs/04_Purchases.md §6 layer 1)."""

    code = "exact_duplicate_invoice"

    def __init__(self, invoice_no: str, supplier_name: str, details: dict[str, Any]) -> None:
        super().__init__(
            f"Invoice {invoice_no} from {supplier_name} is already recorded",
            details=details,
        )


class FuzzyDuplicateInvoiceError(DuplicateInvoiceError):
    """Probable duplicate (2-of-3 signals) -- owner may override
    (docs/04_Purchases.md §6 layer 2)."""

    code = "fuzzy_duplicate_invoice"

    def __init__(self, message: str, *, details: dict[str, Any]) -> None:
        super().__init__(message, details=details)


class TotalMismatchWarning(DomainError):
    """Declared invoice total disagrees with computed total beyond
    tolerance -- requires the user to pick which is right
    (docs/04_Purchases.md §5)."""

    code = "total_mismatch"
