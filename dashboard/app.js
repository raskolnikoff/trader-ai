/* trader-ai live dashboard — client code
 * Polls /feed on the webhook server and renders:
 *   - BTC price + 5m delta (from embedded Binance or TV chart fallback)
 *   - Wallet balance / positions / open orders
 *   - Markets table (fair value vs market, edge highlighting)
 *   - Activity feed (recent alerts + trades)
 *   - Optional TV chart snapshot
 *
 * Defensive: every field has a fallback path. Missing data renders "—",
 * never crashes the page. Works against both the pre-PR-18 and PR-18+
 * webhook server.
 */

(() => {
  "use strict";

  // -- Config -----------------------------------------------------------------

  const DEFAULT_BASE = window.location.protocol === "file:"
    ? "http://localhost:8765"
    : window.location.origin;
  const BASE = (new URLSearchParams(window.location.search).get("api") || DEFAULT_BASE).replace(/\/$/, "");
  const POLL_INTERVAL_MS = 2000;
  const BTC_POLL_INTERVAL_MS = 5000;   // Binance price polled separately
  const BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT";
  const BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=2";

  // -- DOM refs ---------------------------------------------------------------

  const $ = (id) => document.getElementById(id);
  const els = {
    statusDot:   document.querySelector(".status-dot"),
    statusText:  $("status-text"),
    lastUpdate:  $("last-update"),
    btcPrice:    $("btc-price"),
    btcDelta:    $("btc-delta"),
    usdce:       $("usdce-balance"),
    posValue:    $("pos-value"),
    openOrders:  $("open-orders"),
    marketsBody: $("markets-body"),
    activity:    $("activity-list"),
    activityCt:  $("activity-count"),
    tvBody:      $("tv-body"),
    tvState:     $("tv-state"),
  };

  // -- State ------------------------------------------------------------------

  const state = {
    lastBtcPrice: null,
    lastMarkets: new Map(),  // condition_id -> {yes_price, fair, edge}
    lastAlertsTs: 0,
  };

  // -- Utils ------------------------------------------------------------------

  const fmtUSD = (n, digits = 2) => {
    if (n == null || isNaN(n)) return "—";
    return "$" + Number(n).toLocaleString("en-US", {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits,
    });
  };
  const fmtPct = (n, digits = 2) => {
    if (n == null || isNaN(n)) return "—";
    const sign = n > 0 ? "+" : "";
    return `${sign}${Number(n).toFixed(digits)}%`;
  };
  const fmtTime = (ts) => {
    if (!ts) return "--:--:--";
    const d = typeof ts === "number" ? new Date(ts * 1000) : new Date(ts);
    return d.toTimeString().slice(0, 8);
  };
  const trunc = (s, n) => {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  };
  const signOf = (n) => (n > 0 ? "up" : n < 0 ? "down" : "flat");

  function flashCell(el, direction) {
    if (!el) return;
    el.classList.remove("flash-cell-up", "flash-cell-down");
    void el.offsetWidth;  // restart animation
    el.classList.add(direction === "up" ? "flash-cell-up" : "flash-cell-down");
  }

  function setStatus(state, text) {
    if (els.statusDot) els.statusDot.dataset.state = state;
    if (els.statusText) els.statusText.textContent = text;
  }

  // -- Fetchers ---------------------------------------------------------------

  async function fetchJSON(url, { timeoutMs = 4000 } = {}) {
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), timeoutMs);
    try {
      const r = await fetch(url, { signal: ctrl.signal });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } finally {
      clearTimeout(t);
    }
  }

  async function fetchFeed() {
    try {
      const data = await fetchJSON(`${BASE}/feed`);
      setStatus("live", "live");
      renderFeed(data);
    } catch (err) {
      console.warn("feed fetch failed:", err.message);
      setStatus("error", "disconnected");
    }
  }

  async function fetchBinancePrice() {
    try {
      const [priceRes, klineRes] = await Promise.allSettled([
        fetchJSON(BINANCE_PRICE_URL, { timeoutMs: 3000 }),
        fetchJSON(BINANCE_KLINES_URL, { timeoutMs: 3000 }),
      ]);

      const price = priceRes.status === "fulfilled" && priceRes.value?.price
        ? parseFloat(priceRes.value.price)
        : null;

      let delta5m = null;
      if (klineRes.status === "fulfilled" && Array.isArray(klineRes.value) && klineRes.value.length >= 2) {
        const [prev, latest] = klineRes.value;
        const prevClose = parseFloat(prev[4]);
        const latestClose = parseFloat(latest[4]);
        if (prevClose > 0) {
          delta5m = ((latestClose - prevClose) / prevClose) * 100;
        }
      }

      if (price != null) renderPrice(price, delta5m);
    } catch (err) {
      console.warn("binance fetch failed:", err.message);
    }
  }

  // -- Renderers --------------------------------------------------------------

  function renderPrice(price, delta5m) {
    if (!els.btcPrice) return;
    const prev = state.lastBtcPrice;
    els.btcPrice.textContent = fmtUSD(price, price > 10000 ? 0 : 2);

    // Flash color based on move direction
    els.btcPrice.classList.remove("flash-up", "flash-down");
    if (prev != null && price !== prev) {
      void els.btcPrice.offsetWidth;
      els.btcPrice.classList.add(price > prev ? "flash-up" : "flash-down");
      setTimeout(() => {
        els.btcPrice.classList.remove("flash-up", "flash-down");
      }, 500);
    }
    state.lastBtcPrice = price;

    // Delta pill
    if (els.btcDelta && delta5m != null) {
      const pill = els.btcDelta.querySelector(".delta-pill");
      if (pill) {
        pill.textContent = fmtPct(delta5m);
        pill.dataset.sign = signOf(delta5m);
      }
    }
  }

  function renderFeed(data) {
    if (els.lastUpdate) {
      els.lastUpdate.textContent = fmtTime(new Date());
    }

    renderPositions(data.positions || []);
    renderActivity(data.alerts || []);
    renderMarkets(data.markets || []);   // present in extended feed; graceful if absent
    renderTvChart(data.tv_chart);        // field absent on pre-PR-18 servers
  }

  function renderPositions(positions) {
    // Aggregate position value and open order count
    const totalValue = positions.reduce((acc, p) => acc + (p.size || 0) * (p.price || 0), 0);
    const openCount  = positions.filter(p => p.status === "CONFIRMED").length;

    if (els.posValue) els.posValue.textContent = fmtUSD(totalValue);
    if (els.openOrders) els.openOrders.textContent = String(openCount);

    // USDC.e balance is not in /feed today; leave as "—" until server exposes it
    if (els.usdce && els.usdce.textContent === "$--.--") {
      // Placeholder; will update when balance endpoint is wired
    }
  }

  function renderActivity(alerts) {
    if (!els.activity) return;
    if (!Array.isArray(alerts) || alerts.length === 0) {
      els.activity.innerHTML = '<li class="activity-empty muted">no events yet</li>';
      if (els.activityCt) els.activityCt.textContent = "0 events";
      return;
    }

    const rows = alerts.slice(0, 10).map(a => {
      const time = fmtTime(a.ts);
      const action = (a.action || "").toLowerCase();
      const kind = action.includes("buy") || action === "long"  ? "buy"
                 : action.includes("sell") || action === "short" ? "sell"
                 : "info";
      const actionBadge = a.action
        ? `<span class="action" data-kind="${kind}">${escapeHtml(a.action)}</span>`
        : "";
      const priceTxt = a.price != null ? fmtUSD(a.price, 2) : "";
      return `
        <li>
          <span class="time mono">${time}</span>
          <span class="event">
            ${actionBadge}${escapeHtml(a.symbol || "")} ${priceTxt ? `<span class="muted">· ${priceTxt}</span>` : ""}
          </span>
        </li>`;
    }).join("");

    els.activity.innerHTML = rows;
    if (els.activityCt) {
      els.activityCt.textContent = `${alerts.length} event${alerts.length === 1 ? "" : "s"}`;
    }
  }

  function renderMarkets(markets) {
    if (!els.marketsBody) return;
    if (!Array.isArray(markets) || markets.length === 0) {
      els.marketsBody.innerHTML =
        '<tr class="row-empty"><td colspan="5">waiting for first scan…</td></tr>';
      return;
    }

    const rows = markets.map(m => {
      const mkt = m.market_price ?? m.yes_price;
      const fair = m.fair_value;
      const edge = (fair != null && mkt != null) ? (fair - mkt) * 100 : null;
      const edgeSign = edge == null ? "flat" : signOf(edge);
      const liq = m.liquidity_usd;

      return `
        <tr>
          <td title="${escapeHtml(m.question || "")}">${escapeHtml(trunc(m.question || "", 56))}</td>
          <td class="num">${mkt != null ? mkt.toFixed(3) : "—"}</td>
          <td class="num">${fair != null ? fair.toFixed(3) : "—"}</td>
          <td class="num edge-cell" data-sign="${edgeSign}">${edge != null ? fmtPct(edge, 1) : "—"}</td>
          <td class="num muted">${liq != null ? fmtUSD(liq, 0) : "—"}</td>
        </tr>`;
    }).join("");

    els.marketsBody.innerHTML = rows;
  }

  function renderTvChart(snapshot) {
    if (!els.tvBody || !els.tvState) return;
    if (!snapshot || !snapshot.connected) {
      els.tvState.textContent = "not connected";
      // keep existing "TradingView Desktop not running" placeholder
      return;
    }

    els.tvState.textContent = "connected";
    const symbol = snapshot.symbol || "—";
    const tf     = snapshot.timeframe || "—";
    const price  = snapshot.price != null ? fmtUSD(snapshot.price) : "—";

    els.tvBody.innerHTML = `
      <div class="tv-symbol">${escapeHtml(symbol)} <span class="muted">· ${escapeHtml(String(tf))}</span></div>
      <div class="tv-meta">
        <span>price: <span class="mono tnum">${price}</span></span>
      </div>
    `;
  }

  // -- Helpers ----------------------------------------------------------------

  function escapeHtml(s) {
    if (s == null) return "";
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  // -- Boot -------------------------------------------------------------------

  setStatus("connecting", "connecting");
  fetchBinancePrice();
  fetchFeed();
  setInterval(fetchBinancePrice, BTC_POLL_INTERVAL_MS);
  setInterval(fetchFeed, POLL_INTERVAL_MS);

  // Expose a minimal hook for debugging
  window.__traderAI = { state, fetchFeed, fetchBinancePrice };
})();
