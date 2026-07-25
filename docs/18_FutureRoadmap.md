# 18 — Future Roadmap

This document records what was **deliberately deferred**, and why —
distinct from a wish list. Every item below was considered during v1
design and consciously not built, per
[`CLAUDE.md`](../CLAUDE.md#non-negotiable-philosophy)'s instruction
not to design for hypothetical requirements. Recording the reasoning
here means a future decision to build one of these starts from an
informed baseline instead of re-litigating the tradeoff from scratch.

## 1. Multi-tenancy / SaaS {#multi-tenancy}

**What's already in place**: every business table carries `org_id`
(§ [02_Database.md §1](02_Database.md#1-conventions)); every
repository query filters by it; JWT claims carry `org_id`
([10_API.md §3](10_API.md#3-authentication)); Celery Beat schedules
are org-timezone-aware
([11_BackgroundWorkers.md §3](11_BackgroundWorkers.md#3-celery-beat-schedule)).
This was done deliberately in v1, at near-zero marginal cost, because
retrofitting a tenant column onto an already-large schema and every
existing query is expensive; carrying it from row one is not.

**What's deliberately NOT built**: tenant self-service signup/
provisioning, per-tenant billing, tenant-admin UI, cross-tenant admin
tooling, row-level security policies enforcing `org_id` isolation at
the Postgres level (currently enforced only in the repository layer —
adequate for a single trusted org, not adequate defense-in-depth for
untrusted multi-tenant isolation), and a fine-grained permission model
beyond the three roles in [14_Security.md §1](14_Security.md#1-rbac-rbac)
(different tenant organizations would have different structures —
sole proprietor vs. multi-partner vs. small company — that a
three-role model doesn't accommodate).

**Trigger for revisiting**: a second real business (not a
hypothetical one) wanting to run on this system. At that point:
add Postgres row-level security policies as a second enforcement
layer, build tenant provisioning, and design the richer permission
model — each additive to the existing schema, not a rewrite of it.

## 2. Additional product types

The mechanism ([00_ProjectVision.md §4](00_ProjectVision.md#4-why-the-core-is-product-agnostic-not-textile-specific),
[02_Database.md §3.6](02_Database.md#36-product_types)) exists in v1;
what's deferred is **building out a second product type's actual
configuration** (a hardware, grocery, or general-goods
`product_types`/`ocr_templates`/`export_templates` set) — there is no
second product type to configure until the business (or a future
tenant) actually trades in one. Adding one is expected to be
config-only per the architecture, and doing so for the first time will
be the real-world validation of that claim — worth treating as a
milestone to deliberately test, not just assume works.

## 3. Multi-currency

Deferred per [00_ProjectVision.md §7](00_ProjectVision.md#7-non-goals-explicitly-out-of-scope-for-v1).
When needed: `organizations.base_currency` already exists as the
anchor; would add a `currency` + `exchange_rate_at_transaction_time`
pair to every monetary transaction table, with all reporting
aggregated in `base_currency` using the recorded rate (never a
live-refetched historical rate, for the same reproducibility reason
`avg_cost_at_sale_time` is snapshotted rather than recomputed —
[06_Accounting.md §4](06_Accounting.md#4-weighted-average-costs-role-in-accounting)).

## 4. Manufacturing / BOM support

Out of scope — this is a trading (buy-sell) ERP. If the business ever
began converting raw fabric into finished goods (cutting, stitching),
that would require a bill-of-materials, work-order, and
production-cost-allocation model layered on top of (not replacing)
the existing purchase/inventory/sales core — a substantial addition,
not attempted speculatively.

## 5. Full GST/tax filing integration

`gst_number` fields exist ([06_Accounting.md §11](06_Accounting.md#11-tax-handling-tax-handling));
automated tax computation and e-filing integration (e.g., GSTN API)
is deferred until there's a concrete filing workflow to automate —
building against an assumed-but-unvalidated filing process risks
building the wrong thing.

## 6. Native mobile app

Deferred — WhatsApp is the mobile interface by design
([00_ProjectVision.md §2](00_ProjectVision.md#2-why-whatsapp-not-a-web-app-or-mobile-app)).
Would only become worth reconsidering if a future feature genuinely
cannot be expressed well through WhatsApp's conversational interface
(e.g., a highly visual inventory-photo-grid browsing experience) —
no such feature has been identified yet.

## 7. Scheduled/automated report delivery

Mechanism reuse noted in
[13_Reports.md §7](13_Reports.md#7-scheduling) — "email/send the P&L
every Monday morning" is a small addition (a new Beat schedule entry
calling the existing `report_generation` task) once a partner actually
asks for it; not built speculatively because nobody has.

## 8. Dedicated secrets manager

[16_Deployment.md §5](16_Deployment.md#5-secrets-secrets) currently
uses a permission-restricted `.env` file, appropriate for a
single-host deployment. Revisit (Vault, cloud provider secrets
manager) if the deployment topology grows beyond one host or if
compliance requirements (e.g., onboarding an external accountant with
infrastructure access) demand stronger secret isolation.

## 9. Read replica / horizontal DB scaling

[01_Architecture.md §11](01_Architecture.md#11-performance--scalability-considerations)
and [16_Deployment.md §10](16_Deployment.md#10-scalability-considerations-deployment-level)
both flag this as a lever, not a current need. Trigger: reporting
queries measurably contending with transactional write latency, which
current volume (low tens of transactions/day) does not produce.

## 10. Row-versioning / point-in-time query beyond audit logs

Considered and rejected for v1 in
[14_Security.md §3](14_Security.md#3-daily-backups--version-history) —
`audit_logs` + nightly backups cover the actual "what changed, and can
we recover" needs identified so far. A temporal-tables approach would
be reconsidered only if a specific need emerges that audit logs +
backups genuinely can't satisfy (e.g., needing to run a report "as the
data looked on any arbitrary past instant," not just reconstruct a
specific record's history).

## 11. Richer OCR: handwriting, GPU acceleration

[07_OCR.md §12](07_OCR.md#12-edge-cases-exhaustive) documents
handwritten-annotation cells as low-confidence-by-design (routed to
manual entry), and
[07_OCR.md §13](07_OCR.md#13-performance) documents the CPU-only
deployment choice. Both are revisit-able if real usage shows either
becoming a frequent friction point (e.g., a supplier whose sheets are
mostly handwritten, or OCR latency becoming the bottleneck partners
actually complain about) — not built ahead of that evidence.

## 12. Learning dictionary pruning / active review UI

Flagged in [07_OCR.md §8](07_OCR.md#8-auto-correction-and-the-learning-dictionary-learning-dictionary) —
`hit_count` is tracked but not yet used to surface stale/wrong
learned corrections for an owner to review and retire. Worth building
once the dictionary has enough real accumulated entries that manual
`ocr-learning-dictionary` API browsing
([10_API.md §4](10_API.md#4-endpoints)) stops being sufficient.

## 13. Fine-grained, per-action permission matrix

[14_Security.md §1](14_Security.md#1-rbac-rbac) explains why three
roles are sufficient for the current org size. Revisit alongside
multi-tenancy (§1) or if staff headcount grows enough that "staff can
do everything except X" stops matching how the business actually wants
to delegate responsibility.
