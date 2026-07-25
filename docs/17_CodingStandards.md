# 17 — Coding Standards

## 1. Type hints & static analysis

- Every function/method signature is fully typed; `mypy --strict`
  clean on `backend/` in CI (§ [15_Testing.md §5](15_Testing.md#ci-pipeline)).
- `Decimal` for every money/quantity value, never `float` — a `ruff`
  custom rule (or a `flake8-decimal`-style plugin) flags `float(...)`
  or bare float literals assigned to any variable/field named matching
  `*_amount|*_total|*_cost|*_rate|qty*|*_kg` as a lint error, since
  this is the single most consequential correctness rule in the
  codebase (per [02_Database.md](02_Database.md)) and is worth
  enforcing mechanically rather than trusting review alone to catch
  every instance.
- `ruff` for linting + formatting (replaces `black`/`isort`/`flake8` as
  one faster tool); config in `pyproject.toml`, no per-developer
  overrides.

## 2. Layering / import boundaries {#import-boundaries}

Enforced via `import-linter` contracts in `setup.cfg`/`pyproject.toml`,
matching the layering diagram in
[01_Architecture.md §4](01_Architecture.md#4-layering-rule-enforced-not-aspirational):

```ini
[importlinter]
root_package = backend

[importlinter:contract:layers]
name = Layered architecture
type = layers
layers =
    backend.api
    backend.services
    backend.repositories
    backend.models

[importlinter:contract:no-cross-repo]
name = Repositories do not import each other's business logic
type = forbidden
source_modules = backend.repositories
forbidden_modules = backend.services
```

A PR that violates these contracts fails CI at the "Import-boundary
check" stage (§ [15_Testing.md §5](15_Testing.md#ci-pipeline)) before
any test even runs — architectural drift is caught immediately, not
discovered months later during a refactor.

## 3. Repository pattern

One repository class per aggregate root, always accepting an injected
`AsyncSession`, always applying the soft-delete filter by default (per
[02_Database.md §4](02_Database.md#soft-delete)). Full example shape:
[01_Architecture.md §12](01_Architecture.md#12-illustrative-pattern-not-a-stub-this-is-the-actual-shape-every-servicerepository-follows).

Rules:
- A repository method returns ORM model instances or `None`/`list`,
  never raw dict/rows, never a domain exception (that's the service
  layer's job to raise, using information the repository returns).
- A repository never spans a business decision — "is this a
  duplicate" is a service concern that *uses* a repository's
  `find_potential_duplicates`-style method (a fetch), not a repository
  method that itself returns a boolean judgment.

## 4. Service layer

- One service class per bounded context (`PurchaseService`,
  `SalesService`, `InventoryService`, `LedgerService`, ...), each
  method representing one use case, orchestrating one or more
  repositories inside an explicit transaction boundary.
- Services raise typed domain exceptions
  (`backend/core/exceptions.py`) for expected business outcomes —
  never return an ambiguous `None`/`False`/error-string for a
  condition the caller must branch on; the API/WhatsApp layer catches
  specific exception types and maps each to its documented user-facing
  response (per every `Errors:` subsection in
  [08_WhatsApp.md](08_WhatsApp.md) and the error envelope in
  [10_API.md §5](10_API.md#5-error-envelope)).
- Every mutating service method is `@audited` (a decorator that
  enforces — at minimum, at test-collection time via §
  [15_Testing.md §3.5](15_Testing.md#35-audit-coverage-test-audit-coverage-test) —
  that an `AuditService.record(...)` call is present) and wraps its
  DB writes in one transaction: all-or-nothing, no partial commits.

## 5. No business logic in routes

A FastAPI route handler: parses/validates the request via a Pydantic
schema (mostly free — FastAPI does this from type hints), resolves
`org_id`/`role` from the JWT dependency, calls exactly one service
method, translates the result or caught exception into an HTTP
response. Nothing else. The same discipline applies to the WhatsApp
command dispatcher (`backend/api/whatsapp_router.py` or similar): it
parses command text into a typed command object, calls one service
method, formats the result into a WhatsApp reply — no domain decisions
made in the dispatcher itself.

## 6. Command registry pattern {#command-registry-pattern}

Every WhatsApp command ([08_WhatsApp.md](08_WhatsApp.md)) is defined
**once**, as data, in `backend/api/whatsapp_commands.py`:

```python
@dataclass(frozen=True)
class CommandSpec:
    name: str
    syntax: str
    min_role: UserRole
    parser: Callable[[str], CommandPayload]
    handler: Callable[[CommandPayload, RequestContext], Awaitable[CommandResult]]
    help_text: str

COMMAND_REGISTRY: dict[str, CommandSpec] = {
    "purchase": CommandSpec(
        name="purchase",
        syntax="purchase Supplier: <name> Invoice: <no> Date: <DD-MM-YYYY> ...",
        min_role=UserRole.STAFF,
        parser=parse_purchase_command,
        handler=PurchaseService.handle_command,
        help_text="Record a purchase manually, or send a photo for OCR.",
    ),
    # ... one entry per command in 08_WhatsApp.md
}
```

This registry is the **single source** for: the command dispatcher's
routing table, the `help` command's output (role-filtered per §4 in
[08_WhatsApp.md](08_WhatsApp.md#4-permissions-model-referenced-by-every-command-below)),
permission enforcement (`min_role` checked identically for every
command, same mechanism the API uses per
[14_Security.md §1](14_Security.md#1-rbac-rbac)), and the grammar test
suite (§3.8 in [15_Testing.md](15_Testing.md#38-whatsapp-command-grammar-tests)).
[08_WhatsApp.md](08_WhatsApp.md) is the human-readable specification
this registry implements — the two must stay consistent by
construction (one source, two views), not by manual diligence.

## 7. Exceptions catalogue (excerpt — grows with the domain)

```python
# backend/core/exceptions.py
class DomainError(Exception):
    """Base for all expected business-rule outcomes. Logged at INFO, not ERROR."""

class DuplicateInvoiceError(DomainError): ...
class ExactDuplicateInvoiceError(DuplicateInvoiceError): ...
class FuzzyDuplicateInvoiceError(DuplicateInvoiceError): ...
class TotalMismatchWarning(DomainError): ...
class InsufficientStockError(DomainError): ...
class NegativeStockOverrideRequired(InsufficientStockError): ...
class BelowCostSaleWarning(DomainError): ...
class CreditLimitExceededWarning(DomainError): ...
class DuplicateSaleError(DomainError): ...
class InvalidPurchaseStateError(DomainError): ...
class UndoWindowExpiredError(DomainError): ...
class UnauthorizedRoleError(DomainError): ...
```

Naming convention: `*Error` for outcomes that always block until
resolved; `*Warning` for outcomes that require a confirmation but
proceed on override (per the documented behavior in each owning doc —
this naming distinction is itself meaningful and checked in review,
not decorative).

## 8. Logging

- `structlog`, JSON output in every environment (not just production —
  keeps local dev logs consistent with what's actually shipped and
  debugged in staging/prod).
- Every log line carries `org_id`, and `request_id`/`whatsapp_message_id`
  when applicable, via context binding at the top of each
  request/task, so a single transaction's full log trail is
  greppable/filterable end to end.
- No `print()` anywhere in `backend/` — a `ruff` rule bans it outright.

## 9. Docstrings and comments

Per the project's general default: no comments explaining *what* code
does (names should do that); a short comment only where a *why* isn't
otherwise obvious — a non-obvious rounding rule, a workaround for a
specific OCR engine quirk, a business-rule citation back to the owning
doc section (e.g., `# see 03_Inventory.md §4 -- exact reversal only
possible if...`). No multi-paragraph docstrings; a one-line docstring
is fine where a public service method's purpose isn't obvious from its
name and type signature alone.

## 10. Migrations discipline

Covered in full in [02_Database.md §6](02_Database.md#6-migrations-alembic);
restated here as a coding-standards rule because it's enforced the
same way as everything else in this doc — in CI, not by convention
alone.

## 11. Docs-drift check {#docs-drift-check}

A CI step (§ [15_Testing.md §5](15_Testing.md#ci-pipeline)) that:
- Diffs the set of registered FastAPI routes against the endpoint list
  in [10_API.md §4](10_API.md#4-endpoints) (via a small script parsing
  both the OpenAPI schema and this doc's fenced code blocks) — new
  routes without a corresponding doc entry fail the build.
- Diffs `COMMAND_REGISTRY` (§6) keys against the set of `###` command
  headings in [08_WhatsApp.md](08_WhatsApp.md) — same principle,
  command surface and documentation cannot silently diverge.

This exists because a spec that isn't mechanically checked against the
code it describes decays into fiction within a few PRs — the whole
point of building this documentation set up front (per this project's
explicit instruction to produce implementation-level, non-simplified
docs) is undermined if nothing keeps it truthful once code changes
start happening.

## 12. PR checklist {#pr-checklist}

Every PR description includes (templated in
`.github/pull_request_template.md`):
- [ ] Which doc section(s) this implements/changes, and whether the
      doc needed updating alongside the code (not after).
- [ ] New/changed business rules have unit tests covering the
      documented edge cases, not just the happy path.
- [ ] Any new mutating service method is `@audited` and has an audit
      coverage test passing.
- [ ] No `float` used for money/quantity (self-check before the lint
      rule catches it).
- [ ] If this touches the purchase-sheet export format: golden-file
      test updated deliberately, not just re-recorded to make CI pass.
- [ ] If this adds a WhatsApp command or route: registry/doc entries
      added together.

## 13. Commit conventions

Conventional Commits style (`feat:`, `fix:`, `refactor:`, `test:`,
`docs:`, `chore:`) for changelog generation; a commit that changes
business behavior always references the doc section it implements in
the body, e.g. `Implements duplicate detection per 04_Purchases.md §6`.
