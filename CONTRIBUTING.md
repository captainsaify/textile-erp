# Contributing

The standards here are stricter than most projects'. That is deliberate:
this system holds a real business's money, and a rounding error or a
silent failure costs someone actual rupees. Read this before your first
PR — it will save you a rewrite.

[`docs/17_CodingStandards.md`](docs/17_CodingStandards.md) is the full
version. This is what you need to not get sent back.

## The five rules that are not negotiable

1. **Money is `NUMERIC` / `Decimal`, never `float`.** Database columns,
   Python types, Pydantic schemas — and the browser too, where amounts
   are BigInt paise. `0.1 + 0.2` is not `0.3`, and a business that
   trusts you does not want to discover that on a payables report.

2. **Every mutation writes an `audit_logs` row.** No business table
   changes without one. No hard deletes on business tables — soft
   delete only. Corrections are compensating entries; nothing is
   erased, ever. This is why the system can say *who* changed a bill
   and *when*, on the bill itself.

3. **No placeholder code, no TODOs, no mocked business logic** reaches
   `main`. A feature is fully implemented — edge cases and tests
   included — or it is not merged. A half-feature in a financial system
   is worse than a missing one, because people trust it.

4. **WhatsApp is the primary interface.** Every mutating feature must be
   usable start to finish from the chat. The dashboard and REST API are
   read paths for reporting and admin, never the only way to do
   something.

5. **Config over code for domain variation.** Product types, OCR column
   templates and unit systems are database rows and YAML seeds. If you
   find yourself writing `if product_type == "textile"`, stop.

## Layering

```
routes / commands  →  services  →  repositories  →  models
```

- **No business logic in a route or a command handler.** They parse
  input, call one service, and format the reply.
- Services orchestrate repositories inside an explicit transaction and
  raise typed domain exceptions from `backend/core/exceptions.py` — never
  an ambiguous `None`/`False` for something the caller must branch on.
- Repositories return ORM instances, apply the soft-delete filter by
  default, and never make a business judgement. "Find possible
  duplicates" is a repository fetch; "is this a duplicate" is a service
  decision.

## Before you open a PR

```bash
uv run ruff check backend/ alembic/   # must be clean
uv run ruff format backend/
uv run mypy --strict backend/         # must be clean
uv run pytest backend/tests/          # all green
```

Tests need `TEST_DATABASE_URL` and a local Redis. Coverage target is 95%
on `backend/services/` and `backend/ocr/`.

## Writing tests

Test the *property*, not the implementation. A good test here says what
would go wrong in the business if the code were wrong — several in this
repo are named after the bug they exist to prevent, and their docstrings
record what actually happened. Follow that: a test called
`test_the_summary_export_carries_the_same_corrections` is worth ten
called `test_export_works`.

## Comments

Explain **why**, not what. The codebase's comments carry the reasoning
and the failures behind a decision, because that is the part that
cannot be re-derived from reading the code six months later. If a
comment would only restate the line below it, delete it.

## Migrations

Every schema change ships an Alembic migration in the same PR. Both
directions must work:

```bash
uv run alembic upgrade head
uv run alembic downgrade -1
uv run alembic upgrade head
```

## Things that will get a PR rejected

- A `float` anywhere near an amount, a quantity or a rate
- A mutation with no audit row, or a hard `DELETE` on a business table
- A new feature that only works on the dashboard
- A route that reaches past its service into a repository or the session
- `print()` — use `structlog` via `backend/core/logging.py`
- A commit that adds `.env`, credentials, a database dump, or a runtime
  log. Check `git status` before you stage; `data/` and `docker/certs/`
  are gitignored for a reason.

## Secrets

Never commit one. If you think you might have, say so immediately rather
than quietly force-pushing — history rewrites need coordinating with
everyone who has cloned. Rotate the secret regardless of whether the
push succeeded.
