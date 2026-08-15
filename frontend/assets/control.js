/* Master Control — invoice entry. ui-plan.md §5.
 *
 * No framework and no build step, as everywhere else here. The one
 * genuinely fiddly component is the picker, and it is fiddly because of
 * what it has to show rather than how it is built: a code names a
 * product only together with its brand — three share `55X` on these
 * books — so the list carries the brand and the stock that tells them
 * apart. Choosing by looking is the single reason this beats the chat.
 *
 * Money never becomes a JavaScript number. Totals are computed in
 * integer paise through money.js, and the server recomputes them on
 * save; what it returns is what gets displayed afterwards.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // In memory only. A control session is 30 minutes and has no refresh
  // token; putting it in localStorage would give an XSS bug a key to
  // the half of the app that can delete things.
  let token = null;
  let kind = "purchase";
  let draftKey = null;

  const DRAFT_STORE = "control:draft";
  const DRAFT_TTL_DAYS = 7;

  async function api(path, options = {}) {
    const response = await fetch(`/api/v1${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: `Bearer ${token}` } : {}),
        ...(options.headers || {}),
      },
    });
    // A 401 means "expired" only if we had a session to expire. The
    // sign-in request goes through here too, and reporting a wrong
    // password as an expired session sends someone to the screen they
    // are already on, with the wrong reason.
    if (response.status === 401 && token) {
      token = null;
      $("app").hidden = true;
      $("signin").hidden = false;
      throw new Error("Session expired — sign in again.");
    }
    const body = response.status === 204 ? null : await response.json();
    if (!response.ok) {
      throw new Error(body?.detail || body?.message || `Request failed (${response.status})`);
    }
    return body;
  }

  function banner(message, ok = false) {
    const node = $("banner");
    node.textContent = message;
    node.className = ok ? "banner ok" : "banner";
    node.hidden = false;
    if (ok) setTimeout(() => (node.hidden = true), 6000);
  }

  // ------------------------------------------------------------ combo

  /** Text input + filtered list + keyboard. Selection is by looking,
   *  never by the code alone. */
  function combo(input, listNode, { fetchItems, render, onPick, onClear }) {
    let items = [];
    let active = -1;
    let timer = null;

    const close = () => {
      listNode.hidden = true;
      active = -1;
    };

    const paint = () => {
      listNode.replaceChildren();
      items.forEach((item, index) => {
        const row = render(item);
        row.className = `combo-item${item.__create ? " create" : ""}`;
        row.setAttribute("role", "option");
        row.setAttribute("aria-selected", String(index === active));
        row.addEventListener("mousedown", (event) => {
          event.preventDefault();
          choose(index);
        });
        listNode.append(row);
      });
      listNode.hidden = items.length === 0;
    };

    const choose = (index) => {
      const item = items[index];
      if (!item) return;
      onPick(item);
      close();
    };

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-expanded", "false");
    input.addEventListener("input", () => {
      if (onClear) onClear();
      clearTimeout(timer);
      const value = input.value.trim();
      if (value.length < 1) return close();
      // Debounced: a picker that fires per keystroke turns a fourteen
      // line bill into a few hundred requests.
      timer = setTimeout(async () => {
        try {
          items = await fetchItems(value);
          active = items.length ? 0 : -1;
          paint();
          input.setAttribute("aria-expanded", String(!listNode.hidden));
        } catch (exc) {
          banner(exc.message);
        }
      }, 140);
    });

    input.addEventListener("keydown", (event) => {
      if (listNode.hidden) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        active = (active + (event.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
        paint();
      } else if (event.key === "Enter") {
        // Only when something is highlighted, so Enter on a typo cannot
        // pick — or create — anything.
        if (active >= 0) {
          event.preventDefault();
          choose(active);
        }
      } else if (event.key === "Escape") {
        close();
      }
    });
    input.addEventListener("blur", () => setTimeout(close, 120));
  }

  // -------------------------------------------------- creating items

  /** A real form, not one click.
   *
   *  A product with a blank description is one nobody can identify in a
   *  stock list three weeks later, and a code without a label is not a
   *  product here at all -- three share `55X`. So both are required, and
   *  the brand offers the existing ones while still accepting a new
   *  name, because "existing code under a new label" is the case this
   *  was built for.
   */
  async function createItem(code) {
    const known = await api("/control/brands").catch(() => ({ items: [] }));
    const brand = window.prompt(
      `New item ${code}\n\nLabel (brand)?` +
        (known.items.length ? `\nExisting: ${known.items.join(", ")}` : ""),
      "",
    );
    if (!brand || !brand.trim()) return null;
    const description = window.prompt(`Description for ${code} · ${brand.trim()}?`, "");
    if (!description || description.trim().length < 2) {
      banner("An item needs a description — otherwise nobody can identify it later.");
      return null;
    }
    try {
      const made = await api("/control/items", {
        method: "POST",
        body: JSON.stringify({
          code,
          brand: brand.trim(),
          description: description.trim(),
        }),
      });
      banner(
        made.created
          ? `Created ${made.code} · ${made.brand}.`
          : `${made.code} already existed under ${made.brand} — using it.`,
        true,
      );
      return made;
    } catch (exc) {
      banner(exc.message);
      return null;
    }
  }

  // ------------------------------------------------------------- rows

  let rows = [];

  function blankRow() {
    return {
      product_id: null,
      code: "",
      brand: null,
      description: "",
      pieces: "",
      weight_kg: "",
      rate: "",
      note: "",
    };
  }

  /** Total KG = bales x kg per bale. Derived, never typed: a total and
   *  its parts that can disagree is a class of bug, not a feature. */
  function totalKg(row) {
    const pieces = Number(row.pieces || 0);
    const per = Number(row.weight_kg || 0);
    if (!pieces || !per) return "";
    return String(Math.round(pieces * per * 1000) / 1000);
  }

  /** Amount in paise, exactly. Quantity is not money, so it may be a
   *  Number; the rate and the product are handled as paise. */
  function amountPaise(row) {
    const kg = Number(totalKg(row) || 0);
    if (!kg || !row.rate) return 0n;
    const ratePaise = Money.toPaise(row.rate);
    return (BigInt(Math.round(kg * 1000)) * ratePaise) / 1000n;
  }

  function renderRows() {
    const body = $("rows");
    body.replaceChildren();

    rows.forEach((row, index) => {
      const tr = body.insertRow();
      tr.insertCell().outerHTML = `<td class="num derived">${index + 1}</td>`;

      const itemCell = tr.insertCell();
      const wrap = el("div", "field");
      const input = el("input");
      input.type = "text";
      input.placeholder = "Code or description…";
      input.value = row.code ? `${row.code} · ${row.brand || "—"}` : "";
      const list = el("div", "combo-list");
      list.hidden = true;
      wrap.append(input, list);
      itemCell.append(wrap);

      combo(input, list, {
        fetchItems: async (query) => {
          const found = await api(
            `/control/items?q=${encodeURIComponent(query)}&kind=${kind}`,
          );
          // Near-matches first, creation last and never highlighted, so
          // Enter on a typo cannot bring a product into existence.
          // Offered on a purchase only: inventing an item to sell that
          // was never bought is how stock goes negative on paper.
          if (kind === "purchase") {
            found.items.push({ __create: true, code: query.trim() });
          }
          return found.items;
        },
        render: (item) => {
          const node = el("div");
          if (item.__create) {
            node.append(el("span", "desc", `+ Create “${item.code}” as a new item…`));
            return node;
          }
          node.append(
            el("span", "code", item.code),
            el("span", "brand", item.brand || "—"),
            el("span", "desc", item.description || ""),
            el("span", "stock", `${item.on_hand} on hand`),
          );
          return node;
        },
        onClear: () => {
          row.product_id = null;
          row.note = "";
        },
        onPick: async (item) => {
          if (item.__create) {
            const made = await createItem(item.code);
            if (!made) {
              input.value = "";
              return;
            }
            item = made;
          }
          row.product_id = item.product_id;
          row.code = item.code;
          row.brand = item.brand;
          row.description = item.description;
          // The rate is a suggestion, not an answer — filled only when
          // the row is empty so it can never overwrite a typed price.
          if (!row.rate && item.last_rate) row.rate = item.last_rate;
          row.note = item.created
            ? "new item — check the code and label before saving"
            : kind === "sale" && Number(item.on_hand) <= 0
              ? "nothing on hand — this will go negative"
              : "";
          renderRows();
          focusCell(index, "pieces");
        },
      });

      const numeric = (key, placeholder) => {
        const cell = tr.insertCell();
        cell.className = "num";
        const field = el("input");
        field.type = "text";
        field.inputMode = "decimal";
        field.value = row[key];
        field.placeholder = placeholder;
        field.dataset.row = String(index);
        field.dataset.key = key;
        field.addEventListener("input", () => {
          row[key] = field.value;
          repaintDerived();
        });
        field.addEventListener("keydown", (event) => {
          if (event.key === "Enter" && index === rows.length - 1) {
            event.preventDefault();
            addRow();
          }
        });
        cell.append(field);
      };

      numeric("pieces", "bales");
      numeric("weight_kg", "kg");

      const kgCell = tr.insertCell();
      kgCell.className = "num derived";
      kgCell.dataset.derivedKg = String(index);
      kgCell.textContent = totalKg(row) || "—";

      numeric("rate", "0.00");

      const amountCell = tr.insertCell();
      amountCell.className = "num derived";
      amountCell.dataset.derivedAmount = String(index);
      amountCell.textContent = Money.format(Money.fromPaise(amountPaise(row)));

      const dropCell = tr.insertCell();
      const drop = el("button", "drop", "×");
      drop.type = "button";
      drop.title = "Remove this line";
      drop.addEventListener("click", () => {
        rows.splice(index, 1);
        if (!rows.length) rows.push(blankRow());
        renderRows();
      });
      dropCell.append(drop);

      if (row.note) {
        const noteRow = body.insertRow();
        const cell = noteRow.insertCell();
        cell.colSpan = 8;
        cell.className = "line-note";
        cell.textContent = `⚠ ${row.note}`;
      }
    });

    repaintDerived();
  }

  /** Recompute without rebuilding: rebuilding steals focus mid-typing. */
  function repaintDerived() {
    rows.forEach((row, index) => {
      const kgCell = document.querySelector(`[data-derived-kg="${index}"]`);
      const amountCell = document.querySelector(`[data-derived-amount="${index}"]`);
      if (kgCell) kgCell.textContent = totalKg(row) || "—";
      if (amountCell) {
        amountCell.textContent = Money.format(Money.fromPaise(amountPaise(row)));
      }
    });
    renderTotals();
    saveDraft();
  }

  function addRow() {
    rows.push(blankRow());
    renderRows();
    const inputs = $("rows").querySelectorAll("input[type=text]");
    const last = inputs[inputs.length - 5];
    if (last) last.focus();
  }

  function focusCell(index, key) {
    const field = document.querySelector(`input[data-row="${index}"][data-key="${key}"]`);
    if (field) field.focus();
  }

  // ---------------------------------------------------------- charges

  let charges = [];

  function renderCharges() {
    const host = $("charge-rows");
    host.replaceChildren();
    charges.forEach((charge, index) => {
      const row = el("div", "charge-row");
      const label = el("input");
      label.placeholder = "GST, packing…";
      label.value = charge.label;
      label.addEventListener("input", () => {
        charge.label = label.value;
        saveDraft();
      });
      const amount = el("input");
      amount.className = "num";
      amount.inputMode = "decimal";
      amount.placeholder = "0";
      amount.value = charge.amount;
      amount.addEventListener("input", () => {
        charge.amount = amount.value;
        renderTotals();
        saveDraft();
      });
      const drop = el("button", "drop", "×");
      drop.type = "button";
      drop.addEventListener("click", () => {
        charges.splice(index, 1);
        renderCharges();
        renderTotals();
      });
      row.append(label, amount, drop);
      host.append(row);
    });
  }

  // ----------------------------------------------------------- totals

  function renderTotals() {
    const subtotal = rows.reduce((total, row) => total + amountPaise(row), 0n);
    const chargeTotal = charges.reduce(
      (total, charge) => total + (charge.amount ? Money.toPaise(charge.amount) : 0n),
      0n,
    );
    const freight = $("freight").value ? Money.toPaise($("freight").value) : 0n;
    const discount = $("discount").value ? Money.toPaise($("discount").value) : 0n;
    const grand = subtotal + chargeTotal + freight - discount;
    const paid = kind === "sale" && $("paid-now").value ? Money.toPaise($("paid-now").value) : 0n;

    const line = (label, value, className) => {
      const node = el("div", className);
      node.append(el("span", null, label), el("b", null, Money.format(Money.fromPaise(value))));
      return node;
    };

    const host = $("totals");
    host.replaceChildren(line("Subtotal", subtotal));
    charges.forEach((charge) => {
      if (charge.amount) {
        host.append(line(charge.label || "Charge", Money.toPaise(charge.amount)));
      }
    });
    if (freight) host.append(line("Freight", freight));
    if (discount) host.append(line("Discount", -discount));
    host.append(line("Total", grand, "grand"));
    if (paid) {
      host.append(line("Paid now", paid));
      host.append(line("Balance", grand - paid, "balance"));
    }
  }

  // ----------------------------------------------------------- drafts

  /** A half-entered bill survives a closed laptop. Only what was typed
   *  — never a server figure, so a stale draft cannot show a stale
   *  balance — and never the paid amount, which is the one field where
   *  being quietly wrong costs money. */
  function saveDraft() {
    if (!draftKey) return;
    const payload = {
      at: Date.now(),
      kind,
      party: $("party").value,
      invoice: $("invoice").value,
      date: $("entry-date").value,
      rows,
      charges,
      freight: $("freight").value,
      discount: $("discount").value,
    };
    try {
      localStorage.setItem(DRAFT_STORE, JSON.stringify(payload));
    } catch {
      /* storage full or blocked; a draft is a convenience, not a record */
    }
  }

  function readDraft() {
    try {
      const raw = localStorage.getItem(DRAFT_STORE);
      if (!raw) return null;
      const draft = JSON.parse(raw);
      const age = (Date.now() - draft.at) / 86400000;
      if (age > DRAFT_TTL_DAYS) {
        localStorage.removeItem(DRAFT_STORE);
        return null;
      }
      return draft;
    } catch {
      return null;
    }
  }

  function clearDraft() {
    localStorage.removeItem(DRAFT_STORE);
    $("draft-bar").hidden = true;
  }

  function offerDraft() {
    const draft = readDraft();
    if (!draft || !draft.rows?.some((row) => row.product_id)) return;
    const when = new Date(draft.at).toLocaleString();
    $("draft-label").textContent =
      `Unsaved ${draft.kind} from ${when} — ${draft.rows.filter((r) => r.product_id).length} line(s).`;
    $("draft-bar").hidden = false;
    $("draft-restore").onclick = () => {
      kind = draft.kind;
      setKind(kind);
      $("party").value = draft.party || "";
      $("invoice").value = draft.invoice || "";
      $("entry-date").value = draft.date || "";
      rows = draft.rows.length ? draft.rows : [blankRow()];
      charges = draft.charges || [];
      $("freight").value = draft.freight || "";
      $("discount").value = draft.discount || "";
      renderRows();
      renderCharges();
      renderTotals();
      $("draft-bar").hidden = true;
    };
    $("draft-discard").onclick = clearDraft;
  }

  // ------------------------------------------------------------- mode

  const PAGES = {
    records: "page-records",
    money: "page-money",
    stock: "page-stock",
    whatsapp: "page-whatsapp",
    system: "page-system",
  };

  // What each page has to fetch before it means anything. Kept as data
  // so adding a page is one line here rather than another branch below.
  const ON_OPEN = {
    records: () => loadRecords(),
    stock: () => loadProducts(),
    whatsapp: () => loadWhatsApp(),
    system: () => loadSystem(),
  };

  function setKind(next) {
    if (PAGES[next]) {
      document.querySelectorAll("#nav button").forEach((button) => {
        button.classList.toggle("active", button.dataset.page === next);
        if (button.classList.contains("active")) {
          button.scrollIntoView({ inline: "center", block: "nearest" });
        }
      });
      $("entry").hidden = true;
      Object.values(PAGES).forEach((id) => ($(id).hidden = id !== PAGES[next]));
      if (ON_OPEN[next]) ON_OPEN[next]().catch((exc) => banner(exc.message));
      return;
    }
    $("entry").hidden = false;
    Object.values(PAGES).forEach((id) => ($(id).hidden = true));
    kind = next;
    document.querySelectorAll("#nav button").forEach((button) => {
      button.classList.toggle("active", button.dataset.page === kind);
      if (button.classList.contains("active")) {
        button.scrollIntoView({ inline: "center", block: "nearest" });
      }
    });
    const sale = kind === "sale";
    $("party-label").textContent = sale ? "Customer" : "Supplier";
    $("invoice-field").hidden = sale;
    $("payment-field").hidden = !sale;
    $("paid-block").hidden = !sale;
    $("date-label").textContent = sale ? "Sale date" : "Invoice date";
    $("save").textContent = sale ? "Save sale" : "Save bill";
  }

  function resetForm() {
    rows = [blankRow()];
    charges = [];
    ["party", "invoice", "freight", "discount", "paid-now"].forEach((id) => ($(id).value = ""));
    $("party-note").textContent = "";
    $("invoice-note").textContent = "";
    $("entry-date").value = new Date().toISOString().slice(0, 10);
    draftKey = crypto.randomUUID();
    renderRows();
    renderCharges();
    renderTotals();
  }

  // ------------------------------------------------------------- save

  async function save(event) {
    event.preventDefault();
    const chosen = rows.filter((row) => row.product_id);
    if (!chosen.length) return banner("Add at least one line.");
    if (!$("party").value.trim()) return banner("Pick a party first.");

    const payload = {
      lines: chosen.map((row) => ({
        code: row.code,
        brand: row.brand,
        qty: totalKg(row),
        rate: row.rate || "0",
        pieces: row.pieces || null,
        weight_kg: row.weight_kg || null,
        description: row.description || null,
      })),
      charges: Object.fromEntries(
        charges.filter((c) => c.label && c.amount).map((c) => [c.label, c.amount]),
      ),
      freight: $("freight").value || "0",
      discount: $("discount").value || "0",
    };

    $("save").disabled = true;
    try {
      let result;
      if (kind === "purchase") {
        result = await api("/control/purchases", {
          method: "POST",
          body: JSON.stringify({
            ...payload,
            supplier: $("party").value.trim(),
            invoice_no: $("invoice").value.trim(),
            invoice_date: $("entry-date").value,
          }),
        });
        banner(
          result.already_existed
            ? `That bill was already recorded — showing the one that exists (${result.grand_total}).`
            : `Saved: ${result.invoice_no}, ${result.grand_total}.`,
          true,
        );
      } else {
        result = await api("/control/sales", {
          method: "POST",
          body: JSON.stringify({
            ...payload,
            customer: $("party").value.trim(),
            payment_type: $("payment-type").value,
            paid_now: $("paid-now").value || "0",
            paid_via: $("paid-via").value,
            sale_date: $("entry-date").value,
            // Minted with the form. A slow connection and an impatient
            // thumb is the case a terminal never has.
            idempotency_key: draftKey,
          }),
        });
        banner(
          result.already_existed
            ? "That sale was already recorded."
            : `Saved: ${result.grand_total}, ${result.customer} now owes ${result.outstanding_after}.`,
          true,
        );
      }
      clearDraft();
      resetForm();
    } catch (exc) {
      banner(exc.message);
    } finally {
      $("save").disabled = false;
    }
  }

  // ---------------------------------------------------------- records

  async function loadRecords() {
    await Promise.all([loadPurchaseRecords(), loadSaleRecords()]);
  }

  async function loadSaleRecords() {
    const data = await api("/control/sales/recent");
    $("recent-sales").replaceChildren(
      rowsTable(["Ref", "Date", "Customer", "Total", "Paid", ""], data.items, (item) => {
        const remove = el("button", "link", "Remove");
        remove.type = "button";
        remove.addEventListener("click", () => previewPurge(item.reference, "sale"));
        return [
          item.reference,
          item.date,
          item.customer,
          Money.format(item.grand_total),
          Money.format(item.amount_paid),
          remove,
        ];
      }),
    );
  }

  async function loadPurchaseRecords() {
    const data = await api("/control/purchases/recent");
    const host = $("recent");
    host.replaceChildren();
    if (!data.items.length) {
      host.append(el("p", "muted", "No purchases yet."));
      return;
    }
    const table = el("table", "grid");
    const head = table.createTHead().insertRow();
    ["Invoice", "Date", "Supplier", "Total", "Paid", ""].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      head.append(th);
    });
    const body = table.createTBody();
    data.items.forEach((item) => {
      const tr = body.insertRow();
      tr.insertCell().textContent = item.invoice_no;
      tr.insertCell().textContent = item.date;
      tr.insertCell().textContent = item.supplier;
      const total = tr.insertCell();
      total.className = "num";
      total.textContent = Money.format(item.grand_total);
      const paid = tr.insertCell();
      paid.className = "num";
      paid.textContent = Money.format(item.amount_paid);
      const action = tr.insertCell();
      const remove = el("button", "link", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => previewPurge(item.invoice_no));
      action.append(remove);
    });
    host.replaceChildren(scroller(table));
  }

  /** Preview is mandatory and is a real dry run: the removal genuinely
   *  happens inside a transaction that is thrown away, so the figures
   *  shown are the ones the commit would produce. */
  async function previewPurge(reference, kind = "purchase") {
    const panel = $("purge-preview");
    panel.hidden = false;
    panel.replaceChildren(el("p", "muted", "Working out what this would do…"));
    try {
      const plan = await api("/control/purge/preview", {
        method: "POST",
        body: JSON.stringify({ kind, reference }),
      });
      panel.replaceChildren();
      panel.append(el("h3", null, `Remove ${plan.reference}?`));
      const facts = el("ul");
      facts.append(el("li", null, `${plan.lines} line(s), ${Money.format(plan.grand_total)}`));
      if (plan.carries_stock) {
        facts.append(el("li", null, "its stock will be reversed"));
      }
      (plan.notes || []).forEach((note) => facts.append(el("li", null, note)));
      panel.append(facts);

      if (!plan.ok) {
        const why = el("p", "error", "Cannot remove this:");
        panel.append(why);
        const list = el("ul");
        (plan.blockers || []).forEach((b) => list.append(el("li", null, b)));
        panel.append(list);
        return;
      }

      panel.append(el("p", "muted", "This can be undone afterwards."));
      const label = el("label", null, `Type ${plan.reference} to confirm`);
      const input = el("input");
      const go = el("button", "primary", "Remove");
      go.type = "button";
      go.addEventListener("click", async () => {
        go.disabled = true;
        try {
          const done = await api("/control/purge", {
            method: "POST",
            body: JSON.stringify({ kind, reference, confirm: input.value.trim() }),
          });
          banner(
            `${done.reference} removed. Undo with the reversal id ${done.reversal}.`,
            true,
          );
          panel.hidden = true;
          await loadRecords();
        } catch (exc) {
          banner(exc.message);
        } finally {
          go.disabled = false;
        }
      });
      const cancel = el("button", "link", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => (panel.hidden = true));
      panel.append(label, input, go, cancel);
      input.focus();
    } catch (exc) {
      panel.replaceChildren(el("p", "error", exc.message));
    }
  }

  // ------------------------------------------------------------ money

  /** Every action here reports what the server said, not what was
   *  typed. They differ whenever an allocation lands somewhere other
   *  than expected, and the server's answer is the true one. */
  function wire(buttonId, outId, run) {
    $(buttonId).addEventListener("click", async () => {
      const out = $(outId);
      $(buttonId).disabled = true;
      out.textContent = "Working…";
      out.classList.remove("warn");
      try {
        out.textContent = await run();
      } catch (exc) {
        out.textContent = exc.message;
        out.classList.add("warn");
      } finally {
        $(buttonId).disabled = false;
      }
    });
  }

  wire("rc-go", "rc-out", async () => {
    const result = await api("/control/receive", {
      method: "POST",
      body: JSON.stringify({
        party: $("rc-party").value.trim(),
        amount: $("rc-amount").value.trim(),
        via: $("rc-via").value,
        against: $("rc-against").value.trim() || null,
      }),
    });
    const applied = result.allocations
      .map((a) => `${a.reference} ${Money.format(a.applied)}`)
      .join(", ");
    ["rc-party", "rc-amount", "rc-against"].forEach((id) => ($(id).value = ""));
    return (
      `Received ${Money.format(result.amount)} from ${result.party}. ` +
      (applied ? `Settled ${applied}. ` : "") +
      (Money.isZero(result.advance) ? "" : `${Money.format(result.advance)} on account. `) +
      `Now owes ${Money.format(result.outstanding_after)}.`
    );
  });

  wire("pay-go", "pay-out", async () => {
    const result = await api("/control/pay", {
      method: "POST",
      body: JSON.stringify({
        party: $("pay-party").value.trim(),
        amount: $("pay-amount").value.trim(),
        via: $("pay-via").value,
      }),
    });
    ["pay-party", "pay-amount"].forEach((id) => ($(id).value = ""));
    return `Paid ${Money.format(result.amount)} to ${result.party}. Owing ${Money.format(result.outstanding_after)}.`;
  });

  wire("ex-go", "ex-out", async () => {
    const result = await api("/control/expenses", {
      method: "POST",
      body: JSON.stringify({
        category: $("ex-cat").value.trim(),
        amount: $("ex-amount").value.trim(),
        description: $("ex-note").value.trim() || null,
      }),
    });
    ["ex-cat", "ex-amount", "ex-note"].forEach((id) => ($(id).value = ""));
    // The service spots a category that looks like one already in use.
    // Two buckets for one thing splits the reporting silently, so the
    // warning is worth more than the tidiness of hiding it.
    const similar = result.similar_category
      ? ` — note: you already use “${result.similar_category}”.`
      : "";
    return `Recorded ${Money.format(result.amount)} on ${result.category}.${similar}`;
  });

  // -------------------------------------------------- fixes and stock

  wire("fx-go", "fx-out", async () => {
    const result = await api("/control/purchases/fix-line", {
      method: "POST",
      body: JSON.stringify({
        invoice_no: $("fx-invoice").value.trim(),
        line_no: Number($("fx-line").value || 0),
        code: $("fx-code").value.trim() || null,
        brand: $("fx-brand").value.trim() || null,
        rate: $("fx-rate").value.trim() || null,
      }),
    });
    return `${result.invoice_no} line ${result.line_no}: ${result.notes.join("; ")}`;
  });

  wire("sa-go", "sa-out", async () => {
    const result = await api("/control/stock/adjust", {
      method: "POST",
      body: JSON.stringify({
        code: $("sa-code").value.trim(),
        brand: $("sa-brand").value.trim() || null,
        qty_delta: $("sa-qty").value.trim(),
        reason: $("sa-reason").value,
        note: $("sa-note").value.trim() || null,
      }),
    });
    return `${result.code}: ${result.on_hand} on hand at ${Money.format(result.avg_cost)}.`;
  });

  wire("rc-all", "rc-all-out", async () => {
    const result = await api("/control/stock/recost", { method: "POST" });
    if (!result.changed.length) return "Nothing moved — the books already agreed with history.";
    return result.changed
      .map((c) => `${c.code}: ${c.avg_before} → ${c.avg_after}`)
      .join("; ");
  });

  // ----------------------------------------------------------- system

  /** A table in its own horizontal scroller.
   *
   * Cells are `white-space: nowrap`, so a six-column table is wider than
   * a phone. Without the wrapper that width is the *page's*, and the
   * whole document scrolls sideways -- the one thing ui-plan.md §9 says
   * must never happen. Every caller only appends what this returns, so
   * wrapping here covers all of them. */
  function scroller(table) {
    const box = el("div", "table-scroll");
    box.append(table);
    return box;
  }

  function rowsTable(headers, items, cells) {
    const table = el("table", "grid");
    const head = table.createTHead().insertRow();
    headers.forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      head.append(th);
    });
    const body = table.createTBody();
    if (!items.length) {
      const cell = body.insertRow().insertCell();
      cell.colSpan = headers.length;
      cell.className = "muted";
      cell.textContent = "Nothing here.";
      return scroller(table);
    }
    items.forEach((item) => {
      const tr = body.insertRow();
      cells(item).forEach((value) => {
        const cell = tr.insertCell();
        if (value instanceof Node) cell.append(value);
        else cell.textContent = value;
      });
    });
    return scroller(table);
  }

  async function loadSystem() {
    const [reversals, audit, backups, report] = await Promise.all([
      api("/control/reversals"),
      api("/control/audit?limit=40"),
      api("/control/backups"),
      api("/control/diagnostics"),
    ]);
    renderDiagnostics(report);

    $("reversals").replaceChildren(
      rowsTable(["When", "What", "Subject", ""], reversals.items, (item) => {
        const undo = el("button", "link", "Undo");
        undo.type = "button";
        undo.addEventListener("click", () => previewReversal(item.id));
        return [item.when.slice(0, 16).replace("T", " "), item.operation, item.subject, undo];
      }),
    );

    $("audit").replaceChildren(
      rowsTable(["When", "What", "How", "Who"], audit.items, (item) => [
        item.when.slice(0, 16).replace("T", " "),
        item.action,
        item.channel,
        item.who,
      ]),
    );

    $("backups").replaceChildren(
      rowsTable(["Name", "Taken", "Size"], backups.items, (item) => [
        item.name,
        item.taken.slice(0, 16).replace("T", " "),
        `${item.size_kb} KB`,
      ]),
    );
  }

  /** Row by row, before anything moves. A reversal that puts some rows
   *  back and leaves the rest is worse than one that refuses. */
  async function previewReversal(reference) {
    const plan = await api(`/control/reversals/${reference}/preview`, { method: "POST" });
    const lines = plan.rows.map((r) => `${r.state}${r.detail ? ` — ${r.detail}` : ""}`);
    if (!plan.ok) {
      banner(`Blocked: ${plan.blocked.join("; ")}`);
      return;
    }
    if (!window.confirm(`Undo “${plan.subject}”?\n\n${lines.join("\n")}`)) return;
    try {
      const done = await api(`/control/reversals/${reference}`, { method: "POST" });
      banner(`${done.subject}: ${done.moved} row(s) put back.`, true);
      await loadSystem();
    } catch (exc) {
      banner(exc.message);
    }
  }

  wire("sf-go", "sf-out", async () => {
    const result = await api("/control/sales/fix", {
      method: "POST",
      body: JSON.stringify({
        reference: $("sf-ref").value.trim(),
        customer: $("sf-customer").value.trim() || null,
        line_no: $("sf-line").value ? Number($("sf-line").value) : null,
        code: $("sf-code").value.trim() || null,
        brand: $("sf-brand").value.trim() || null,
      }),
    });
    await loadRecords();
    return `${result.sale_id}: ${result.notes.join("; ")}`;
  });

  wire("bk-go", "backups", async () => {
    const made = await api("/control/backups", { method: "POST" });
    await loadSystem();
    return `Took ${made.name} (${made.size_kb} KB).`;
  });

  // --------------------------------------------------------- catalogue

  /** The catalogue, and the two counts that make it useful. A list of
   *  codes cannot tell a duplicate from a real product; “bought 4, sold
   *  1” beside “bought 0, sold 0” on the same code can. */
  async function loadProducts() {
    const query = $("pr-q").value.trim();
    const data = await api(`/control/products?q=${encodeURIComponent(query)}`);
    $("products").replaceChildren(
      rowsTable(
        ["Code", "Label", "Description", "On hand", "Avg cost", "Bought", "Sold", ""],
        data.items,
        (item) => {
          // Offered only where it is possible. A button that explains
          // why it cannot work, after it is pressed, is worse than one
          // that is not there.
          let action = "";
          if (item.deletable) {
            const drop = el("button", "link", "Delete");
            drop.type = "button";
            drop.addEventListener("click", () => deleteProduct(item));
            action = drop;
          }
          return [
            item.code,
            item.brand || "—",
            item.description,
            item.on_hand,
            Money.format(item.avg_cost),
            String(item.purchases),
            String(item.sales),
            action,
          ];
        },
      ),
    );
  }

  async function deleteProduct(item) {
    if (!window.confirm(`Delete ${item.label}? Nothing has ever been bought or sold on it.`)) {
      return;
    }
    try {
      const done = await api("/control/products/delete", {
        method: "POST",
        body: JSON.stringify({ code: item.code, brand: item.brand || null }),
      });
      banner(`${done.label} removed. Undo with the reversal id ${done.reversal}.`, true);
      await loadProducts();
    } catch (exc) {
      banner(exc.message);
    }
  }

  $("pr-q").addEventListener("input", () => {
    clearTimeout($("pr-q").dataset.timer);
    $("pr-q").dataset.timer = setTimeout(
      () => loadProducts().catch((exc) => banner(exc.message)),
      250,
    );
  });

  /** Preview, then confirmation typed back. A merge folds two histories
   *  into one and moves the surviving product's cost; that is not a
   *  thing to find out about after clicking. */
  wire("pm-go", "pm-out", async () => {
    const body = {
      loser_code: $("pm-loser").value.trim(),
      loser_brand: $("pm-loser-brand").value.trim() || null,
      winner_code: $("pm-winner").value.trim(),
      winner_brand: $("pm-winner-brand").value.trim() || null,
    };
    const plan = await api("/control/products/merge/preview", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const panel = $("pm-preview");
    panel.hidden = false;
    panel.replaceChildren();

    if (!plan.ok) {
      const list = el("ul");
      (plan.blockers || []).forEach((b) => list.append(el("li", null, b)));
      panel.append(el("p", "error", "Cannot merge these:"), list);
      return `${plan.loser} → ${plan.winner}: blocked.`;
    }

    const facts = el("ul");
    facts.append(
      el("li", null, `${plan.movements} stock movement(s) move to ${plan.winner}`),
      el("li", null, `${plan.purchase_lines} purchase line(s), ${plan.sales_lines} sale line(s)`),
      el("li", null, `${plan.loser_qty} + ${plan.winner_qty} = ${plan.qty_after} on hand`),
    );
    (plan.notes || []).forEach((note) => facts.append(el("li", null, note)));
    panel.append(facts);

    const label = el("label", null, `Type ${plan.winner} to confirm`);
    const input = el("input");
    const go = el("button", "primary", "Merge them");
    go.type = "button";
    go.addEventListener("click", async () => {
      go.disabled = true;
      try {
        const done = await api("/control/products/merge", {
          method: "POST",
          body: JSON.stringify({ ...body, confirm: input.value.trim() }),
        });
        banner(`Merged into ${done.winner}. Undo with the reversal id ${done.reversal}.`, true);
        panel.hidden = true;
        await loadProducts();
      } catch (exc) {
        banner(exc.message);
      } finally {
        go.disabled = false;
      }
    });
    panel.append(label, input, go);
    input.focus();
    return `${plan.loser} → ${plan.winner}: ${plan.qty_after} on hand afterwards.`;
  });

  // ---------------------------------------------------------- whatsapp

  async function loadWhatsApp() {
    const [health, contacts, messages] = await Promise.all([
      api("/control/messages/health"),
      api("/control/contacts"),
      api(`/control/messages?limit=60&failed_only=${$("msg-failed").checked}`),
    ]);

    // Grouped by cause, because seventeen failures with one cause are
    // one problem and a list of seventeen rows hides that.
    const host = $("msg-health");
    host.replaceChildren();
    if (!health.failed) {
      host.append(
        el(
          "p",
          "muted",
          `${health.messages} message(s) in the last ${health.window_hours} hours. All delivered.`,
        ),
      );
    } else {
      host.append(
        el("p", null, `${health.failed} of ${health.messages} did not arrive:`),
      );
      health.causes.forEach((cause) => {
        const box = el("div", "msg-cause");
        box.append(el("b", null, `${cause.count}× ${cause.code}`));
        box.append(el("p", null, cause.meaning || cause.detail || "no reason given"));
        host.append(box);
      });
    }

    $("contacts").replaceChildren(
      rowsTable(
        ["Name", "Role", "WhatsApp", "Email", "Last message"],
        contacts.items,
        (item) => {
          const number = el("span", item.whatsapp_number ? "" : "msg-fail");
          number.textContent = item.whatsapp_number || "none — cannot be reached";
          return [
            item.name,
            item.role,
            number,
            item.email || "—",
            item.last_seen ? item.last_seen.slice(0, 16).replace("T", " ") : "never",
          ];
        },
      ),
    );

    $("messages").replaceChildren(
      rowsTable(["When", "", "Who", "Result", "Message"], messages.items, (item) => {
        const result = el("span", item.ok ? "" : "msg-fail");
        result.textContent = item.ok ? "ok" : item.error_code || "failed";
        if (!item.ok && item.meaning) result.title = item.meaning;
        return [
          item.when.slice(5, 16).replace("T", " "),
          item.direction === "out" ? "→" : "←",
          item.peer,
          result,
          item.preview.slice(0, 60),
        ];
      }),
    );
  }

  $("msg-failed").addEventListener("change", () =>
    loadWhatsApp().catch((exc) => banner(exc.message)),
  );

  wire("ct-go", "ct-out", async () => {
    const result = await api("/control/contacts/relink", {
      method: "POST",
      body: JSON.stringify({
        number: $("ct-number").value.trim(),
        user: $("ct-user").value.trim(),
      }),
    });
    ["ct-number", "ct-user"].forEach((id) => ($(id).value = ""));
    await loadWhatsApp();
    return `${result.number} now reaches ${result.user}. ${result.notes.join("; ")}`;
  });

  // ------------------------------------------------------ diagnostics

  function renderDiagnostics(report) {
    $("diagnostics").replaceChildren(
      rowsTable(
        ["", "Rows"],
        report.counts,
        (row) => [row.label, String(row.count)],
      ),
      el(
        "p",
        "muted",
        `${report.database_mb} MB of data, ${report.disk_free_gb} GB free on disk, ` +
          `${report.backups} backup(s).`,
      ),
    );
    report.nightly.forEach((run) => {
      const line = el(
        "p",
        run.stale ? "msg-fail" : "muted",
        `${run.kind} reconciliation last ran ${run.last_run.slice(0, 16).replace("T", " ")}`,
      );
      $("diagnostics").append(line);
    });
    if (!report.nightly.length) {
      $("diagnostics").append(
        el("p", "msg-fail", "No reconciliation has ever run — the nightly job is not scheduled."),
      );
    }

    const drift = $("ledger-drift");
    drift.replaceChildren();
    if (!report.ledger_drift.length) {
      drift.append(el("p", "muted", "Every running balance agrees with the rows behind it."));
      return;
    }
    report.ledger_drift.forEach((row) => {
      drift.append(
        el(
          "p",
          "msg-fail",
          `${row.ledger} running balance says ${Money.format(row.says)}, ` +
            `should be ${Money.format(row.should_be)}`,
        ),
      );
    });
  }

  wire("lg-go", "lg-out", async () => {
    const result = await api("/control/ledger/rebuild", { method: "POST" });
    await loadSystem();
    return result.corrected
      ? `${result.corrected} running balance(s) corrected. ${result.notes.join("; ")}`
      : "Nothing moved — every running balance already agreed.";
  });

  // ------------------------------------------------------------- boot

  async function afterSignIn(name) {
    $("signin").hidden = true;
    $("app").hidden = false;
    $("whoami").textContent = name;
    resetForm();
    offerDraft();
    try {
      const health = await api("/control/health");
      $("books-health").textContent = health.ok
        ? "books balance"
        : "⚠ books do not balance";
      $("books-health").style.color = health.ok ? "" : "var(--danger)";
    } catch {
      /* the header is a courtesy; a failed check must not block entry */
    }
  }

  $("signin-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("signin-error").hidden = true;
    try {
      const result = await api("/auth/control/login", {
        method: "POST",
        body: JSON.stringify({
          email: $("email").value.trim(),
          password: $("password").value,
        }),
      });
      token = result.token;
      $("password").value = "";
      await afterSignIn(result.full_name);
    } catch (exc) {
      $("signin-error").textContent = exc.message;
      $("signin-error").hidden = false;
    }
  });

  $("signout").addEventListener("click", () => {
    token = null;
    $("app").hidden = true;
    $("signin").hidden = false;
  });

  document.querySelectorAll("#nav button").forEach((button) => {
    button.addEventListener("click", () => {
      setKind(button.dataset.page);
      resetForm();
    });
  });

  $("add-charge").addEventListener("click", () => {
    charges.push({ label: "", amount: "" });
    renderCharges();
  });
  ["freight", "discount", "paid-now"].forEach((id) => {
    $(id).addEventListener("input", () => {
      renderTotals();
      saveDraft();
    });
  });
  $("discard").addEventListener("click", () => {
    clearDraft();
    resetForm();
  });
  $("entry").addEventListener("submit", save);

  combo($("party"), $("party-list"), {
    fetchItems: async (query) => {
      const found = await api(
        `/control/parties?q=${encodeURIComponent(query)}&kind=${
          kind === "sale" ? "customer" : "supplier"
        }`,
      );
      // A new party is the ordinary case for a growing business. Being
      // forced to pick from a list means either abandoning a half-typed
      // bill or taking the nearest existing name -- which is exactly how
      // three sales ended up under the wrong customer. Offered last and
      // never highlighted, so Enter on a near-match cannot create one.
      found.items.push({ __create: true, name: query.trim() });
      return found.items;
    },
    render: (item) => {
      const node = el("div");
      if (item.__create) {
        node.append(el("span", "desc", `+ Add “${item.name}” as a new party…`));
        return node;
      }
      node.append(
        el("span", "code", item.name),
        el("span", "stock", `${Money.format(item.outstanding)} open`),
      );
      return node;
    },
    onClear: () => ($("party-note").textContent = ""),
    onPick: async (item) => {
      if (item.__create) {
        const label = kind === "sale" ? "customer" : "supplier";
        if (!window.confirm(`Add “${item.name}” as a new ${label}?`)) {
          $("party").value = "";
          return;
        }
        try {
          const made = await api("/control/parties", {
            method: "POST",
            body: JSON.stringify({ kind: label, name: item.name }),
          });
          $("party").value = made.name;
          $("party-note").textContent = made.created
            ? `new ${label} added`
            : `${made.name} already existed — using it`;
          return;
        } catch (exc) {
          banner(exc.message);
          return;
        }
      }
      $("party").value = item.name;
      $("party-note").textContent = `${Money.format(item.outstanding)} outstanding`;
    },
  });

  document.addEventListener("keydown", (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!$("app").hidden) $("entry").requestSubmit();
    }
  });

  setKind("purchase");
})();
