/* Dashboard shell -- docs/21_WebDashboard.md.
 *
 * Read-only by design (CLAUDE.md philosophy #5): the only writes this
 * page can make are signing in, signing out, and acknowledging a
 * reconciliation mismatch. Everything that changes business data stays
 * on WhatsApp.
 *
 * The access token lives in a module variable, never localStorage --
 * docs/21 §5. An XSS bug that can read localStorage hands over a
 * long-lived credential; a variable at least dies with the tab.
 */

(() => {
  const API = "/api/v1";
  let token = null;
  let me = null;

  const $ = (id) => document.getElementById(id);

  function banner(message) {
    const node = $("banner");
    node.textContent = message;
    node.hidden = false;
    clearTimeout(banner.timer);
    banner.timer = setTimeout(() => {
      node.hidden = true;
    }, 4000);
  }

  async function api(path, options = {}) {
    const response = await fetch(API + path, {
      ...options,
      headers: {
        ...(options.headers || {}),
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.body ? { "Content-Type": "application/json" } : {}),
      },
    });
    if (response.status === 401) {
      signOut();
      throw new Error("Session expired — please sign in again.");
    }
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      throw new Error(
        detail.detail ||
          detail.message ||
          `Request failed (${response.status})`,
      );
    }
    return response.json();
  }

  /* Owner-only endpoints 403 for staff. That is not an error to shout
   * about -- the section simply isn't theirs to see (docs/12 §6). */
  async function optional(path) {
    try {
      return await api(path);
    } catch {
      return null;
    }
  }

  const text = (value) => String(value ?? "");

  /* The API uses two list envelopes: paged reads return
   * {items, next_cursor}, the endpoints added for this dashboard return
   * {data}. Reading both here beats changing tested response shapes to
   * suit the client. */
  const rowsOf = (payload) =>
    (payload && (payload.items || payload.data)) || [];

  function table(columns, rows, { onRowClick } = {}) {
    const el = document.createElement("table");
    const head = el.createTHead().insertRow();
    columns.forEach((column) => {
      const th = document.createElement("th");
      th.textContent = column.label;
      if (column.numeric) th.style.textAlign = "right";
      head.append(th);
    });
    const body = el.createTBody();
    if (!rows.length) {
      const cell = body.insertRow().insertCell();
      cell.colSpan = columns.length;
      cell.className = "muted";
      cell.textContent = "Nothing here yet.";
      return el;
    }
    rows.forEach((row) => {
      const tr = body.insertRow();
      if (onRowClick) {
        tr.className = "clickable";
        tr.addEventListener("click", () => onRowClick(row));
      }
      columns.forEach((column) => {
        const td = tr.insertCell();
        const value = column.render
          ? column.render(row)
          : text(row[column.key]);
        if (value instanceof Node) td.append(value);
        else td.innerHTML = value;
        if (column.numeric) td.className = "num";
      });
    });
    return el;
  }

  /* Every sheet is fetched with the bearer token rather than linked --
   * a plain href would drop the Authorization header and come back a
   * 401. Built server-side on request, so what downloads always
   * includes any correction made since (docs/27_Documents.md), and a
   * page-level export writes the same report_jobs row the WhatsApp
   * `export` command writes: a download taken from here is exactly as
   * traceable as one taken from the chat (docs/28 §2.4). */
  function downloadButton(path, filename, label = "Download sheet") {
    const button = document.createElement("button");
    button.className = "link";
    button.textContent = label;
    button.addEventListener("click", async (event) => {
      event.stopPropagation();
      const original = button.textContent;
      button.textContent = "building…";
      button.disabled = true;
      try {
        const response = await fetch(API + path, {
          headers: { Authorization: `Bearer ${token}` },
        });
        if (!response.ok) throw new Error("That sheet couldn't be built.");
        const url = URL.createObjectURL(await response.blob());
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = filename;
        anchor.click();
        URL.revokeObjectURL(url);
      } catch (exc) {
        banner(exc.message);
      } finally {
        button.textContent = original;
        button.disabled = false;
      }
    });
    return button;
  }

  /* A page's own export, in the header beside its filters. Replaced
   * rather than appended so re-entering a page doesn't stack buttons. */
  function slot(id, path, filename, label = "Download sheet ⭳") {
    const node = $(id);
    if (node) node.replaceChildren(downloadButton(path, filename, label));
  }

  /* Our sheet, drawn from the same build that produces the workbook the
   * button beside it downloads (docs/28 §2.2). Rendering the raw lines
   * instead would put a second, differently-derived version of the bill
   * on screen -- which is the one thing a document exists to prevent. */
  function documentSheet(doc) {
    const wrap = document.createElement("div");
    wrap.className = "sheet";

    const caption = document.createElement("p");
    caption.className = "sheet-caption";
    caption.textContent = doc.caption;
    wrap.append(caption);

    if (doc.banner) {
      const warning = document.createElement("p");
      warning.className = "sheet-banner";
      warning.textContent = doc.banner;
      wrap.append(warning);
    }

    const scroll = document.createElement("div");
    scroll.className = "table-scroll";
    const el = document.createElement("table");
    const head = el.createTHead().insertRow();
    doc.columns.forEach((label, index) => {
      const th = document.createElement("th");
      th.textContent = label;
      if (index >= 5 && index <= 8) th.style.textAlign = "right";
      head.append(th);
    });
    const body = el.createTBody();
    doc.rows.forEach((row) => {
      const tr = body.insertRow();
      // a band of colour is findable at 52 lines; one tinted cell is not
      if (row.changed) tr.className = "changed";
      row.cells.forEach((value, index) => {
        const td = tr.insertCell();
        td.textContent = value;
        if (index >= 5 && index <= 8) td.className = "num";
      });
    });
    const totals = body.insertRow();
    totals.className = "totals";
    doc.totals.forEach((value, index) => {
      const td = totals.insertCell();
      td.textContent = value;
      if (index >= 5 && index <= 8) td.className = "num";
    });
    scroll.append(el);
    wrap.append(scroll);

    if (doc.notes && doc.notes.length) {
      const notes = document.createElement("ul");
      notes.className = "sheet-notes";
      doc.notes.forEach((note) => {
        const li = document.createElement("li");
        li.textContent = note;
        notes.append(li);
      });
      wrap.append(notes);
    }

    if (doc.history && doc.history.length) {
      // available, not in the way: nineteen lines of audit trail open
      // by default would bury the bill they annotate
      const details = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = `CHANGES (${doc.history.length})`;
      details.append(summary);
      const list = document.createElement("ul");
      list.className = "sheet-history";
      doc.history.forEach((entry) => {
        const li = document.createElement("li");
        li.textContent = entry;
        list.append(li);
      });
      details.append(list);
      wrap.append(details);
    }
    return wrap;
  }

  /* Documents that have no page of their own -- a payment receipt is
   * reached from a ledger row and from a payables row, and giving it a
   * page would mean navigating away from the list you are working. */
  function showDocument(path, downloadPath, filename) {
    const overlay = document.createElement("div");
    overlay.className = "overlay";
    const card = document.createElement("div");
    card.className = "card overlay-card";
    card.innerHTML = `<p class="muted">Building…</p>`;
    overlay.append(card);
    overlay.addEventListener("click", (event) => {
      if (event.target === overlay) overlay.remove();
    });
    document.body.append(overlay);

    api(path)
      .then((doc) => {
        card.replaceChildren();
        const head = document.createElement("div");
        head.className = "detail-head";
        const title = document.createElement("h2");
        title.textContent = doc.title || "Document";
        head.append(title);
        const actions = document.createElement("div");
        actions.className = "filters";
        actions.append(downloadButton(downloadPath, filename));
        const close = document.createElement("button");
        close.className = "link";
        close.textContent = "Close";
        close.addEventListener("click", () => overlay.remove());
        actions.append(close);
        head.append(actions);
        card.append(head, documentSheet(doc));
      })
      .catch((exc) => {
        card.innerHTML = `<p class="error">${text(exc.message)}</p>`;
        setTimeout(() => overlay.remove(), 2500);
      });
  }

  function money(value) {
    const formatted = Money.format(value);
    return Money.isNegative(value)
      ? `<span style="color:var(--bad)">${formatted}</span>`
      : formatted;
  }

  // ------------------------------------------------------------ auth

  async function signIn(event) {
    event.preventDefault();
    const error = $("login-error");
    error.hidden = true;
    try {
      const body = JSON.stringify({
        email: $("email").value,
        password: $("password").value,
      });
      const response = await fetch(`${API}/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body,
      });
      if (!response.ok) throw new Error("Those details weren't accepted.");
      const payload = await response.json();
      token = payload.access_token;
      me = { full_name: payload.full_name, role: payload.role };
      $("login").hidden = true;
      $("app").hidden = false;
      $("whoami").textContent = `${me.full_name} · ${me.role}`;
      await showPage("overview");
    } catch (exc) {
      error.textContent = exc.message;
      error.hidden = false;
    }
  }

  function signOut() {
    token = null;
    me = null;
    $("app").hidden = true;
    $("login").hidden = false;
  }

  // -------------------------------------------------------- overview

  function kpi(label, value, sub, { negative = false } = {}) {
    return `<div class="kpi">
      <div class="label">${label}</div>
      <div class="value${negative ? " negative" : ""}">${value}</div>
      ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>`;
  }

  async function loadOverview() {
    const data = await api("/dashboard");
    $("kpis").innerHTML = [
      kpi("Cash", Money.format(data.cash_balance), "In hand", {
        negative: Money.isNegative(data.cash_balance),
      }),
      kpi("Bank", Money.format(data.bank_balance), "Balance", {
        negative: Money.isNegative(data.bank_balance),
      }),
      kpi(
        "Stock value",
        Money.format(data.inventory.value),
        `${data.inventory.active_products} products`,
      ),
      kpi(
        "Receivables",
        Money.format(data.receivables.total),
        `${data.receivables.parties} party(ies)`,
      ),
      kpi(
        "Payables",
        Money.format(data.payables.total),
        `${data.payables.parties} party(ies)`,
      ),
      // Owner-only, so it is absent rather than zero for staff -- the
      // API omits the key entirely (docs/21 §6).
      ...(data.partner_capital
        ? [
            kpi(
              "Partner capital",
              Money.format(
                Money.sum(data.partner_capital.map((row) => row.balance)),
              ),
              `${data.partner_capital.length} partner(s)`,
            ),
          ]
        : []),
    ].join("");

    const capitalCard = $("capital-card");
    if (data.partner_capital && data.partner_capital.length) {
      capitalCard.hidden = false;
      $("capital").replaceChildren(
        table(
          [
            { label: "Partner", key: "partner" },
            {
              label: "Principal",
              numeric: true,
              render: (row) => money(row.balance),
            },
          ],
          [
            ...data.partner_capital,
            {
              partner: "Total",
              balance: Money.sum(
                data.partner_capital.map((row) => row.balance),
              ),
            },
          ],
        ),
      );
    } else {
      capitalCard.hidden = true;
    }

    const alerts = [];
    if (Number(data.inventory.negative_stock_count) > 0) {
      alerts.push(
        `<p><span class="pill bad">Negative stock</span> ${data.inventory.negative_stock_count} product(s) below zero — a sale was recorded against stock that wasn't there.</p>`,
      );
    }
    if (Number(data.inventory.low_stock_count) > 0) {
      alerts.push(
        `<p><span class="pill warn">Low stock</span> ${data.inventory.low_stock_count} product(s) at or under their reorder level.</p>`,
      );
    }
    const runs = await optional(
      "/inventory/reconciliations?unacknowledged=true",
    );
    const openRuns = rowsOf(runs);
    if (openRuns.length) {
      alerts.push(
        `<p><span class="pill bad">Reconciliation</span> ${openRuns.length} unacknowledged mismatch(es) — see Admin.</p>`,
      );
    }
    $("alerts").innerHTML =
      alerts.join("") ||
      `<p class="muted">Nothing needs attention right now.</p>`;

    // Profit is owner-only; a staff account simply gets no trend, which
    // is why this degrades rather than erroring.
    const metrics = await optional("/metrics/monthly?months=6");
    if (metrics) {
      Charts.profitColumns($("chart-profit"), rowsOf(metrics));
      Charts.revenueColumns($("chart-revenue"), rowsOf(metrics));
    } else {
      const note = `<p class="muted">Profit figures are owner-only.</p>`;
      $("chart-profit").innerHTML = note;
      $("chart-revenue").innerHTML = note;
    }
  }

  // ----------------------------------------------------------- stock

  let stockRows = [];

  function renderStock(filter = "") {
    const needle = filter.trim().toLowerCase();
    const rows = needle
      ? stockRows.filter(
          (row) =>
            row.code.toLowerCase().includes(needle) ||
            (row.brand || "").toLowerCase().includes(needle) ||
            (row.description || "").toLowerCase().includes(needle),
        )
      : stockRows;

    $("stock-table").replaceChildren(
      table(
        [
          { label: "Code", key: "code" },
          {
            // a code is unique only within a brand, so a stock list
            // without this column can't tell two of them apart
            label: "Brand",
            render: (row) =>
              row.brand
                ? text(row.brand)
                : `<span class="muted">not set</span>`,
          },
          { label: "Description", key: "description" },
          {
            label: "On hand",
            numeric: true,
            render: (row) => text(row.qty_on_hand),
          },
          {
            label: "Avg cost",
            numeric: true,
            render: (row) => money(row.avg_cost),
          },
          { label: "Value", numeric: true, render: (row) => money(row.value) },
          {
            label: "",
            render: (row) =>
              Money.isNegative(row.qty_on_hand)
                ? `<span class="pill bad">negative</span>`
                : "",
          },
        ],
        rows,
        { onRowClick: (row) => loadMovements(row) },
      ),
    );
  }

  async function loadStock() {
    slot("dl-stock", "/exports/stock.xlsx", "stock.xlsx");
    $("stock-detail").hidden = true;
    const payload = await api("/inventory?limit=200");
    stockRows = rowsOf(payload);
    renderStock($("stock-search").value);
  }

  /* Where the stock actually went. A balance answers "how much"; only
   * the movements answer "why", which is the question someone opens
   * this page with when a number looks wrong. */
  async function loadMovements(product) {
    const payload = await api(`/inventory/${product.id}/movements?limit=200`);
    const rows = rowsOf(payload);
    const panel = $("stock-detail");
    panel.replaceChildren();

    const head = document.createElement("div");
    head.className = "detail-head";
    const brand = product.brand ? ` · ${text(product.brand)}` : "";
    head.innerHTML = `
      <div>
        <h2>${text(product.code)}${brand}</h2>
        <p class="muted">${text(product.description)} — ${text(product.qty_on_hand)} ${text(
          product.unit,
        )} on hand @ ${money(product.avg_cost)} avg</p>
      </div>
      <button class="link" id="close-stock">Close</button>`;
    panel.append(head);

    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    wrap.append(
      table(
        [
          { label: "When", render: (row) => text(row.at).slice(0, 16).replace("T", " ") },
          { label: "What", render: (row) => text(row.type).replace(/_/g, " ") },
          { label: "From", render: (row) => text(row.origin || row.reason || "") },
          {
            // the sign is the whole point: in or out
            label: "Change",
            numeric: true,
            render: (row) =>
              String(row.qty_delta).startsWith("-")
                ? `<span style="color:var(--bad)">${text(row.qty_delta)}</span>`
                : `<span style="color:var(--good)">+${text(row.qty_delta)}</span>`,
          },
          { label: "Balance after", numeric: true, render: (row) => text(row.resulting_qty) },
          {
            label: "Avg cost after",
            numeric: true,
            render: (row) => money(row.resulting_avg_cost),
          },
        ],
        // oldest first: a stock history reads as a story, and the API
        // returns newest-first for the dashboard's other uses
        [...rows].reverse(),
      ),
    );
    panel.append(wrap);
    panel.hidden = false;
    $("close-stock").addEventListener("click", () => {
      panel.hidden = true;
    });
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ------------------------------------------------------- purchases

  async function loadPurchases() {
    slot(
      "dl-purchases",
      "/exports/purchases.xlsx",
      "purchases.xlsx",
      "Download all bills ⭳",
    );
    const payload = await api("/purchases?limit=50");
    $("purchase-detail").hidden = true;
    $("purchases-table").replaceChildren(
      table(
        [
          { label: "Date", key: "date" },
          { label: "Invoice", key: "invoice_no" },
          { label: "Supplier", key: "supplier" },
          {
            label: "Total",
            numeric: true,
            render: (row) => money(row.grand_total),
          },
          {
            label: "Paid",
            numeric: true,
            render: (row) => money(row.amount_paid),
          },
          {
            label: "Status",
            render: (row) =>
              `<span class="pill ${row.payment_status === "paid" ? "good" : "warn"}">${row.payment_status}</span>`,
          },
          {
            label: "",
            render: (row) =>
              downloadButton(
                `/purchases/${row.id}/sheet`,
                `purchase-${row.invoice_no}.xlsx`,
                "Sheet ⭳",
              ),
          },
        ],
        rowsOf(payload),
        { onRowClick: (row) => loadPurchaseDetail(row.id) },
      ),
    );
  }

  /* The one genuinely new capability over WhatsApp (docs/21 §3): the
   * scanned sheet beside *our* sheet built from the same data the
   * workbook downloads from -- so the original and our arithmetic can be
   * compared line for line, corrections included (docs/28 §2.3). */
  async function loadPurchaseDetail(id) {
    const [detail, doc] = await Promise.all([
      api(`/purchases/${id}`),
      api(`/purchases/${id}/document`),
    ]);
    const panel = $("purchase-detail");

    panel.replaceChildren();
    const head = document.createElement("div");
    head.className = "detail-head";
    const title = document.createElement("div");
    title.innerHTML = `
      <h2>${text(detail.invoice_no)} · ${text(detail.supplier)}</h2>
      <p class="muted">${text(detail.date)} · ${money(detail.grand_total)}</p>`;
    head.append(title);

    const actions = document.createElement("div");
    actions.className = "filters";
    actions.append(
      downloadButton(
        `/purchases/${id}/sheet`,
        `purchase-${detail.invoice_no}.xlsx`,
        "Our sheet ⭳",
      ),
    );
    if (detail.scan_url) {
      const scan = document.createElement("button");
      scan.className = "link";
      scan.textContent = "Original scan ⭳";
      scan.addEventListener("click", async () => {
        try {
          const response = await fetch(API + detail.scan_url, {
            headers: { Authorization: `Bearer ${token}` },
          });
          if (!response.ok) throw new Error("The scan couldn't be loaded.");
          const url = URL.createObjectURL(await response.blob());
          const anchor = document.createElement("a");
          anchor.href = url;
          anchor.download = `scan-${detail.invoice_no}`;
          anchor.click();
          URL.revokeObjectURL(url);
        } catch (exc) {
          banner(exc.message);
        }
      });
      actions.append(scan);
    }
    const closer = document.createElement("button");
    closer.className = "link";
    closer.id = "close-detail";
    closer.textContent = "Close";
    actions.append(closer);
    head.append(actions);
    panel.append(head);

    const beside = document.createElement("div");
    beside.className = "scan-beside";
    const left = document.createElement("div");
    left.append(documentSheet(doc));
    beside.append(left);

    const right = document.createElement("div");
    right.className = "scan-pane";
    if (detail.scan_url) {
      const img = document.createElement("img");
      img.alt = `Scanned sheet for ${text(detail.invoice_no)}`;
      img.loading = "lazy";
      img.title = "Click to view full size";
      // a photographed sheet is often unreadable at column width, so
      // clicking drops the fit-to-column constraint
      img.addEventListener("click", () => img.classList.toggle("zoomed"));
      // the image endpoint needs the bearer token, so it is fetched
      // rather than set as a plain src
      fetch(API + detail.scan_url, {
        headers: { Authorization: `Bearer ${token}` },
      })
        .then((response) =>
          response.ok ? response.blob() : Promise.reject(response),
        )
        .then((blob) => {
          img.src = URL.createObjectURL(blob);
        })
        .catch(() => {
          right.innerHTML = `<p class="muted">The original scan couldn't be loaded.</p>`;
        });
      right.append(img);
    } else {
      right.innerHTML = `<p class="muted">This purchase was typed in, not photographed.</p>`;
    }
    beside.append(right);
    panel.append(beside);
    panel.hidden = false;
    $("close-detail").addEventListener("click", () => {
      panel.hidden = true;
    });
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ----------------------------------------------------------- sales

  async function loadSales() {
    slot(
      "dl-sales",
      "/exports/sales.xlsx",
      "sales.xlsx",
      "Download all sales ⭳",
    );
    const payload = await api("/sales?limit=50");
    $("sale-detail").hidden = true;
    $("sales-table").replaceChildren(
      table(
        [
          { label: "Date", render: (row) => text(row.date || row.sale_date) },
          { label: "Customer", key: "customer" },
          {
            label: "Total",
            numeric: true,
            render: (row) => money(row.grand_total),
          },
          {
            label: "Paid",
            numeric: true,
            render: (row) => money(row.amount_paid),
          },
          {
            label: "Payment",
            render: (row) =>
              `<span class="pill ${row.payment_status === "paid" ? "good" : "warn"}">${text(
                row.payment_status,
              )}</span>`,
          },
          { label: "Status", key: "status" },
          {
            label: "",
            render: (row) =>
              downloadButton(
                `/sales/${row.id}/sheet`,
                `sale-${row.id.slice(0, 8)}.xlsx`,
                "Sheet ⭳",
              ),
          },
        ],
        rowsOf(payload),
        { onRowClick: (row) => loadSaleDetail(row.id) },
      ),
    );
  }

  /* Margin per line is the reason to open a sale: what it sold for
   * against what it cost us (docs/21 §3). */
  async function loadSaleDetail(id) {
    const [detail, doc] = await Promise.all([
      api(`/sales/${id}`),
      api(`/sales/${id}/document`),
    ]);
    const panel = $("sale-detail");
    panel.replaceChildren();

    const head = document.createElement("div");
    head.className = "detail-head";
    const title = document.createElement("div");
    title.innerHTML = `
      <h2>${text(detail.customer)}</h2>
      <p class="muted">${text(detail.date || detail.sale_date)} · ${money(detail.grand_total)}</p>`;
    head.append(title);
    const actions = document.createElement("div");
    actions.className = "filters";
    actions.append(
      downloadButton(
        `/sales/${id}/sheet`,
        `sale-${id.slice(0, 8)}.xlsx`,
        "Sheet ⭳",
      ),
    );
    const closer = document.createElement("button");
    closer.className = "link";
    closer.id = "close-sale";
    closer.textContent = "Close";
    actions.append(closer);
    head.append(actions);
    panel.append(head);

    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    wrap.append(
      table(
        [
          { label: "#", key: "line_no" },
          { label: "Code", key: "code" },
          { label: "Qty", numeric: true, key: "qty" },
          { label: "Rate", numeric: true, render: (row) => money(row.rate) },
          {
            label: "Cost",
            numeric: true,
            render: (row) => money(row.cost || "0"),
          },
          {
            label: "Total",
            numeric: true,
            render: (row) => money(row.line_total),
          },
          {
            label: "Margin",
            numeric: true,
            render: (row) => {
              if (row.margin === undefined || row.margin === null) return "";
              // a line sold under cost is the thing worth seeing here
              return Money.isNegative(row.margin)
                ? `<span class="pill bad">${Money.format(row.margin)}</span>`
                : money(row.margin);
            },
          },
        ],
        detail.lines || [],
      ),
    );
    panel.append(wrap);
    // The margin table above answers "was this worth selling"; the sheet
    // below is the invoice itself, identical to what downloads.
    panel.append(documentSheet(doc));
    panel.hidden = false;
    $("close-sale").addEventListener("click", () => {
      panel.hidden = true;
    });
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // --------------------------------------------------------- parties

  let partyRows = [];

  function renderParties() {
    const kind = $("party-kind").value;
    const needle = $("party-search").value.trim().toLowerCase();
    const rows = partyRows.filter(
      (row) =>
        (!kind || row.kind === kind) &&
        (!needle || row.name.toLowerCase().includes(needle)),
    );

    $("parties-table").replaceChildren(
      table(
        [
          { label: "Name", key: "name" },
          {
            label: "Side",
            render: (row) =>
              `<span class="pill ${row.kind === "supplier" ? "warn" : "good"}">${row.kind}</span>`,
          },
          { label: "Phone", render: (row) => text(row.phone) },
          {
            // 0.00 is a fact worth showing: it is the difference between
            // "settled up" and "never traded with"
            label: "Outstanding",
            numeric: true,
            render: (row) => money(row.outstanding),
          },
          {
            label: "Oldest unpaid",
            render: (row) => text(row.oldest_date || ""),
          },
        ],
        rows,
        { onRowClick: (row) => loadPartyLedger(row) },
      ),
    );
  }

  async function loadParties() {
    slot(
      "dl-parties",
      "/exports/parties.xlsx?role=supplier",
      "suppliers.xlsx",
      "Suppliers ⭳",
    );
    $("party-detail").hidden = true;
    partyRows = rowsOf(await api("/parties"));
    renderParties();
  }

  async function loadPartyLedger(party) {
    const detail = await api(`/parties/${party.kind}/${party.id}/ledger`);
    const panel = $("party-detail");
    panel.replaceChildren();

    const head = document.createElement("div");
    head.className = "detail-head";
    const owes = party.kind === "supplier" ? "owed to them" : "owed by them";
    const title = document.createElement("div");
    title.innerHTML = `
      <h2>${text(detail.name)}</h2>
      <p class="muted">${text(detail.kind)} · ${money(detail.balance)} ${owes}</p>`;
    head.append(title);
    const actions = document.createElement("div");
    actions.className = "filters";
    actions.append(
      downloadButton(
        `/exports/statement.xlsx?kind=${party.kind}&party_id=${party.id}`,
        `statement-${party.name.replace(/\W+/g, "-")}.xlsx`,
        "Statement ⭳",
      ),
    );
    const closer = document.createElement("button");
    closer.className = "link";
    closer.id = "close-party";
    closer.textContent = "Close";
    actions.append(closer);
    head.append(actions);
    panel.append(head);

    const wrap = document.createElement("div");
    wrap.className = "table-scroll";
    wrap.append(
      table(
        [
          { label: "Date", key: "date" },
          { label: "What", key: "kind" },
          { label: "Reference", render: (row) => text(row.reference) },
          {
            label: "Billed",
            numeric: true,
            render: (row) => (Money.isZero(row.debit) ? "" : money(row.debit)),
          },
          {
            label: "Settled",
            numeric: true,
            render: (row) =>
              Money.isZero(row.credit) ? "" : money(row.credit),
          },
          {
            label: "Balance",
            numeric: true,
            render: (row) => money(row.balance),
          },
        ],
        detail.data,
      ),
    );
    panel.append(wrap);
    panel.hidden = false;
    $("close-party").addEventListener("click", () => {
      panel.hidden = true;
    });
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  // ---------------------------------------------------------- ledger

  let ledgerRows = [];

  function renderLedger() {
    const account = $("ledger-account").value;
    const needle = $("ledger-search").value.trim().toLowerCase();
    const rows = ledgerRows.filter(
      (row) =>
        (!account || row.account === account) &&
        (!needle ||
          String(row.notes || "")
            .toLowerCase()
            .includes(needle) ||
          String(row.type || "")
            .toLowerCase()
            .includes(needle)),
    );

    // A reversal and the entry it reversed both stay in the list --
    // nothing is deleted -- but neither is money that moved, and
    // counting them made a month of 1cr look like 2.3cr.
    const counted = rows.filter((row) => !row.cancelled);
    const inflow = Money.sum(
      counted.filter((r) => !Money.isNegative(r.amount)).map((r) => r.amount),
    );
    const outflow = Money.sum(
      counted.filter((r) => Money.isNegative(r.amount)).map((r) => r.amount),
    );
    const undone = rows.length - counted.length;
    $("ledger-totals").innerHTML = [
      kpi(
        "Money in",
        Money.format(inflow),
        `${counted.length} entries counted`,
      ),
      kpi("Money out", Money.format(outflow), "for this filter", {
        negative: true,
      }),
      ...(undone
        ? [kpi("Cancelled", String(undone), "reversed entries, not counted")]
        : []),
    ].join("");

    $("ledger-table").replaceChildren(
      table(
        [
          { label: "Date", key: "date" },
          { label: "Account", key: "account" },
          { label: "Type", key: "type" },
          {
            label: "Amount",
            numeric: true,
            render: (row) => money(row.amount),
          },
          // the running balance is per account, so it is meaningless
          // beside a row from the other one -- hence the filter above
          {
            label: "Balance",
            numeric: true,
            render: (row) => money(row.resulting_balance),
          },
          {
            label: "Notes",
            render: (row) =>
              row.cancelled
                ? `<span class="muted">${text(row.notes || "")} · reversed</span>`
                : text(row.notes || ""),
          },
          {
            // A settlement has a receipt; an expense or a capital
            // contribution has no second document to open.
            label: "",
            render: (row) =>
              row.reference ? receiptButton(row.reference) : "",
          },
        ],
        rows,
      ),
    );
  }

  function receiptButton(reference) {
    const button = document.createElement("button");
    button.className = "link";
    button.textContent = "Receipt";
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      showDocument(
        `/payments/${reference}/document`,
        `/payments/${reference}/sheet`,
        `payment-${reference}.xlsx`,
      );
    });
    return button;
  }

  async function loadLedger() {
    // The cashbook, not the party ledger: the partners call both
    // "ledger", and this tab is the one about money that moved.
    slot(
      "dl-ledger",
      "/exports/cashbook.xlsx",
      "cashbook.xlsx",
      "Download cashbook ⭳",
    );
    const [cash, bank] = await Promise.all([
      optional("/ledgers/cash?limit=200"),
      optional("/ledgers/bank?limit=200"),
    ]);
    ledgerRows = [
      ...((cash && cash.entries) || []).map((row) => ({
        ...row,
        account: "Cash",
      })),
      ...((bank && bank.entries) || []).map((row) => ({
        ...row,
        account: "Bank",
      })),
    ].sort((a, b) => String(b.date).localeCompare(String(a.date)));
    renderLedger();
  }

  // ----------------------------------------------------------- money

  async function loadMoney() {
    slot(
      "dl-receivables",
      "/exports/parties.xlsx?role=customer",
      "customers.xlsx",
      "All customers ⭳",
    );
    slot(
      "dl-payables",
      "/exports/parties.xlsx?role=supplier",
      "suppliers.xlsx",
      "All suppliers ⭳",
    );
    const [receivables, payables] = await Promise.all([
      optional("/receivables"),
      optional("/payables"),
    ]);

    // The statement is the answer to the question this page raises --
    // "why is that the number?" -- so it belongs on the row, not three
    // clicks away on another tab.
    const partyColumns = (kind) => [
      { label: "Name", key: "name" },
      {
        label: "Outstanding",
        numeric: true,
        render: (row) => money(row.outstanding),
      },
      { label: "Oldest", render: (row) => text(row.oldest_date || "") },
      {
        label: "",
        render: (row) =>
          downloadButton(
            `/exports/statement.xlsx?kind=${kind}&party_id=${row.id}`,
            `statement-${String(row.name).replace(/\W+/g, "-")}.xlsx`,
            "Statement ⭳",
          ),
      },
    ];
    $("receivables").replaceChildren(
      table(partyColumns("customer"), rowsOf(receivables)),
    );
    $("payables").replaceChildren(
      table(partyColumns("supplier"), rowsOf(payables)),
    );
  }

  // ----------------------------------------------------------- admin

  async function loadAdmin() {
    const runs = await optional("/inventory/reconciliations");
    const container = $("reconciliations");
    container.replaceChildren();

    if (!rowsOf(runs).length) {
      container.innerHTML = `<p class="muted">No reconciliation runs recorded yet.</p>`;
    } else {
      container.append(
        table(
          [
            {
              label: "Started",
              render: (row) => text(row.started_at || "").slice(0, 16),
            },
            { label: "Kind", key: "kind" },
            {
              label: "Result",
              render: (row) =>
                row.status === "ok"
                  ? `<span class="pill good">ok</span>`
                  : `<span class="pill bad">${row.status}</span>`,
            },
            { label: "Checked", numeric: true, key: "checked_count" },
            { label: "Mismatches", numeric: true, key: "mismatch_count" },
            {
              label: "",
              render: (row) => {
                if (row.mismatch_count === 0) return "";
                if (row.acknowledged_at)
                  return `<span class="muted">acknowledged</span>`;
                const button = document.createElement("button");
                button.className = "link";
                button.textContent = "Acknowledge";
                button.addEventListener("click", async (event) => {
                  event.stopPropagation();
                  try {
                    await api(`/inventory/reconcile/${row.id}/acknowledge`, {
                      method: "POST",
                    });
                    banner(
                      "Recorded that you've seen it. The figures are unchanged.",
                    );
                    await loadAdmin();
                  } catch (exc) {
                    banner(exc.message);
                  }
                });
                return button;
              },
            },
          ],
          rowsOf(runs),
        ),
      );
    }

    await loadAudit();
  }

  /* The audit trail with the payload every mutation already writes.
   * Twenty rows reading "product.created" answer nothing; the same rows
   * naming the product, and expanding to the exact before/after, are a
   * record of what happened. */
  let auditRows = [];

  //: fields that are money wherever they appear, so they format as money
  const MONEY_FIELDS = new Set([
    "amount",
    "grand_total",
    "line_total",
    "rate",
    "applied",
    "advance",
    "outstanding",
    "balance",
    "total",
  ]);

  const IDENTIFYING = [
    "code",
    "name",
    "invoice_no",
    "reference",
    "amount",
    "via",
    "description",
  ];

  function fieldValue(key, value) {
    if (value === undefined || value === null || value === "") return "—";
    if (Array.isArray(value)) {
      return value.map((item) => describe(item)).join("; ") || "—";
    }
    if (typeof value === "object") return describe(value);
    return MONEY_FIELDS.has(key) ? Money.format(value) : text(value);
  }

  /* A nested object rendered as "applied ₹40,00,000.00, reference 001"
   * rather than raw JSON -- the allocations array is the useful part of
   * a payment and was unreadable as a blob. */
  function describe(item) {
    if (item === null || typeof item !== "object") return text(item);
    return Object.entries(item)
      .map(([key, value]) => `${key} ${fieldValue(key, value)}`)
      .join(", ");
  }

  function summarise(entry) {
    const state = entry.after_state || entry.before_state || {};
    // Data-driven rather than a branch per action: payload fields are
    // named consistently across services, so a new action reads well
    // without anyone editing this. Zero-valued fields are dropped --
    // "advance 0.00" on every payment is noise, not information.
    const meaningful = (key) => {
      const value = state[key];
      if (value === undefined || value === null || value === "") return false;
      if (typeof value === "object") return false;
      return !(MONEY_FIELDS.has(key) && Money.toPaise(value) === 0n);
    };
    const keys = [
      ...IDENTIFYING.filter(meaningful),
      ...Object.keys(state).filter(
        (key) => !IDENTIFYING.includes(key) && meaningful(key),
      ),
    ].slice(0, 3);
    if (!keys.length) return `<span class="muted">no detail recorded</span>`;
    return keys
      .map((key) => `<b>${fieldValue(key, state[key])}</b>`)
      .join(" · ");
  }

  function detailPanel(entry) {
    const before = entry.before_state || {};
    const after = entry.after_state || {};
    const fields = [
      ...new Set([...Object.keys(before), ...Object.keys(after)]),
    ];
    if (!fields.length) {
      return `<p class="muted">Nothing was recorded beyond the action itself.</p>`;
    }

    // Something created has no "before". Showing a Before column full of
    // em dashes says nothing; a diff is only a diff when both sides
    // exist.
    const isChange = Object.keys(before).length > 0;
    if (!isChange) {
      const rows = fields
        .map(
          (field) =>
            `<div class="field">${field}</div><div>${fieldValue(field, after[field])}</div>`,
        )
        .join("");
      return `<div class="record-grid">${rows}</div>`;
    }

    const rows = fields
      .map((field) => {
        const was = before[field];
        const now = after[field];
        const changed = JSON.stringify(was) !== JSON.stringify(now);
        return `<div class="field">${field}</div>
          <div class="${changed ? "was" : "muted"}">${fieldValue(field, was)}</div>
          <div class="${changed ? "changed" : ""}">${fieldValue(field, now)}</div>`;
      })
      .join("");

    return `<div class="change-grid">
        <div class="head">Field</div><div class="head">Before</div><div class="head">After</div>
        ${rows}
      </div>`;
  }

  function renderAudit() {
    const action = $("audit-action").value;
    const needle = $("audit-search").value.trim().toLowerCase();
    const rows = auditRows.filter((entry) => {
      if (action && entry.action !== action) return false;
      if (!needle) return true;
      return JSON.stringify(entry).toLowerCase().includes(needle);
    });

    const built = table(
      [
        {
          label: "When",
          render: (row) => text(row.created_at).slice(0, 16).replace("T", " "),
        },
        { label: "Action", key: "action" },
        { label: "What", render: summarise },
        { label: "By", render: (row) => text(row.actor || "") },
        { label: "Channel", key: "channel" },
      ],
      rows,
    );

    built.querySelectorAll("tbody tr").forEach((tr, index) => {
      const cell = tr.cells[2];
      if (!cell) return;
      cell.classList.add("summary");
      tr.classList.add("clickable");
      tr.addEventListener("click", () => {
        const next = tr.nextElementSibling;
        if (next && next.classList.contains("detail-row")) {
          next.remove();
          return;
        }
        const detail = tr.parentElement.insertRow(tr.rowIndex);
        detail.className = "detail-row";
        const td = detail.insertCell();
        td.colSpan = 5;
        td.innerHTML = detailPanel(rows[index]);
      });
    });

    $("audit").replaceChildren(built);
  }

  async function loadAudit() {
    const [entries, actions] = await Promise.all([
      optional("/audit?limit=200"),
      optional("/audit/actions"),
    ]);
    auditRows = rowsOf(entries);

    const select = $("audit-action");
    const chosen = select.value;
    select.replaceChildren(new Option("All actions", ""));
    rowsOf(actions).forEach((row) => {
      select.append(new Option(`${row.action} (${row.count})`, row.action));
    });
    select.value = chosen;

    renderAudit();
  }

  // ------------------------------------------------------ navigation

  const LOADERS = {
    overview: loadOverview,
    stock: loadStock,
    purchases: loadPurchases,
    sales: loadSales,
    parties: loadParties,
    ledger: loadLedger,
    money: loadMoney,
    admin: loadAdmin,
  };

  async function showPage(name) {
    document.querySelectorAll("#nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.page === name);
    });
    document.querySelectorAll(".page").forEach((page) => {
      page.hidden = page.id !== `page-${name}`;
    });
    try {
      await LOADERS[name]();
    } catch (exc) {
      banner(exc.message);
    }
  }

  $("login-form").addEventListener("submit", signIn);
  $("signout").addEventListener("click", signOut);
  $("stock-search").addEventListener("input", (event) =>
    renderStock(event.target.value),
  );
  $("party-kind").addEventListener("change", renderParties);
  $("party-search").addEventListener("input", renderParties);
  $("ledger-account").addEventListener("change", renderLedger);
  $("ledger-search").addEventListener("input", renderLedger);
  $("audit-action").addEventListener("change", renderAudit);
  $("audit-search").addEventListener("input", renderAudit);
  document.querySelectorAll("#nav button").forEach((button) => {
    button.addEventListener("click", () => showPage(button.dataset.page));
  });
})();
