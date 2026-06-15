const form = document.querySelector("#controls");
const refreshButton = document.querySelector("#refreshButton");
const screenForm = document.querySelector("#screenerControls");
const screenButton = document.querySelector("#screenButton");
const autoRefresh = document.querySelector("#autoRefresh");
const connectionStatus = document.querySelector("#connectionStatus");
const rowsBody = document.querySelector("#rowsBody");
const screenRowsBody = document.querySelector("#screenRowsBody");

const fields = {
  symbol: document.querySelector("#symbol"),
  range: document.querySelector("#range"),
  last: document.querySelector("#last"),
  bandPeriod: document.querySelector("#bandPeriod"),
  stdMultiplier: document.querySelector("#stdMultiplier"),
  volumePeriod: document.querySelector("#volumePeriod"),
  volumeMultiplier: document.querySelector("#volumeMultiplier"),
  kdPeriod: document.querySelector("#kdPeriod"),
  kSmoothing: document.querySelector("#kSmoothing"),
  dSmoothing: document.querySelector("#dSmoothing"),
  rsiPeriod: document.querySelector("#rsiPeriod"),
  macdFast: document.querySelector("#macdFast"),
  macdSlow: document.querySelector("#macdSlow"),
  macdSignal: document.querySelector("#macdSignal"),
  screenStrategy: document.querySelector("#screenStrategy"),
  screenLimit: document.querySelector("#screenLimit"),
  screenUniverse: document.querySelector("#screenUniverse"),
  screenMaxSymbols: document.querySelector("#screenMaxSymbols"),
  screenSymbols: document.querySelector("#screenSymbols"),
  screenUpdated: document.querySelector("#screenUpdated"),
  latestDate: document.querySelector("#latestDate"),
  latestSignal: document.querySelector("#latestSignal"),
  latestAction: document.querySelector("#latestAction"),
  latestReason: document.querySelector("#latestReason"),
  closeValue: document.querySelector("#closeValue"),
  barCount: document.querySelector("#barCount"),
  bandPosition: document.querySelector("#bandPosition"),
  bandValues: document.querySelector("#bandValues"),
  volumeRatio: document.querySelector("#volumeRatio"),
  volumeValues: document.querySelector("#volumeValues"),
  volumePriceSignal: document.querySelector("#volumePriceSignal"),
  turnoverValues: document.querySelector("#turnoverValues"),
  volumePriceReason: document.querySelector("#volumePriceReason"),
  kdValues: document.querySelector("#kdValues"),
  kdSignal: document.querySelector("#kdSignal"),
  kdReason: document.querySelector("#kdReason"),
  rsiValue: document.querySelector("#rsiValue"),
  rsiSignal: document.querySelector("#rsiSignal"),
  rsiReason: document.querySelector("#rsiReason"),
  macdValues: document.querySelector("#macdValues"),
  macdSignalSummary: document.querySelector("#macdSignalSummary"),
  macdReason: document.querySelector("#macdReason"),
  trendValues: document.querySelector("#trendValues"),
  structureValues: document.querySelector("#structureValues"),
  trendReason: document.querySelector("#trendReason"),
  boxSignal: document.querySelector("#boxSignal"),
  boxValues: document.querySelector("#boxValues"),
  boxReason: document.querySelector("#boxReason"),
  dowSignal: document.querySelector("#dowSignal"),
  dowValues: document.querySelector("#dowValues"),
  dowReason: document.querySelector("#dowReason"),
  reliabilityScore: document.querySelector("#reliabilityScore"),
  reliabilitySignal: document.querySelector("#reliabilitySignal"),
  reliabilityReason: document.querySelector("#reliabilityReason"),
  planAction: document.querySelector("#planAction"),
  planLevels: document.querySelector("#planLevels"),
  planReason: document.querySelector("#planReason"),
  lastUpdated: document.querySelector("#lastUpdated"),
  riskNote: document.querySelector("#riskNote"),
};

let refreshTimer = null;

function formatNumber(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  });
}

function formatInt(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function formatCompact(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toLocaleString("zh-TW", {
    notation: "compact",
    maximumFractionDigits: 1,
  });
}

function actionClass(action) {
  return String(action || "").toLowerCase();
}

function kdDetail(row) {
  const parts = [row.kdSignalText];
  if (row.kdSaturation && !["NONE", "NO_DATA"].includes(row.kdSaturation)) {
    parts.push(row.kdSaturationText);
  }
  if (row.kdDivergenceSignal && !["NONE", "NO_DATA"].includes(row.kdDivergenceSignal)) {
    parts.push(row.kdDivergenceText);
  }
  return parts.filter(Boolean).join(" / ") || "--";
}

function kdReason(row) {
  const parts = [row.kdReason];
  if (row.kdSaturation && !["NONE", "NO_DATA"].includes(row.kdSaturation)) {
    parts.push(row.kdSaturationReason);
  }
  if (row.kdDivergenceSignal && !["NONE", "NO_DATA"].includes(row.kdDivergenceSignal)) {
    parts.push(row.kdDivergenceReason);
  }
  return parts.filter(Boolean).join(" ");
}

function setStatus(label, state) {
  connectionStatus.textContent = label;
  connectionStatus.className = `status-pill ${state ? `is-${state}` : ""}`.trim();
}

function paramsFromForm() {
  const params = new URLSearchParams();
  params.set("symbol", fields.symbol.value.trim() || "NVDA");
  params.set("range", fields.range.value);
  params.set("last", fields.last.value);
  params.set("bandPeriod", fields.bandPeriod.value);
  params.set("stdMultiplier", fields.stdMultiplier.value);
  params.set("volumePeriod", fields.volumePeriod.value);
  params.set("volumeMultiplier", fields.volumeMultiplier.value);
  params.set("kdPeriod", fields.kdPeriod.value);
  params.set("kSmoothing", fields.kSmoothing.value);
  params.set("dSmoothing", fields.dSmoothing.value);
  params.set("rsiPeriod", fields.rsiPeriod.value);
  params.set("macdFast", fields.macdFast.value);
  params.set("macdSlow", fields.macdSlow.value);
  params.set("macdSignal", fields.macdSignal.value);
  return params;
}

function screenParamsFromForm() {
  const params = new URLSearchParams();
  params.set("strategy", fields.screenStrategy.value);
  params.set("limit", fields.screenLimit.value);
  params.set("universe", fields.screenUniverse.value);
  params.set("maxSymbols", fields.screenMaxSymbols.value);
  params.set("symbols", fields.screenSymbols.value);
  params.set("range", fields.range.value);
  return params;
}

function renderLatest(data) {
  const latest = data.latest;
  const isShortHistory = latest.historyMode === "SHORT_HISTORY";
  const klass = actionClass(latest.action);
  fields.latestDate.textContent = latest.date;
  fields.latestSignal.textContent = latest.signalText;
  fields.latestAction.textContent = latest.actionText;
  fields.latestAction.className = klass ? `action-${klass}` : "";
  fields.latestReason.textContent = latest.reasonText;
  fields.closeValue.textContent = formatNumber(latest.close, 2);
  fields.barCount.textContent = isShortHistory
    ? `${formatInt(latest.availableBars)} / ${formatInt(latest.requiredBars)} 筆 / ${latest.historyModeText} / 完整度 ${formatNumber(latest.historyCompletenessPct, 0)}%`
    : `${formatInt(data.barsFetched)} 筆 / ${data.source}`;

  const lower = formatNumber(latest.lower, 2);
  const middle = formatNumber(latest.middle, 2);
  const upper = formatNumber(latest.upper, 2);
  if (latest.lower === null || latest.middle === null || latest.upper === null) {
    fields.bandPosition.textContent = isShortHistory ? "短歷史" : "資料不足";
  } else if (latest.close <= latest.lower) {
    fields.bandPosition.textContent = "低於下軌";
  } else if (latest.close >= latest.upper) {
    fields.bandPosition.textContent = "高於上軌";
  } else {
    fields.bandPosition.textContent = "區間內";
  }
  fields.bandValues.textContent = `L ${lower} / M ${middle} / U ${upper}`;

  fields.volumeRatio.textContent = latest.volumeRatio === null ? "--" : `${formatNumber(latest.volumeRatio, 2)}x`;
  fields.volumeValues.textContent = isShortHistory
    ? `${formatInt(latest.volume)} / 成交金額 ${formatCompact(latest.turnover)}`
    : `${formatInt(latest.volume)} / 均量 ${formatInt(latest.volumeMa)}`;
  fields.volumePriceSignal.textContent = latest.volumePriceSignalText || "--";
  fields.turnoverValues.textContent = isShortHistory
    ? `成交金額 ${formatCompact(latest.turnover)} / 尚無60日均值 / ${latest.historyRequirementText || "--"}`
    : `成交金額 ${formatCompact(latest.turnover)} / 60日均 ${formatCompact(latest.turnoverMa)} / ${latest.liquidityText || "--"}`;
  fields.volumePriceReason.textContent = `${latest.volumePriceReason || ""} ${latest.liquidityReason || ""}`.trim();
  fields.kdValues.textContent = `K ${formatNumber(latest.kdK, 1)} / D ${formatNumber(latest.kdD, 1)}`;
  fields.kdSignal.textContent = `${latest.kdBiasText}: ${kdDetail(latest)} / ${latest.kdZoneText || "--"}`;
  fields.kdReason.textContent = kdReason(latest);
  fields.rsiValue.textContent = formatNumber(latest.rsi, 1);
  fields.rsiSignal.textContent = `${latest.rsiBiasText}: ${latest.rsiSignalText}`;
  fields.rsiReason.textContent = latest.rsiReason;
  fields.macdValues.textContent = `M ${formatNumber(latest.macd, 2)} / S ${formatNumber(latest.macdSignalLine, 2)}`;
  fields.macdSignalSummary.textContent = `${latest.macdBiasText}: ${latest.macdSignalText}`;
  fields.macdReason.textContent = latest.macdReason;
  fields.trendValues.textContent = `${latest.trendBiasText}: ${latest.trendSignalText}`;
  fields.structureValues.textContent = `MA20 ${formatNumber(latest.maShort, 2)} / MA60 ${formatNumber(latest.maLong, 2)} / 支撐 ${formatNumber(latest.support, 2)} / 壓力 ${formatNumber(latest.resistance, 2)}`;
  fields.trendReason.textContent = latest.trendReason;
  fields.boxSignal.textContent = latest.boxSignalText || "--";
  fields.boxValues.textContent = `箱頂 ${formatNumber(latest.boxUpper, 2)} / 箱底 ${formatNumber(latest.boxLower, 2)} / 寬 ${formatNumber(latest.boxWidthPct, 1)}% / 品質 ${formatInt(latest.boxQualityScore)}`;
  fields.boxReason.textContent = latest.boxReason || "--";
  fields.dowSignal.textContent = latest.dowSignalText || "--";
  fields.dowValues.textContent = `${latest.dowPrimaryTrendText || "--"} / ${latest.dowSecondaryTrendText || "--"} / ${latest.dowPhaseText || "--"} / ${formatInt(latest.dowScore)}`;
  fields.dowReason.textContent = latest.dowReason || "--";
  fields.reliabilityScore.textContent = `${formatInt(latest.reliabilityScore)} / 100`;
  fields.reliabilitySignal.textContent = `${latest.reliabilityText}: ${latest.consensusText}`;
  fields.reliabilityReason.textContent = latest.reliabilityReason;
  fields.planAction.textContent = latest.planActionText;
  fields.planLevels.textContent = `入場 ${formatNumber(latest.entryTrigger, 2)} / 停損 ${formatNumber(latest.stopLevel, 2)} / 目標 ${formatNumber(latest.targetLevel, 2)}`;
  fields.planReason.textContent = `${latest.setupText}。${latest.invalidationText}。${latest.planReason}`;
  fields.lastUpdated.textContent = new Date(data.generatedAt).toLocaleString();
  fields.riskNote.textContent = data.riskNote;
}

function renderRows(rows) {
  rowsBody.innerHTML = "";
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const klass = actionClass(row.action);
    tr.innerHTML = `
      <td>${row.date}</td>
      <td>${formatNumber(row.close, 2)}</td>
      <td>${formatNumber(row.lower, 2)}</td>
      <td>${formatNumber(row.middle, 2)}</td>
      <td>${formatNumber(row.upper, 2)}</td>
      <td>${row.volumeRatio === null ? "--" : `${formatNumber(row.volumeRatio, 2)}x`}</td>
      <td>${row.volumePriceSignalText}</td>
      <td>${formatCompact(row.turnoverMa)}</td>
      <td>${row.liquidityText}</td>
      <td>${formatNumber(row.kdK, 1)}</td>
      <td>${formatNumber(row.kdD, 1)}</td>
      <td>${formatNumber(row.kdJ, 1)}</td>
      <td>${formatNumber(row.rsi, 1)}</td>
      <td>${formatNumber(row.macd, 2)}</td>
      <td>${formatNumber(row.macdHistogram, 2)}</td>
      <td>${formatNumber(row.maShort, 2)}</td>
      <td>${formatNumber(row.maLong, 2)}</td>
      <td>${formatNumber(row.support, 2)}</td>
      <td>${formatNumber(row.resistance, 2)}</td>
      <td><span class="tag ${actionClass(row.trendBias)}">${row.trendSignalText}</span></td>
      <td>${row.boxSignalText}</td>
      <td>${formatNumber(row.boxUpper, 2)}</td>
      <td>${formatNumber(row.boxLower, 2)}</td>
      <td>${row.dowSignalText}</td>
      <td>${row.dowPhaseText}</td>
      <td>${formatNumber(row.entryTrigger, 2)}</td>
      <td>${formatNumber(row.stopLevel, 2)}</td>
      <td>${formatNumber(row.targetLevel, 2)}</td>
      <td><span class="tag ${klass}">${row.actionText}</span></td>
      <td>${row.signalText}</td>
      <td><span class="tag ${actionClass(row.kdBias)}">${kdDetail(row)}</span></td>
      <td><span class="tag ${actionClass(row.rsiBias)}">${row.rsiSignalText}</span></td>
      <td><span class="tag ${actionClass(row.macdBias)}">${row.macdSignalText}</span></td>
      <td>${row.reliabilityScore} / ${row.reliabilityText}</td>
    `;
    rowsBody.appendChild(tr);
  });
}

function renderScreenRows(data) {
  screenRowsBody.innerHTML = "";
  const scanned = data.symbolsScanned ?? data.rows.length;
  const total = data.totalUniverse ?? scanned;
  const skipped = data.errors?.length ? ` / 略過 ${formatInt(data.errors.length)}` : "";
  fields.screenUpdated.textContent = `${new Date(data.generatedAt).toLocaleString()} / 掃描 ${formatInt(scanned)} / 股票池 ${formatInt(total)} 檔${skipped}`;
  if (!data.rows.length) {
    screenRowsBody.innerHTML = `<tr><td colspan="17" class="empty-cell">沒有符合條件的結果</td></tr>`;
    return;
  }
  data.rows.forEach((row, index) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${index + 1}</td>
      <td>${row.symbol}</td>
      <td>${row.date}</td>
      <td>${formatNumber(row.close, 2)}</td>
      <td>${formatInt(row.strategyScore)}</td>
      <td>${row.strategyMatchText}</td>
      <td>${row.reliabilityScore} / ${row.reliabilityText}</td>
      <td>${row.consensusText}</td>
      <td>${row.planActionText}</td>
      <td>${row.volumePriceSignalText}</td>
      <td>${row.liquidityText}</td>
      <td>${row.boxSignalText} / ${formatInt(row.boxQualityScore)}</td>
      <td>${row.dowSignalText} / ${formatInt(row.dowScore)}</td>
      <td>${formatNumber(row.rsi, 1)} / ${row.rsiSignalText}</td>
      <td>${formatNumber(row.macd, 2)} / ${row.macdSignalText}</td>
      <td>${row.trendSignalText}</td>
      <td>${row.strategyReason}</td>
    `;
    screenRowsBody.appendChild(tr);
  });
}

function renderError(message) {
  rowsBody.innerHTML = `<tr><td colspan="34" class="empty-cell">${message}</td></tr>`;
  fields.latestDate.textContent = "--";
  fields.latestSignal.textContent = "錯誤";
  fields.latestAction.textContent = "--";
  fields.latestAction.className = "";
  fields.latestReason.textContent = message;
  fields.kdValues.textContent = "--";
  fields.kdSignal.textContent = "--";
  fields.kdReason.textContent = "--";
  fields.rsiValue.textContent = "--";
  fields.rsiSignal.textContent = "--";
  fields.rsiReason.textContent = "--";
  fields.macdValues.textContent = "--";
  fields.macdSignalSummary.textContent = "--";
  fields.macdReason.textContent = "--";
  fields.volumePriceSignal.textContent = "--";
  fields.turnoverValues.textContent = "--";
  fields.volumePriceReason.textContent = "--";
  fields.trendValues.textContent = "--";
  fields.structureValues.textContent = "--";
  fields.trendReason.textContent = "--";
  fields.boxSignal.textContent = "--";
  fields.boxValues.textContent = "--";
  fields.boxReason.textContent = "--";
  fields.dowSignal.textContent = "--";
  fields.dowValues.textContent = "--";
  fields.dowReason.textContent = "--";
  fields.reliabilityScore.textContent = "--";
  fields.reliabilitySignal.textContent = "--";
  fields.reliabilityReason.textContent = "--";
  fields.planAction.textContent = "--";
  fields.planLevels.textContent = "--";
  fields.planReason.textContent = "--";
}

async function refresh() {
  setStatus("載入", "loading");
  refreshButton.disabled = true;
  try {
    const response = await fetch(`/api/analyze?${paramsFromForm().toString()}`, {
      headers: { Accept: "application/json" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    renderLatest(data);
    renderRows(data.rows);
    setStatus("已更新", "ok");
  } catch (error) {
    renderError(error.message);
    setStatus("錯誤", "error");
  } finally {
    refreshButton.disabled = false;
  }
}

async function runScreen() {
  screenButton.disabled = true;
  screenRowsBody.innerHTML = `<tr><td colspan="17" class="empty-cell">掃描中...</td></tr>`;
  try {
    const response = await fetch(`/api/screen?${screenParamsFromForm().toString()}`, {
      headers: { Accept: "application/json" },
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Request failed");
    renderScreenRows(data);
  } catch (error) {
    screenRowsBody.innerHTML = `<tr><td colspan="17" class="empty-cell">${error.message}</td></tr>`;
  } finally {
    screenButton.disabled = false;
  }
}

function syncTimer() {
  if (refreshTimer) {
    window.clearInterval(refreshTimer);
    refreshTimer = null;
  }
  if (autoRefresh.checked) {
    refreshTimer = window.setInterval(refresh, 60000);
  }
}

function syncScreenUniverse() {
  const presetUniverse = fields.screenUniverse.value !== "custom";
  fields.screenSymbols.disabled = presetUniverse;
  fields.screenSymbols.title = presetUniverse ? "已改用官方股票池清單" : "";
}

function applyInitialQuery() {
  const params = new URLSearchParams(window.location.search);
  const symbol = params.get("symbol");
  if (symbol && fields.symbol) {
    fields.symbol.value = symbol.trim().toUpperCase();
  }
}

function registerServiceWorker() {
  if (!("serviceWorker" in navigator)) return;
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // The app still works as a normal website if installation support is unavailable.
    });
  });
}

form.addEventListener("submit", (event) => {
  event.preventDefault();
  refresh();
});

screenForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runScreen();
});

autoRefresh.addEventListener("change", syncTimer);
fields.screenUniverse.addEventListener("change", syncScreenUniverse);
applyInitialQuery();
syncScreenUniverse();
registerServiceWorker();
refresh();
