# 21 — Web Dashboard

## 1. Purpose and non-goals {#purpose}

A phone-sized text reply is a bad place to compare six months of margin.
The dashboard exists for the things WhatsApp is genuinely worse at:
**history, comparison, and looking at the scanned sheet next to what
the system extracted from it.**

It is **read-heavy by design**. [`CLAUDE.md`](../CLAUDE.md#non-negotiable-philosophy)
rule 5 requires every mutating feature to be usable start-to-finish from
WhatsApp, and the dashboard must never become the only way to do
something. Concretely:

- **No data entry.** No purchase form, no sale form. Those flows carry
  duplicate detection, below-cost warnings and confirmation steps that
  exist once, on WhatsApp.
- **Two exceptions, both already in [10_API.md](10_API.md) §4:**
  `POST /purchases/{id}/undo` and `POST /inventory/reconcile`. Both
  delegate to the existing services. Both sit behind a typed
  confirmation in the UI, not a bare button.
- Everything else is a view.

## 2. Who uses it, and for what {#users}

Two partners, on a laptop, a few times a week — not all day. That
shapes every technical choice below: there is no concurrency problem to
solve, no offline story to build, and no audience for a single-page-app
framework's re-render performance.

| Question they actually ask | Where it's answered |
|---|---|
| Are we making money? | Overview → profit trend |
| Who owes us, and how long? | Money → receivables aging |
| What did we pay for this stock? | Stock → per-product cost history |
| Did the bot read that sheet right? | Purchases → detail, scan beside the lines |
| What changed, and who changed it? | Admin → audit log |

## 3. Pages {#pages}

| Page | Contents |
|---|---|
| **Login** | Email + password → JWT ([10_API.md §3](10_API.md#3-authentication)) |
| **Overview** | KPI row, profit trend, sales vs purchases, open alerts |
| **Stock** | Full product table, low/negative emphasis, per-product movement history |
| **Purchases** | List → detail with line items **and the original scan** |
| **Sales** | List → detail with line items and margin per line |
| **Money** | Cash & bank ledgers, receivables/payables aging, partner capital *(owner)* |
| **Reports** | P&L for a period, Excel export request + download *(owner for P&L)* |
| **Admin** | Audit log, nightly reconciliation status, settings *(owner)* |

The **purchase detail page is the one genuinely new capability** — the
scanned sheet rendered beside the extracted lines. That comparison is
impossible on WhatsApp and is exactly what builds trust in the OCR.
`purchase_headers.ocr_source_attachment_id` already links them.

## 4. What to visualise, and as what {#forms}

Form is chosen by the reader's job, before any colour decision.
Two rules that decide most of this: **a single current value is a stat
tile, not a one-bar chart**, and **more than ~7 meaningful classes is a
table, not more colours.**

### 4.1 Not charts

The KPI row is **stat tiles** — value, delta vs. previous period, and a
sparkline. Cash · Bank · Inventory value · Net profit (MTD) ·
Receivables · Payables. Net profit is the **hero figure**, set large;
it is the number the business is actually run on.

The 26-product stock listing is a **table**. Twenty-six categorical
colours would be unreadable and indistinguishable under colour-vision
deficiency; sorting and filtering answer the question better.

### 4.2 Charts

| Question | Form | Colour job |
|---|---|---|
| Is profit trending up? | line, single series | one hue; no legend — the title names it |
| Sales vs purchases over time | line, 2 series | categorical (2), legend + direct labels |
| Which stock is worth the most? | horizontal bar, top 10 | sequential, one hue |
| How old is what we're owed? | horizontal bar over 0–30/31–60/61–90/90+ | **sequential** — the buckets are ordered, so older is darker |
| Which items are below reorder level? | bar, **emphasis** | accent for the ones below, gray for the rest |
| Which sales went below cost? | diverging bar around zero margin | diverging pair, neutral midpoint |
| Cash & bank balance over time | line, 2 series | categorical (2) |

Aging is the interesting one: the buckets form an **ordered scale**, so
a sequential ramp (older = darker) reads correctly, where four
categorical hues would imply the buckets are unrelated kinds.

### 4.3 Explicitly forbidden

- **No dual-axis charts.** Two y-scales is the single most common
  charting mistake. Sales and margin-% belong in two charts or indexed
  to a common base — never sharing a plot with different scales.
- **No pie charts** of products, suppliers, or anything with more than
  two or three slices.
- **No generated colours** for a 9th series. Fold the tail into
  "Other", facet into small multiples, or use a table.
- **Colour follows the entity, never its rank.** Filtering to fewer
  suppliers must not repaint the survivors.

### 4.4 Colour must be validated, not judged

Before any palette ships, run the validator from the `dataviz` skill
(`scripts/validate_palette.js`) against both the light and dark surface
and fix every FAIL. Colour-blind safety is computable; it is never to be
eyeballed. Dark mode is a **selected** set of steps validated against
the dark surface — not an automatic inversion of the light one.

For ≥2 series a legend is always present and up to four are also
directly labelled, so identity is never carried by colour alone. Every
chart has a table view.

## 5. Technology {#tech}

**Static HTML, CSS and vanilla JavaScript. Charts as inline SVG. No
build step, no framework, no npm.**

The reasoning is maintenance, not purity. This is a two-user read-only
dashboard maintained by one person. A React toolchain adds a build
pipeline, a lockfile, and a supply chain to keep patched, in exchange
for ergonomics that matter at a scale this will never reach. Hand-built
SVG also gives exact control over the mark specs above, which charting
libraries fight.

- `frontend/` — plain files, served directly by the existing nginx.
- Auth: access token in memory, refresh token in a `Secure`,
  `HttpOnly`, `SameSite=Strict` cookie. **Not `localStorage`** — an XSS
  bug there hands over a 7-day refresh token.
- Charts render from the same JSON the API already returns. Money
  arrives as strings and is formatted for display only; it is never
  parsed into a JavaScript number, whose 53-bit float would betray the
  `NUMERIC` discipline the rest of the system keeps.

That last point is the one non-obvious constraint: **money must not
become a JS number.** Formatting and comparison use string/integer-paise
handling.

## 6. API gaps {#gaps}

Already built ([10_API.md](10_API.md)): auth, dashboard, profit-loss,
cash/bank ledgers, export request + status, products, inventory,
movements, purchases (list/detail/undo), sales (list/detail).

Still needed for the pages above:

| Endpoint | Feeds |
|---|---|
| `GET /suppliers`, `GET /suppliers/{id}` (+aging) | Money → payables |
| `GET /customers`, `GET /customers/{id}` (+aging) | Money → receivables |
| `GET /purchases/{id}/attachment` | Purchase detail — the scan |
| `GET /audit-logs` | Admin |
| `GET /reconciliation-runs` | Admin — nightly check status |
| `GET /partners/{id}/capital` | Money *(owner)* |
| `GET /reports/balance-sheet`, `/cash-flow` | Reports |
| `GET /reports/export/{job_id}/download` | Signed, expiring download |
| Time-series aggregates for the trend charts | Overview |

The aging data already exists as `SupplierRepository.stats()` /
`CustomerRepository.stats()`; those endpoints are adapters, not new
logic. The **time-series aggregates are genuinely new** — nothing
today returns "net profit per month for six months", and computing it
per request by replaying the journal would not scale. See §9.

## 7. Deployment {#deployment}

Domain: **example.com**.

Recommended: serve the ERP on a subdomain, `erp.example.com`,
leaving the apex free for anything else.

Everything needed already exists in `docker/nginx.conf`:

- TLS termination with HSTS, `nosniff`, `DENY` framing
- HTTP → HTTPS redirect, with `/.well-known/acme-challenge/` left on
  plain HTTP so certificate renewal works
- `/api/` and `/webhooks/` proxied to the app

Three steps to go live:

1. **DNS** — an `A` record for `erp.` pointing at the server.
2. **Certificate** — certbot in webroot mode against the ACME path
   already configured, writing to `docker/certs/`, which is gitignored.
   Renewal is a cron job; the nginx container reloads on new certs.
3. **A `frontend` service, or none.** Since there is no build step, the
   simplest correct answer is **no service at all**: mount `frontend/`
   into the nginx container as static root. The compose file's
   illustrative frontend service in [16_Deployment.md](16_Deployment.md)
   assumed a build; without one it is not needed.

The webhook URL stays where it is. Moving it to this domain would end
the cloudflared quick-tunnel fragility documented in
[HANDOFF.md](../HANDOFF.md) — a stable public hostname means Meta's
callback never needs repointing again. **That is arguably a bigger win
than the dashboard itself**, and it comes free with step 1.

## 8. Security {#security}

- Every request carries the JWT; `org_id` comes from the token and
  never from the URL ([10_API.md §3](10_API.md#3-authentication)) — the
  REST API's tests already assert another org's records 404.
- Owner-only pages (P&L, partner capital, admin) are enforced
  **server-side**. Hiding a nav item is presentation, not authorisation.
- The dashboard is served same-origin with the API, so no CORS is
  configured — the absence is deliberate. Adding a permissive CORS
  policy to "make local dev easier" would expose the API to any origin.
- Scanned invoices are business records: `/purchases/{id}/attachment`
  is authenticated and org-scoped like everything else, never a
  guessable static path.

## 9. Performance {#performance}

The only real concern is the trend charts. Recomputing six months of
monthly profit by replaying `journal_lines` on every page load is the
kind of query that is fine with one purchase and unacceptable with
three years of them.

[12_Dashboard.md §7](12_Dashboard.md#7-performance-considerations)
already specifies the answer: a lightweight `daily_org_metrics` rollup
refreshed nightly alongside reconciliation, which the charts read
instead of the transactional tables. That rollup does not exist yet and
is the main backend work this doc implies.

Everything else is small: the stock table is 26 rows today and a few
hundred at worst, and the KPI row is the `/dashboard` endpoint that
already exists.

## 10. Phasing {#phasing}

**Phase 1 — Login + Overview.** Auth flow, KPI row, and the two trend
charts. Requires the daily rollup (§9). This alone answers "are we
making money?"

**Phase 2 — Stock and Purchases**, including the scan-beside-lines
detail view. The highest-trust-building page in the product.

**Phase 3 — Money and Reports.** Aging charts, ledgers, P&L, export
downloads. Needs the supplier/customer endpoints.

**Phase 4 — Admin.** Audit log, reconciliation status.

Deployment (§7) can happen at any point from Phase 1 and should happen
early, because the stable webhook URL is worth having regardless.

## 11. Testing {#testing}

- API endpoint tests follow the existing pattern in
  `backend/tests/api/test_rest_api.py`: authorisation boundaries first,
  then shape.
- **Money is asserted to serialise as strings**, never numbers — there
  is already such a test, and it extends to every new endpoint.
- Charts get a rendering smoke test that opens the page and checks for
  label collisions and overflow. The palette validator checks colour;
  it does not check layout, so layout is checked by looking.
- A test that the dashboard's figures **equal the WhatsApp command's
  figures** for the same period. Two surfaces reading one service is
  the design ([12_Dashboard.md §1](12_Dashboard.md#1-two-surfaces-one-data-model));
  a test is what keeps it true.
