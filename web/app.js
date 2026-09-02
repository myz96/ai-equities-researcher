/* Report UI for the AI equity analyst. Vanilla JS, streams SSE from /analyze/run. */

const $ = (id) => document.getElementById(id);

let ANALYSTS = {}; // config key -> {display_name, description, investing_style}

const QUANT_SUFFIX = "_analyst";
const SIGNAL_ICONS = { bullish: "▲", bearish: "▼", neutral: "▬" };

init();

async function init() {
  try {
    const [config, analysts, models, usage] = await Promise.all([
      fetch("/site-config").then((r) => r.json()),
      fetch("/analyze/analysts").then((r) => r.json()),
      fetch("/analyze/models").then((r) => r.json()),
      fetch("/analyze/usage").then((r) => r.json()),
    ]);
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
    for (const a of analysts) ANALYSTS[a.key] = a;
  } catch (e) {
    console.error("init failed", e);
  }

  $("banner-close").addEventListener("click", () => {
    $("birthday-banner").classList.add("hidden");
    localStorage.setItem("birthdaySeen", "1");
  });

  $("search-form").addEventListener("submit", (e) => {
    e.preventDefault();
    runAnalysis($("ticker-input").value.trim().toUpperCase());
  });
}

function agentToConfigKey(agentKey) {
  return agentKey.replace(/_agent$/, "");
}

function displayName(agentKey) {
  const key = agentToConfigKey(agentKey);
  if (ANALYSTS[key]) return ANALYSTS[key].display_name;
  return key.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

async function runAnalysis(ticker) {
  if (!ticker) return;
  $("analyze-btn").disabled = true;
  $("report-section").classList.add("hidden");
  $("error-section").classList.add("hidden");
  $("progress-section").classList.remove("hidden");
  $("progress-grid").innerHTML = "";

  try {
    const response = await fetch("/analyze/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ticker, model_name: $("model-select").value || undefined }),
    });
    if (!response.ok) throw new Error(`Server error ${response.status}`);
    await consumeSSE(response.body, {
      progress: onProgress,
      complete: (event) => renderReport(event.data),
      error: (event) => showError(event.message),
    });
  } catch (e) {
    showError(e.message);
  } finally {
    $("analyze-btn").disabled = false;
    $("progress-section").classList.add("hidden");
  }
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

function onProgress(event) {
  if (!event.agent || event.agent === "system") return;
  const id = `chip-${event.agent}`;
  let chip = $(id);
  if (!chip) {
    chip = document.createElement("div");
    chip.id = id;
    chip.className = "progress-chip";
    chip.innerHTML = `<span class="who"></span><span class="status"></span>`;
    chip.querySelector(".who").textContent = displayName(event.agent);
    $("progress-grid").appendChild(chip);
  }
  chip.querySelector(".status").textContent = event.status || "";
  chip.classList.toggle("done", /^done$/i.test(event.status || ""));
  chip.classList.toggle("failed", /failed|error/i.test(event.status || ""));
}

function showError(message) {
  $("error-text").textContent = message || "Something went wrong.";
  $("error-section").classList.remove("hidden");
}

function renderReport(data) {
  const ticker = data.ticker;
  const signals = data.analyst_signals || {};
  const decision = (data.decisions || {})[ticker] || data.decisions || {};

  $("report-title").textContent = ticker;
  $("report-meta").textContent =
    `Analysis window ${data.start_date} → ${data.end_date} · model ${data.model_name}`;

  renderDecision(decision);
  const entries = collectSignals(signals, ticker);
  renderConsensus(entries);
  renderDebate(signals.debate_room_agent?.[ticker]);
  renderCards(entries);
  renderCost(data.run_cost, data.usage);

  $("report-section").classList.remove("hidden");
  $("report-section").scrollIntoView({ behavior: "smooth" });
}

function collectSignals(signals, ticker) {
  const entries = [];
  for (const [agentKey, byTicker] of Object.entries(signals)) {
    if (agentKey.startsWith("risk_management") || agentKey.startsWith("debate_room")) continue;
    const entry = byTicker?.[ticker];
    if (!entry) continue;
    const configKey = agentToConfigKey(agentKey);
    entries.push({
      agentKey,
      configKey,
      isQuant: configKey.endsWith(QUANT_SUFFIX) || configKey === "news_sentiment",
      signal: String(entry.signal || "neutral").toLowerCase(),
      confidence: Number(entry.confidence || 0),
      reasoning: formatReasoning(entry.reasoning),
    });
  }
  return entries;
}

function renderDecision(decision) {
  const hero = $("decision-hero");
  const action = String(decision.action || "—").toUpperCase();
  const cls = ["BUY", "COVER"].includes(action) ? "bullish"
    : ["SELL", "SHORT"].includes(action) ? "bearish" : "";
  hero.innerHTML = "";
  const stampEl = el("div", `stamp ${cls}`, action);
  const subEl = el("div", "sub",
    `${decision.confidence != null ? Math.round(decision.confidence) + "% CONVICTION" : ""}` +
    `${decision.quantity ? ` · ${decision.quantity} SHARES` : ""}`);
  const rationaleEl = el("p", "rationale", decision.reasoning || "");
  hero.append(stampEl, subEl, rationaleEl);
}

function renderConsensus(entries) {
  const counts = { bullish: 0, neutral: 0, bearish: 0 };
  for (const e of entries) if (counts[e.signal] != null) counts[e.signal] += 1;
  const total = Math.max(1, counts.bullish + counts.neutral + counts.bearish);

  const meter = $("consensus-meter");
  meter.innerHTML = "";
  meter.setAttribute("aria-label",
    `${counts.bullish} bullish, ${counts.neutral} neutral, ${counts.bearish} bearish`);
  for (const [key, cls] of [["bullish", "bull"], ["neutral", "neutral"], ["bearish", "bear"]]) {
    if (!counts[key]) continue;
    const seg = el("div", `seg ${cls}`);
    seg.style.width = `${(counts[key] / total) * 100}%`;
    meter.appendChild(seg);
  }

  const legend = $("consensus-legend");
  legend.innerHTML = "";
  const swatches = { bullish: "var(--bull)", neutral: "var(--muted)", bearish: "var(--bear)" };
  for (const key of ["bullish", "neutral", "bearish"]) {
    const item = el("span", "key");
    const swatch = el("span", "swatch");
    swatch.style.background = swatches[key];
    item.append(swatch, document.createTextNode(
      `${SIGNAL_ICONS[key]} ${key[0].toUpperCase() + key.slice(1)} · ${counts[key]}`));
    legend.appendChild(item);
  }
}

function renderDebate(debate) {
  const block = $("debate-block");
  block.innerHTML = "";
  if (!debate) return;

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
  const eyebrow = el("div", "eyebrow");
  eyebrow.appendChild(el("span", "", "II · THE DEBATE"));
  block.append(eyebrow, card);
}

function renderCards(entries) {
  const personaGrid = $("persona-grid");
  const quantGrid = $("quant-grid");
  personaGrid.innerHTML = "";
  quantGrid.innerHTML = "";

  entries.sort((a, b) => (ANALYSTS[a.configKey]?.order ?? 99) - (ANALYSTS[b.configKey]?.order ?? 99));

  let index = 0;
  for (const entry of entries) {
    const meta = ANALYSTS[entry.configKey] || {};
    const name = meta.display_name || displayName(entry.agentKey);
    const card = el("div", `analyst-card sig-${entry.signal}`);
    card.style.setProperty("--stagger", `${Math.min(index++, 12) * 45}ms`);

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
    if ((entry.reasoning || "").length > 260) {
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
  if (runCost != null) parts.push(`This report: $${runCost.toFixed(2)}`);
  if (usage && usage.used != null) {
    const total = `$${usage.used.toFixed(2)}`;
    parts.push(usage.limit != null ? `Total: ${total} of $${usage.limit}` : `Total: ${total}`);
  }
  $("cost-line").textContent = parts.join(" · ");
}

function formatReasoning(reasoning) {
  if (reasoning == null) return "";
  if (typeof reasoning === "string") return reasoning;
  // Quant agents return nested objects; flatten them into readable lines.
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

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
