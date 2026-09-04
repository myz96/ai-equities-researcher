/* Terliatian Capital v2 — the desk.
   Vanilla JS app shell: hash router, watchlist + library persistence
   (localStorage), live quotes, SVG charts, SSE analysis stream. */

const $ = (id) => document.getElementById(id);

const INDICES = [
  { symbol: "^GSPC", name: "S&P 500" },
  { symbol: "^IXIC", name: "NASDAQ" },
  { symbol: "^AXJO", name: "ASX 200" },
];
const DEFAULT_WATCHLIST = ["AAPL", "NVDA", "MSFT", "BHP.AX"];
const SIGNAL_ICONS = { bullish: "▲", bearish: "▼", neutral: "▬" };
const QUANT_SUFFIX = "_analyst";

let ANALYSTS = {};
let runningTicker = null;
const quoteCache = {}; // symbol -> quote

/* ---------- persistence (server-backed; Neon Postgres behind the API) ---------- */
let watchlistArr = DEFAULT_WATCHLIST.slice();
let notesIndex = []; // newest-first summaries from /desk/notes
const noteCache = {}; // id -> full payload

const store = {
  get watchlist() { return watchlistArr; },
  set watchlist(list) {
    watchlistArr = list;
    fetch("/desk/state/watchlist", {
      method: "PUT", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value: list }),
    }).catch(() => {});
  },
};

function latestNote(ticker) {
  return notesIndex.find((n) => n.ticker === ticker) || null;
}

async function loadDeskData() {
  const [wl, notes] = await Promise.all([
    fetch("/desk/state/watchlist").then((r) => r.json()).catch(() => null),
    fetch("/desk/notes").then((r) => r.json()).catch(() => []),
  ]);
  if (wl && Array.isArray(wl.value)) watchlistArr = wl.value;
  notesIndex = notes || [];
}

async function migrateLocalIfNeeded() {
  if (localStorage.getItem("tc_migrated")) return;
  let reports = null, wl = null;
  try { reports = JSON.parse(localStorage.getItem("tc_reports")); } catch {}
  try { wl = JSON.parse(localStorage.getItem("tc_watchlist")); } catch {}
  if (reports || wl) {
    await fetch("/desk/import", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: Object.values(reports || {}), watchlist: wl || undefined }),
    }).catch(() => {});
  }
  localStorage.setItem("tc_migrated", "1");
}

async function fetchNoteFull(id) {
  if (noteCache[id]) return noteCache[id];
  const res = await fetch(`/desk/notes/${id}`).then((r) => r.json());
  noteCache[id] = res;
  return res;
}

/* ---------- boot ---------- */
init();

async function init() {
  try {
    await migrateLocalIfNeeded();
    await loadDeskData();
    const [config, analysts, models, usage] = await Promise.all([
      fetch("/site-config").then((r) => r.json()),
      fetch("/analyze/analysts").then((r) => r.json()),
      fetch("/analyze/models").then((r) => r.json()),
      fetch("/analyze/usage").then((r) => r.json()),
    ]);
    for (const a of analysts) ANALYSTS[a.key] = a;
    populateModels(models);
    renderCost(null, usage);
    $("site-name").textContent = config.site_name;
    $("site-tagline").textContent = config.tagline;
    document.title = config.site_name;
    $("crest-mark").textContent = config.site_name
      .split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase();
    if (config.birthday_message && !localStorage.getItem("birthdaySeen")) {
      $("birthday-text").textContent = config.birthday_message;
      $("birthday-banner").classList.remove("hidden");
    }
  } catch (e) {
    console.error("init failed", e);
  }

  $("banner-close").addEventListener("click", () => {
    $("birthday-banner").classList.add("hidden");
    localStorage.setItem("birthdaySeen", "1");
  });
  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("ticker-input").value.trim().toUpperCase();
    if (t) { $("ticker-input").value = ""; runAnalysis(t); }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) {
      e.preventDefault();
      $("ticker-input").focus();
    }
  });
  $("watch-add").addEventListener("click", () => {
    $("watch-form").classList.toggle("hidden");
    $("watch-input").focus();
  });
  $("watch-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const t = $("watch-input").value.trim().toUpperCase();
    if (t && !store.watchlist.includes(t)) store.watchlist = [...store.watchlist, t];
    $("watch-input").value = "";
    $("watch-form").classList.add("hidden");
    renderRail();
    refreshQuotes();
  });

  window.addEventListener("hashchange", renderRoute);
  renderRail();
  renderRoute();
  refreshQuotes();
  setInterval(refreshQuotes, 5 * 60 * 1000);
}

/* ---------- routing ---------- */
function currentRoute() {
  const m = location.hash.match(/^#\/t\/([A-Za-z0-9.\-^]+)/);
  return m ? { view: "ticker", ticker: m[1].toUpperCase() } : { view: "desk" };
}

function renderRoute() {
  const route = currentRoute();
  renderRail();
  if (route.view === "ticker") renderTickerPage(route.ticker);
  else renderDesk();
}

/* ---------- quotes ---------- */
async function fetchQuotes(symbols) {
  if (!symbols.length) return [];
  const qs = encodeURIComponent(symbols.join(","));
  const res = await fetch(`/market/quotes?tickers=${qs}`).then((r) => r.json()).catch(() => ({ quotes: [] }));
  for (const q of res.quotes || []) quoteCache[q.ticker] = q;
  return res.quotes || [];
}

async function refreshQuotes() {
  const symbols = [...new Set([...INDICES.map((i) => i.symbol), ...store.watchlist])];
  await fetchQuotes(symbols);
  renderRail();
  const route = currentRoute();
  if (route.view === "desk") renderDesk();
}

/* ---------- rail ---------- */
function renderRail() {
  const route = currentRoute();
  const wl = $("watchlist");
  wl.innerHTML = "";
  for (const t of store.watchlist) {
    const q = quoteCache[t];
    const row = el("a", "watch-row" + (route.view === "ticker" && route.ticker === t ? " active" : ""));
    row.href = `#/t/${t}`;
    row.appendChild(el("span", "wt", t));
    row.appendChild(el("span", "wp", q ? fmtPrice(q.last) : "—"));
    row.appendChild(el("span", "wc " + (q ? (q.change_pct >= 0 ? "up" : "dn") : ""), q ? fmtPct(q.change_pct) : ""));
    const x = el("button", "wx", "✕");
    x.title = `Remove ${t}`;
    x.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      store.watchlist = store.watchlist.filter((w) => w !== t);
      renderRail();
      if (currentRoute().view === "desk") renderDesk();
    });
    row.appendChild(x);
    wl.appendChild(row);
  }
  if (!store.watchlist.length) wl.appendChild(el("div", "rail-empty", "Empty. Add a ticker."));

  const lib = $("library");
  lib.innerHTML = "";
  const latest = dedupeByTicker(notesIndex);
  for (const n of latest.slice(0, 12)) {
    const row = el("a", "lib-row" + (route.view === "ticker" && route.ticker === n.ticker ? " active" : ""));
    row.href = `#/t/${n.ticker}`;
    row.appendChild(el("span", "lt", n.ticker));
    row.appendChild(actionChip(n.action));
    row.appendChild(el("span", "ld", shortDate(n.created_at)));
    lib.appendChild(row);
  }
  if (!latest.length) lib.appendChild(el("div", "rail-empty", "No notes yet."));
}

function dedupeByTicker(index) {
  const seen = new Set();
  return index.filter((n) => (seen.has(n.ticker) ? false : (seen.add(n.ticker), true)));
}

function actionChip(action) {
  action = String(action || "—").toUpperCase();
  const cls = ["BUY", "COVER"].includes(action) ? "bullish" : ["SELL", "SHORT"].includes(action) ? "bearish" : "";
  return el("span", `verdict-chip ${cls}`, action);
}

function decisionOf(data) {
  return (data.decisions || {})[data.ticker] || data.decisions || {};
}

/* ---------- desk ---------- */
function renderDesk() {
  if (runningTicker) { location.hash = `#/t/${runningTicker}`; return; }
  const stage = $("stage");
  stage.innerHTML = "";

  stage.appendChild(eyebrow("THE TAPE"));
  const idxRow = el("div", "index-row");
  let stagger = 0;
  for (const idx of INDICES) {
    const q = quoteCache[idx.symbol];
    const card = el("div", "index-card");
    card.style.setProperty("--stagger", `${stagger++ * 60}ms`);
    const nameRow = el("div", "ix-name");
    nameRow.style.cssText = "display:flex;justify-content:space-between";
    nameRow.append(el("span", "", idx.name), el("span", "", "30D"));
    card.appendChild(nameRow);
    const row = el("div", "ix-row");
    row.appendChild(el("span", "ix-last", q ? fmtPrice(q.last) : "—"));
    row.appendChild(el("span", "ix-chg " + (q ? (q.change_pct >= 0 ? "up" : "dn") : ""),
      q ? `${q.change_pct >= 0 ? "▲" : "▼"} ${fmtPct(q.change_pct)}` : ""));
    card.appendChild(row);
    if (q) card.appendChild(sparkline(q.spark, 54));
    idxRow.appendChild(card);
  }
  stage.appendChild(idxRow);

  stage.appendChild(eyebrow("THE WATCHLIST"));
  const table = el("div", "watch-table");
  const head = el("div", "twr th");
  for (const h of ["Ticker", "Last", "Day", "30 days", ""]) head.appendChild(el("span", "", h));
  table.appendChild(head);
  for (const t of store.watchlist) {
    const q = quoteCache[t];
    const row = el("a", "twr");
    row.href = `#/t/${t}`;
    row.appendChild(el("span", "c-t", t));
    row.appendChild(el("span", "c-l", q ? fmtPrice(q.last) : "—"));
    row.appendChild(el("span", "c-c " + (q ? (q.change_pct >= 0 ? "up" : "dn") : ""), q ? fmtPct(q.change_pct) : ""));
    const sp = el("span", "c-s");
    if (q) sp.appendChild(sparkline(q.spark, 22, 150));
    row.appendChild(sp);
    const act = el("span", "c-a");
    const btn = el("button", "btn-quiet", "Convene");
    btn.addEventListener("click", (e) => { e.preventDefault(); runAnalysis(t); });
    act.appendChild(btn);
    row.appendChild(act);
    table.appendChild(row);
  }
  stage.appendChild(table);

  stage.appendChild(eyebrow("THE LIBRARY"));
  const latest = dedupeByTicker(notesIndex);
  if (!latest.length) {
    const empty = el("div", "desk-empty");
    empty.innerHTML = "No research on file. Type a ticker above — or press <b>/</b> — and convene the committee for its first note.";
    stage.appendChild(empty);
  } else {
    const grid = el("div", "lib-grid");
    let s = 0;
    for (const n of latest) {
      const card = el("a", "lib-card");
      card.style.setProperty("--stagger", `${Math.min(s++, 8) * 50}ms`);
      card.href = `#/t/${n.ticker}`;
      const row1 = el("div", "row1");
      row1.appendChild(el("span", "t", n.ticker));
      row1.appendChild(actionChip(n.action));
      card.appendChild(row1);
      card.appendChild(el("div", "meta",
        `${shortDate(n.created_at)} · ${String(n.model_name || "").split("/").pop()}`));
      if (n.thesis) card.appendChild(el("p", "thesis", n.thesis));
      grid.appendChild(card);
    }
    stage.appendChild(grid);
  }
}

/* ---------- ticker page ---------- */
async function renderTickerPage(ticker) {
  const stage = $("stage");
  stage.innerHTML = "";

  const head = el("div", "tkr-head");
  head.appendChild(el("span", "tk", ticker));
  const px = el("span", "px", "");
  const chg = el("span", "chg", "");
  head.appendChild(px);
  head.appendChild(chg);
  head.appendChild(el("span", "spacer"));
  head.appendChild(el("span", "asof", "DAILY · 3 MONTHS"));
  stage.appendChild(head);

  const chartPanel = el("div", "chart-panel");
  const loading = el("div", "rail-empty", "Loading the tape…");
  chartPanel.appendChild(loading);
  stage.appendChild(chartPanel);

  const noteHost = el("div", "");
  stage.appendChild(noteHost);
  await renderNoteInto(noteHost, ticker);
  if (currentRoute().ticker !== ticker) return;

  const [quotes, priceData] = await Promise.all([
    quoteCache[ticker] ? Promise.resolve([quoteCache[ticker]]) : fetchQuotes([ticker]),
    fetch(`/market/prices?ticker=${encodeURIComponent(ticker)}&months=3`).then((r) => r.json()).catch(() => null),
  ]);
  if (currentRoute().ticker !== ticker) return;
  const q = quoteCache[ticker];
  if (q) {
    px.textContent = fmtPrice(q.last);
    chg.textContent = `${q.change_pct >= 0 ? "▲" : "▼"} ${fmtPct(q.change_pct)}`;
    chg.className = "chg " + (q.change_pct >= 0 ? "up" : "dn");
  }
  if (priceData && priceData.prices && priceData.prices.length > 1) {
    drawBigChart(chartPanel, priceData.prices);
  } else {
    chartPanel.innerHTML = "";
    chartPanel.appendChild(el("div", "rail-empty", "No price history found for this ticker."));
  }
}

async function renderNoteInto(host, ticker) {
  host.innerHTML = "";
  if (runningTicker === ticker) {
    host.appendChild(sessionView());
    return;
  }
  const summary = latestNote(ticker);
  if (!summary) {
    const box = el("div", "no-note");
    box.appendChild(el("p", "", "The committee holds no note on this name."));
    const btn = el("button", "btn-brass", "Convene the committee");
    btn.addEventListener("click", () => runAnalysis(ticker));
    box.appendChild(btn);
    host.appendChild(box);
    return;
  }
  const full = await fetchNoteFull(summary.id).catch(() => null);
  if (!full) {
    host.appendChild(el("div", "error-card", "Could not load the note."));
    return;
  }
  host.appendChild(noteView(full));
}

/* ---------- session ---------- */
function sessionView() {
  const wrap = el("div", "");
  const title = el("h2", "session-title");
  title.append("The committee is deliberating", el("span", "ellipsis"));
  wrap.appendChild(title);
  const grid = el("div", "progress-grid");
  grid.id = "progress-grid";
  wrap.appendChild(grid);
  return wrap;
}

function onProgress(event) {
  const grid = $("progress-grid");
  if (!grid || !event.agent || event.agent === "system") return;
  const id = `chip-${event.agent}`;
  let chip = document.getElementById(id);
  if (!chip) {
    chip = el("div", "progress-chip");
    chip.id = id;
    chip.appendChild(el("span", "who", displayName(event.agent)));
    chip.appendChild(el("span", "status", ""));
    grid.appendChild(chip);
  }
  chip.querySelector(".status").textContent = event.status || "";
  chip.classList.toggle("done", /^done$/i.test(event.status || ""));
  chip.classList.toggle("failed", /failed|error/i.test(event.status || ""));
}

/* ---------- run ---------- */
async function runAnalysis(ticker) {
  if (runningTicker) return;
  runningTicker = ticker;
  $("analyze-btn").disabled = true;
  if (!store.watchlist.includes(ticker)) store.watchlist = [...store.watchlist, ticker];
  location.hash = `#/t/${ticker}`;
  renderRoute();
  fetchQuotes([ticker]).then(() => { if (currentRoute().ticker === ticker) renderRoute(); });

  try {
    const response = await fetch("/analyze/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, model_name: $("model-select").value || undefined }),
    });
    if (!response.ok) throw new Error(`Server error ${response.status}`);
    await consumeSSE(response.body, {
      progress: onProgress,
      complete: (event) => {
        const d = event.data;
        const decision = decisionOf(d);
        notesIndex.unshift({
          id: d.note_id, ticker: d.ticker, created_at: new Date().toISOString(),
          model_name: d.model_name, run_cost: d.run_cost,
          action: decision.action, confidence: decision.confidence, thesis: decision.reasoning,
        });
        if (d.note_id != null) noteCache[d.note_id] = { id: d.note_id, ticker: d.ticker, created_at: new Date().toISOString(), run_cost: d.run_cost, data: d };
        renderCost(d.run_cost, d.usage);
      },
      error: (event) => { throw new Error(event.message); },
    });
  } catch (e) {
    runningTicker = null;
    $("analyze-btn").disabled = false;
    const stage = $("stage");
    const err = el("div", "error-card", `The session failed: ${e.message}`);
    stage.appendChild(err);
    return;
  }
  runningTicker = null;
  $("analyze-btn").disabled = false;
  renderRoute();
}

async function consumeSSE(stream, handlers) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop();
    for (const raw of events) {
      let type = "message";
      let data = "";
      for (const line of raw.split("\n")) {
        if (line.startsWith("event: ")) type = line.slice(7).trim();
        else if (line.startsWith("data: ")) data += line.slice(6);
      }
      if (handlers[type] && data) handlers[type](JSON.parse(data));
    }
  }
}

/* ---------- the note ---------- */
function noteView(rec) {
  const data = rec.data;
  const ticker = data.ticker;
  const wrap = el("div", "");
  const decision = decisionOf(data);
  const entries = collectSignals(data.analyst_signals || {}, ticker);

  // hero: stamp + thesis | tally
  const hero = el("div", "note-hero");
  const left = el("div", "left");
  const stampRow = el("div", "stamp-row");
  const action = String(decision.action || "—").toUpperCase();
  const cls = ["BUY", "COVER"].includes(action) ? "bullish" : ["SELL", "SHORT"].includes(action) ? "bearish" : "";
  stampRow.appendChild(el("div", `stamp ${cls}`, action));
  stampRow.appendChild(el("div", "stamp-sub",
    `${decision.confidence != null ? Math.round(decision.confidence) + "% CONVICTION" : ""}` +
    `${decision.quantity ? ` · ${decision.quantity} SHARES` : ""}`));
  left.appendChild(stampRow);
  if (decision.reasoning) left.appendChild(el("p", "thesis", decision.reasoning));
  hero.appendChild(left);

  const right = el("div", "right");
  right.appendChild(el("div", "tally-label", "The vote"));
  const counts = { bullish: 0, neutral: 0, bearish: 0 };
  for (const e of entries) if (counts[e.signal] != null) counts[e.signal] += 1;
  const total = Math.max(1, counts.bullish + counts.neutral + counts.bearish);
  const meter = el("div", "consensus-meter");
  meter.setAttribute("role", "img");
  meter.setAttribute("aria-label", `${counts.bullish} bullish, ${counts.neutral} neutral, ${counts.bearish} bearish`);
  for (const [key, seg] of [["bullish", "bull"], ["neutral", "neutral"], ["bearish", "bear"]]) {
    if (!counts[key]) continue;
    const s = el("div", `seg ${seg}`);
    s.style.width = `${(counts[key] / total) * 100}%`;
    meter.appendChild(s);
  }
  right.appendChild(meter);
  const legend = el("div", "consensus-legend");
  const swatches = { bullish: "var(--bull)", neutral: "var(--parchment-40)", bearish: "var(--bear)" };
  for (const key of ["bullish", "neutral", "bearish"]) {
    const item = el("span", "key");
    const sw = el("span", "swatch");
    sw.style.background = swatches[key];
    item.append(sw, document.createTextNode(
      `${SIGNAL_ICONS[key]} ${key[0].toUpperCase() + key.slice(1)} · ${counts[key]}`));
    legend.appendChild(item);
  }
  right.appendChild(legend);
  hero.appendChild(right);
  wrap.appendChild(hero);

  // body: debate + investors | quant rail + meta
  const body = el("div", "note-body");
  const main = el("div", "note-main");
  const rail = el("div", "note-rail");

  const debate = (data.analyst_signals || {}).debate_room_agent?.[ticker];
  if (debate) {
    main.appendChild(eyebrow("I · THE DEBATE"));
    main.appendChild(debateView(debate));
  }
  main.appendChild(eyebrow("II · THE INVESTORS"));
  const personaGrid = el("div", "card-grid");
  main.appendChild(personaGrid);

  rail.appendChild(eyebrow("III · THE QUANT DESK"));
  const quantGrid = el("div", "");
  rail.appendChild(quantGrid);
  rail.appendChild(eyebrow("THE RECORD"));
  const meta = el("div", "meta-card");
  metaRow(meta, "Note dated", shortDate(rec.created_at || Date.now()));
  metaRow(meta, "Window", `${data.start_date} → ${data.end_date}`);
  metaRow(meta, "Brain", String(data.model_name || "").split("/").pop());
  const cost = rec.run_cost ?? data.run_cost;
  if (cost != null) metaRow(meta, "Cost", `$${Number(cost).toFixed(2)}`);
  rail.appendChild(meta);

  renderPlaques(entries, personaGrid, quantGrid);
  body.append(main, rail);
  wrap.appendChild(body);
  return wrap;
}

function metaRow(host, k, v) {
  const row = el("div", "");
  row.append(el("span", "", k), el("span", "", v));
  host.appendChild(row);
}

function debateView(debate) {
  const card = el("div", "debate-card");
  card.appendChild(el("h4", "", "The devil's advocate"));
  card.appendChild(el("p", "", debate.reasoning || ""));
  const disagreements = debate.disagreements || [];
  if (disagreements.length) {
    const list = el("div", "disagreements");
    for (const d of disagreements) {
      const item = el("div", "disagreement");
      item.appendChild(el("div", "topic", d.topic));
      const sides = el("div", "sides");
      const bull = el("div", "side bull");
      bull.appendChild(el("b", "", `${SIGNAL_ICONS.bullish} Bull case`));
      bull.appendChild(document.createTextNode(d.bull_view));
      const bear = el("div", "side bear");
      bear.appendChild(el("b", "", `${SIGNAL_ICONS.bearish} Bear case`));
      bear.appendChild(document.createTextNode(d.bear_view));
      sides.append(bull, bear);
      item.appendChild(sides);
      list.appendChild(item);
    }
    card.appendChild(list);
  }
  if (debate.consensus_verdict) {
    const verdict = el("p", "verdict");
    verdict.appendChild(el("b", "", "How solid is the consensus? "));
    verdict.appendChild(document.createTextNode(debate.consensus_verdict));
    card.appendChild(verdict);
  }
  return card;
}

function collectSignals(signals, ticker) {
  const entries = [];
  for (const [agentKey, byTicker] of Object.entries(signals)) {
    if (agentKey.startsWith("risk_management") || agentKey.startsWith("debate_room")) continue;
    const entry = byTicker?.[ticker];
    if (!entry) continue;
    const configKey = agentKey.replace(/_agent$/, "");
    entries.push({
      agentKey,
      configKey,
      isQuant: configKey.endsWith(QUANT_SUFFIX) || configKey === "news_sentiment",
      signal: String(entry.signal || "neutral").toLowerCase(),
      confidence: Number(entry.confidence || 0),
      reasoning: formatReasoning(entry.reasoning),
    });
  }
  entries.sort((a, b) => (ANALYSTS[a.configKey]?.order ?? 99) - (ANALYSTS[b.configKey]?.order ?? 99));
  return entries;
}

function renderPlaques(entries, personaGrid, quantGrid) {
  let index = 0;
  for (const entry of entries) {
    const meta = ANALYSTS[entry.configKey] || {};
    const name = meta.display_name || displayName(entry.agentKey);
    const card = el("div", `analyst-card sig-${entry.signal}`);
    card.style.setProperty("--stagger", `${Math.min(index++, 12) * 40}ms`);

    const head = el("div", "head");
    const medallion = el("div", "medallion",
      name.split(/\s+/).map((w) => w[0]).join("").slice(0, 2).toUpperCase());
    const idBlock = el("div", "who");
    idBlock.appendChild(el("div", "name", name));
    if (meta.description) idBlock.appendChild(el("div", "epithet", meta.description));
    head.append(medallion, idBlock);
    card.appendChild(head);

    const signalRow = el("div", "signal-row");
    signalRow.appendChild(el("span", `badge ${entry.signal}`,
      `${SIGNAL_ICONS[entry.signal] || ""} ${entry.signal.toUpperCase()}`));
    signalRow.appendChild(el("span", "conf", `${Math.round(entry.confidence)}% conviction`));
    card.appendChild(signalRow);

    const reasoning = el("p", "reasoning", entry.reasoning);
    card.appendChild(reasoning);
    if ((entry.reasoning || "").length > 240) {
      const btn = el("button", "read-more", "Read the view");
      btn.addEventListener("click", () => {
        const expanded = card.classList.toggle("expanded");
        btn.textContent = expanded ? "Fold away" : "Read the view";
      });
      card.appendChild(btn);
    }
    (entry.isQuant ? quantGrid : personaGrid).appendChild(card);
  }
}

/* ---------- charts ---------- */
const NS = "http://www.w3.org/2000/svg";

function svgEl(tag, attrs) {
  const node = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

function sparkline(closes, height, width = 200) {
  const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, height, preserveAspectRatio: "none" });
  const min = Math.min(...closes), max = Math.max(...closes);
  const range = max - min || 1;
  const up = closes[closes.length - 1] >= closes[0];
  const pad = 2;
  const points = closes.map((c, i) => {
    const x = (i / (closes.length - 1)) * width;
    const y = pad + (1 - (c - min) / range) * (height - pad * 2);
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  }).join(" ");
  svg.appendChild(svgEl("polyline", {
    points, fill: "none", "stroke-width": 1.5, "stroke-linejoin": "round",
    stroke: up ? "var(--bull)" : "var(--bear)",
  }));
  return svg;
}

function drawBigChart(panel, prices) {
  panel.innerHTML = "";
  const W = 900, H = 220, padX = 6, padTop = 14, padBot = 22;
  const closes = prices.map((p) => p.close);
  const min = Math.min(...closes), max = Math.max(...closes);
  const range = max - min || 1;
  const up = closes[closes.length - 1] >= closes[0];
  const color = up ? "var(--bull)" : "var(--bear)";
  const x = (i) => padX + (i / (closes.length - 1)) * (W - padX * 2);
  const y = (c) => padTop + (1 - (c - min) / range) * (H - padTop - padBot);

  const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "none", height: 240 });

  for (const gy of [padTop, (padTop + H - padBot) / 2, H - padBot]) {
    svg.appendChild(svgEl("line", { x1: padX, x2: W - padX, y1: gy, y2: gy, stroke: "var(--hairline-soft)", "stroke-width": 1 }));
  }
  const pts = closes.map((c, i) => `${x(i).toFixed(1)},${y(c).toFixed(1)}`);
  const area = svgEl("polygon", {
    points: `${padX},${H - padBot} ${pts.join(" ")} ${W - padX},${H - padBot}`,
    fill: color, opacity: 0.08,
  });
  svg.appendChild(area);
  svg.appendChild(svgEl("polyline", {
    points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 2,
    "stroke-linejoin": "round", "stroke-linecap": "round", "vector-effect": "non-scaling-stroke",
  }));
  const lastX = x(closes.length - 1), lastY = y(closes[closes.length - 1]);
  svg.appendChild(svgEl("circle", { cx: lastX, cy: lastY, r: 4.5, fill: color, stroke: "var(--chamber)", "stroke-width": 2 }));
  panel.appendChild(svg);

  // labels: high / low / endpoints
  const labels = el("div", "");
  labels.style.cssText = "display:flex;justify-content:space-between;font-family:var(--mono);font-size:10.5px;color:var(--parchment-40);padding:6px 2px 2px;letter-spacing:.05em";
  labels.append(
    el("span", "", `${prices[0].date}  ·  L ${fmtPrice(min)}`),
    el("span", "", `H ${fmtPrice(max)}  ·  ${prices[prices.length - 1].date}`),
  );
  panel.appendChild(labels);

  // hover crosshair + tooltip
  const tip = el("div", "chart-tip");
  panel.appendChild(tip);
  const cross = svgEl("line", { y1: padTop, y2: H - padBot, stroke: "var(--hairline)", "stroke-width": 1 });
  cross.style.display = "none";
  svg.appendChild(cross);
  const dot = svgEl("circle", { r: 3.5, fill: color, stroke: "var(--chamber)", "stroke-width": 2 });
  dot.style.display = "none";
  svg.appendChild(dot);

  svg.addEventListener("mousemove", (e) => {
    const rect = svg.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    const i = Math.round(frac * (closes.length - 1));
    const cx = x(i), cy = y(closes[i]);
    cross.setAttribute("x1", cx); cross.setAttribute("x2", cx);
    cross.style.display = "";
    dot.setAttribute("cx", cx); dot.setAttribute("cy", cy);
    dot.style.display = "";
    tip.style.display = "block";
    tip.style.left = `${(cx / W) * rect.width + 18}px`;
    tip.style.top = `${(cy / H) * 240 + 16}px`;
    tip.textContent = `${prices[i].date} · ${fmtPrice(closes[i])}`;
  });
  svg.addEventListener("mouseleave", () => {
    cross.style.display = "none"; dot.style.display = "none"; tip.style.display = "none";
  });
}

/* ---------- shared bits ---------- */
function populateModels(models) {
  const select = $("model-select");
  const saved = localStorage.getItem("model") || models.default;
  for (const m of models.models) {
    const option = document.createElement("option");
    option.value = m.model_name;
    option.textContent = m.display_name;
    if (m.model_name === saved) option.selected = true;
    select.appendChild(option);
  }
  select.addEventListener("change", () => localStorage.setItem("model", select.value));
}

function renderCost(runCost, usage) {
  const parts = [];
  if (runCost != null) parts.push(`$${runCost.toFixed(2)}`);
  if (usage && usage.used != null) {
    parts.push(usage.limit != null ? `$${usage.used.toFixed(2)} / $${usage.limit}` : `$${usage.used.toFixed(2)}`);
  }
  $("cost-line").textContent = parts.join(" · ");
}

function displayName(agentKey) {
  const key = agentKey.replace(/_agent$/, "");
  if (ANALYSTS[key]) return ANALYSTS[key].display_name;
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatReasoning(reasoning) {
  if (reasoning == null) return "";
  if (typeof reasoning === "string") return reasoning;
  const lines = [];
  const label = (key) => key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  for (const [key, value] of Object.entries(reasoning)) {
    if (value == null) continue;
    if (typeof value === "object") {
      const signal = value.signal ? String(value.signal).toUpperCase() : null;
      const confidence = value.confidence != null ? ` (${Math.round(value.confidence)}%)` : "";
      const details = value.details || value.metrics || "";
      const detailText = typeof details === "string" ? details : "";
      if (signal) lines.push(`${label(key)}: ${signal}${confidence}${detailText ? " — " + detailText : ""}`);
      else if (detailText) lines.push(`${label(key)}: ${detailText}`);
      else {
        const scalars = Object.entries(value)
          .filter(([, v]) => typeof v !== "object" || v === null)
          .map(([k, v]) => `${label(k).toLowerCase()} ${typeof v === "number" ? +v.toFixed(3) : v}`)
          .slice(0, 5);
        lines.push(`${label(key)}: ${scalars.length ? scalars.join(", ") : JSON.stringify(value)}`);
      }
    } else {
      lines.push(`${label(key)}: ${value}`);
    }
  }
  return lines.join("\n");
}

function eyebrow(text) {
  const wrap = el("div", "eyebrow");
  wrap.appendChild(el("span", "", text));
  return wrap;
}

function fmtPrice(v) {
  if (v == null) return "—";
  return v >= 1000 ? v.toLocaleString("en-US", { maximumFractionDigits: 0 })
    : v.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtPct(v) { return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`; }
function shortDate(ts) {
  return new Date(ts).toLocaleDateString("en-AU", { day: "numeric", month: "short" });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
