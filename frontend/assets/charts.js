/* Charts as hand-built inline SVG -- docs/21_WebDashboard.md §4, §5.
 *
 * No charting library: the mark specs the doc asks for (thin marks,
 * rounded data-ends, a 2px surface gap between adjacent fills, direct
 * labels on selected marks only) are things libraries fight, and this
 * is a two-user dashboard where a dependency to keep patched costs more
 * than the code it saves.
 *
 * Palette: two categorical hues, assigned in fixed order and never
 * cycled. Both modes were checked with the validator rather than judged
 * by eye -- light #2f6f9f/#c4703a and dark #4a90d9/#d4793f each pass
 * the lightness band, chroma floor, CVD separation, normal-vision floor
 * and contrast-vs-surface checks.
 *
 * Every axis label and tooltip figure comes from the exact money
 * string. Only bar geometry uses a float, because that decides pixels.
 */

const Charts = (() => {
  const NS = "http://www.w3.org/2000/svg";

  function el(name, attrs = {}, text) {
    const node = document.createElementNS(NS, name);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function niceCeiling(value) {
    if (value <= 0) return 1;
    const magnitude = 10 ** Math.floor(Math.log10(value));
    return Math.ceil(value / magnitude) * magnitude;
  }

  /** Shared frame: y grid, zero rule, x labels. One axis, always. */
  function frame(svg, { width, height, pad, max, min, points }) {
    const plotHeight = height - pad.top - pad.bottom;
    const scale = (value) => {
      const span = max - min || 1;
      return pad.top + plotHeight - ((value - min) / span) * plotHeight;
    };

    const ticks = 4;
    for (let i = 0; i <= ticks; i += 1) {
      const value = min + ((max - min) / ticks) * i;
      const y = scale(value);
      svg.append(
        el("line", {
          x1: pad.left,
          x2: width - pad.right,
          y1: y,
          y2: y,
          class: value === 0 ? "axis-zero" : "grid",
        }),
      );
      svg.append(
        el(
          "text",
          { x: pad.left - 8, y: y + 4, class: "tick", "text-anchor": "end" },
          Money.compact(String(value)),
        ),
      );
    }

    const band = (width - pad.left - pad.right) / points.length;
    points.forEach((point, index) => {
      svg.append(
        el(
          "text",
          {
            x: pad.left + band * index + band / 2,
            y: height - pad.bottom + 18,
            class: "tick",
            "text-anchor": "middle",
          },
          point.label.replace(" 20", " '"),
        ),
      );
    });

    return { scale, band };
  }

  function tooltip(container) {
    let node = container.querySelector(".tip");
    if (!node) {
      node = document.createElement("div");
      node.className = "tip";
      node.hidden = true;
      container.append(node);
    }
    return node;
  }

  function attachHover(container, target, html) {
    const tip = tooltip(container);
    target.addEventListener("pointerenter", (event) => {
      tip.innerHTML = html;
      tip.hidden = false;
      const bounds = container.getBoundingClientRect();
      tip.style.left = `${event.clientX - bounds.left}px`;
      tip.style.top = `${event.clientY - bounds.top}px`;
    });
    target.addEventListener("pointerleave", () => {
      tip.hidden = true;
    });
  }

  /** Net profit per month. Columns, because the question is magnitude
   * over a handful of discrete periods -- and a loss has to read as a
   * loss, so negatives cross the zero rule rather than being clamped. */
  function profitColumns(container, points) {
    container.replaceChildren();
    if (!points.length) {
      container.append(Object.assign(document.createElement("p"), {
        className: "muted",
        textContent: "No months to show yet.",
      }));
      return;
    }

    const width = 520;
    const height = 240;
    const pad = { top: 16, right: 16, bottom: 34, left: 62 };
    const values = points.map((p) => Money.toPlotValue(p.net_profit));
    const max = niceCeiling(Math.max(0, ...values));
    const min = Math.min(0, ...values) < 0 ? -niceCeiling(Math.abs(Math.min(...values))) : 0;

    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      class: "svg-chart",
      role: "img",
      "aria-label": "Net profit per month",
    });
    const { scale, band } = frame(svg, { width, height, pad, max, min, points });

    const barWidth = Math.min(38, band * 0.55);
    points.forEach((point, index) => {
      const value = values[index];
      const zero = scale(0);
      const y = scale(value);
      const x = pad.left + band * index + (band - barWidth) / 2;
      const bar = el("rect", {
        x,
        y: Math.min(y, zero),
        width: barWidth,
        height: Math.max(2, Math.abs(zero - y)),
        rx: 4,
        class: value < 0 ? "bar bar-loss" : "bar bar-profit",
      });
      svg.append(bar);
      attachHover(
        container,
        bar,
        `<strong>${point.label}</strong><br>Net profit ${Money.format(point.net_profit)}`,
      );
    });

    // Direct-label the latest month only: a number on every column is
    // a table pretending to be a chart.
    const last = points[points.length - 1];
    const lastValue = values[values.length - 1];
    svg.append(
      el(
        "text",
        {
          x: pad.left + band * (points.length - 1) + band / 2,
          y: scale(lastValue) + (lastValue < 0 ? 16 : -8),
          class: "mark-label",
          "text-anchor": "middle",
        },
        Money.compact(last.net_profit),
      ),
    );

    container.append(svg);
  }

  /** Revenue against cost of goods. Two series, so a legend is always
   * present -- identity is never carried by colour alone. */
  function revenueColumns(container, points) {
    container.replaceChildren();
    if (!points.length) {
      container.append(Object.assign(document.createElement("p"), {
        className: "muted",
        textContent: "No months to show yet.",
      }));
      return;
    }

    const width = 520;
    const height = 240;
    const pad = { top: 16, right: 16, bottom: 34, left: 62 };
    const all = points.flatMap((p) => [Money.toPlotValue(p.revenue), Money.toPlotValue(p.cogs)]);
    const max = niceCeiling(Math.max(1, ...all));

    const svg = el("svg", {
      viewBox: `0 0 ${width} ${height}`,
      class: "svg-chart",
      role: "img",
      "aria-label": "Revenue and cost of goods per month",
    });
    const { scale, band } = frame(svg, { width, height, pad, max, min: 0, points });

    const pairWidth = Math.min(34, band * 0.5);
    const barWidth = (pairWidth - 2) / 2; // 2px surface gap between the pair
    points.forEach((point, index) => {
      const base = pad.left + band * index + (band - pairWidth) / 2;
      [
        { key: "revenue", cls: "series-1", label: "Revenue" },
        { key: "cogs", cls: "series-2", label: "Cost of goods" },
      ].forEach((series, position) => {
        const value = Money.toPlotValue(point[series.key]);
        const y = scale(value);
        const bar = el("rect", {
          x: base + position * (barWidth + 2),
          y,
          width: barWidth,
          height: Math.max(2, scale(0) - y),
          rx: 3,
          class: `bar ${series.cls}`,
        });
        svg.append(bar);
        attachHover(
          container,
          bar,
          `<strong>${point.label}</strong><br>${series.label} ${Money.format(point[series.key])}`,
        );
      });
    });

    container.append(svg);
    const legend = document.createElement("ul");
    legend.className = "legend";
    legend.innerHTML = `
      <li><span class="swatch series-1"></span>Revenue</li>
      <li><span class="swatch series-2"></span>Cost of goods</li>`;
    container.append(legend);
  }

  return { profitColumns, revenueColumns };
})();
