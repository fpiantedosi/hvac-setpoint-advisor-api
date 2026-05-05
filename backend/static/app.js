let dashboardData = null;
let remainingSeconds = 0;
let timerHandle = null;
let topOfHourHandle = null;
let validityRefreshHandle = null;
let periodicRefreshHandle = null;
let expiryRetryHandle = null;
let isLoadingDashboard = false;
let lastDecisionKey = null;
let charts = {};

const $ = (id) => document.getElementById(id);

function fmt(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(digits);
}

function parseDate(s) {
  if (!s) return null;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

function fmtDate(s) {
  const d = parseDate(s);
  if (!d) return "--";
  return d.toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtTimeLeft(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function secondsUntil(dateString, fallbackSeconds = 0) {
  const d = parseDate(dateString);
  if (!d) return Math.max(0, Number(fallbackSeconds || 0));
  return Math.max(0, Math.ceil((d.getTime() - Date.now()) / 1000));
}

function decisionKey(data) {
  const d = data?.decision || {};
  return `${d.valid_from || ""}|${d.valid_until || ""}|${d.recommended_setpoint_c || ""}|${d.mode || ""}`;
}

function setLoadingState(isLoading) {
  document.body.classList.toggle("loading", isLoading);
  const btn = $("refreshBtn");
  if (btn) btn.textContent = isLoading ? "Aggiorno..." : "Aggiorna";
}

async function loadDashboard(reason = "manual") {
  if (isLoadingDashboard) return dashboardData;
  isLoadingDashboard = true;
  setLoadingState(true);
  try {
    const res = await fetch(`/api/dashboard?_=${Date.now()}&reason=${encodeURIComponent(reason)}`, {
      cache: "no-store",
      headers: { "Cache-Control": "no-cache" }
    });
    if (!res.ok) throw new Error(`Errore backend: HTTP ${res.status}`);
    const data = await res.json();
    dashboardData = data;
    updateUI(data);
    return data;
  } catch (err) {
    console.error(err);
    showRefreshWarning("Backend non raggiungibile. Ritento automaticamente.");
    throw err;
  } finally {
    isLoadingDashboard = false;
    setLoadingState(false);
  }
}

function showRefreshWarning(text) {
  const el = $("refreshState");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("hidden", !text);
}

function scheduleValidityRefresh(validUntil) {
  if (validityRefreshHandle) clearTimeout(validityRefreshHandle);
  const d = parseDate(validUntil);
  if (!d) return;
  const delay = Math.max(1000, d.getTime() - Date.now() + 3000);
  validityRefreshHandle = setTimeout(() => {
    refreshAfterExpiry();
  }, delay);
}

function refreshAfterExpiry(attempt = 1) {
  if (expiryRetryHandle) clearTimeout(expiryRetryHandle);
  showRefreshWarning("Finestra scaduta: aggiorno la raccomandazione...");
  loadDashboard("validity_expired")
    .then((data) => {
      const sec = secondsUntil(data?.decision?.valid_until, data?.decision?.remaining_seconds);
      if (sec <= 2 && attempt < 12) {
        // Se siamo esattamente sul confine della finestra, il backend/meteo può
        // rispondere ancora con lo slot precedente per pochi secondi. Ritento.
        expiryRetryHandle = setTimeout(() => refreshAfterExpiry(attempt + 1), 5000);
      } else {
        showRefreshWarning("");
      }
    })
    .catch(() => {
      if (attempt < 12) expiryRetryHandle = setTimeout(() => refreshAfterExpiry(attempt + 1), 10000);
    });
}

function startTimer(data) {
  if (timerHandle) clearInterval(timerHandle);
  const d = data.decision;
  remainingSeconds = secondsUntil(d.valid_until, d.remaining_seconds);
  $("validTimer").textContent = fmtTimeLeft(remainingSeconds);
  scheduleValidityRefresh(d.valid_until);

  timerHandle = setInterval(() => {
    remainingSeconds = secondsUntil(d.valid_until, remainingSeconds - 1);
    $("validTimer").textContent = fmtTimeLeft(remainingSeconds);
    if (remainingSeconds <= 0) {
      clearInterval(timerHandle);
      timerHandle = null;
      $("validTimer").textContent = "00:00:00";
      refreshAfterExpiry();
    }
  }, 1000);
}

function scheduleTopOfHourRefresh() {
  if (topOfHourHandle) clearTimeout(topOfHourHandle);
  const now = new Date();
  const next = new Date(now);
  next.setHours(now.getHours() + 1, 0, 5, 0);
  const delay = Math.max(10_000, next.getTime() - now.getTime());
  topOfHourHandle = setTimeout(() => {
    loadDashboard("top_of_hour").catch(console.error);
    scheduleTopOfHourRefresh();
  }, delay);
}

function startPeriodicPolling() {
  if (periodicRefreshHandle) clearInterval(periodicRefreshHandle);
  // Polling leggero: mantiene più dispositivi sincronizzati e aggiorna i grafici
  // senza attendere la chiusura/riapertura della pagina.
  periodicRefreshHandle = setInterval(() => {
    loadDashboard("periodic_poll").catch(console.error);
  }, 30000);
}

function updateUI(data) {
  const d = data.decision;
  const newKey = decisionKey(data);
  const changed = lastDecisionKey && lastDecisionKey !== newKey;
  lastDecisionKey = newKey;

  remainingSeconds = secondsUntil(d.valid_until, d.remaining_seconds);
  $("recommendedSetpoint").textContent = fmt(d.recommended_setpoint_c, 1);
  $("currentSetpoint").textContent = fmt(d.current_setpoint_c, 1);
  $("nominalSetpoint").textContent = fmt(d.nominal_setpoint_c, 1);
  $("mode").textContent = String(d.mode || "--").toUpperCase();
  $("confidence").textContent = String(d.confidence || "--").toUpperCase();
  $("validWindow").textContent = `${fmtDate(d.valid_from)} → ${fmtDate(d.valid_until)}`;
  $("coolingSaving").textContent = fmt(d.energy?.estimated_saving_next_4h_kwh, 1);
  $("heatingSaving").textContent = fmt(d.gas?.estimated_saving_next_4h_smc, 1);
  $("currentTemp").textContent = `${fmt(d.current_temperature_c, 1)} °C`;
  $("temperatureSource").textContent = `Sorgente temperatura: ${d.temperature_source}`;

  if (changed) showRefreshWarning("Nuovo slot di controllo caricato.");
  else showRefreshWarning("");

  renderCharts(data);
  renderCandidateTable(data);
  startTimer(data);
}

function chartOptions(yTitle) {
  return {
    responsive: true,
    animation: false,
    maintainAspectRatio: false,
    plugins: { legend: { display: true, labels: { boxWidth: 14 } } },
    scales: {
      x: { ticks: { maxRotation: 0, autoSkip: true } },
      y: { title: { display: true, text: yTitle } }
    }
  };
}

function dualAxisEnergyOptions() {
  return {
    responsive: true,
    animation: false,
    maintainAspectRatio: false,
    interaction: { mode: "index", intersect: false },
    plugins: { legend: { display: true, labels: { boxWidth: 14 } } },
    scales: {
      x: { ticks: { maxRotation: 0, autoSkip: true } },
      y: {
        type: "linear",
        position: "left",
        title: { display: true, text: "Gruppi frigo [kWh/h]" },
        grid: { drawOnChartArea: true }
      },
      y1: {
        type: "linear",
        position: "right",
        title: { display: true, text: "Caldaie [Smc/h]" },
        grid: { drawOnChartArea: false }
      }
    }
  };
}

function upsertChart(id, config) {
  if (charts[id]) {
    charts[id].data = config.data;
    charts[id].options = config.options;
    charts[id].update("none");
    return;
  }
  charts[id] = new Chart($(id), config);
}


function candidateSavingValue(row, mode) {
  if (!row) return 0;
  if (mode === "cooling") return Number(row.estimated_saving_energy || 0);
  if (mode === "heating") return Number(row.estimated_saving_smc ?? row.estimated_saving_energy ?? 0);
  return Number(row.estimated_saving_energy || 0);
}

function candidateSavingUnit(mode) {
  if (mode === "cooling") return "kWh elettrici / 4h";
  if (mode === "heating") return "Smc gas / 4h";
  return "unità / 4h";
}

function renderCharts(data) {
  const d = data.decision;
  const forecast = d.temperature_forecast_6h || [];
  const tLabels = [fmtDate(d.timestamp), ...forecast.map(p => fmtDate(p.time))];

  const actualInternal = [d.current_temperature_c, ...forecast.map(() => null)];
  const predictedInternal = [d.current_temperature_c, ...forecast.map(p => p.predicted_c)];
  const externalData = [forecast.length ? forecast[0].external_c : null, ...forecast.map(p => p.external_c)];

  upsertChart("temperatureChart", {
    type: "line",
    data: {
      labels: tLabels,
      datasets: [
        {
          label: "Temperatura interna attuale/simulata ora",
          data: actualInternal,
          borderColor: "#1f5eff",
          backgroundColor: "#1f5eff",
          pointRadius: 6,
          showLine: false
        },
        {
          label: "Temperatura interna/ripresa prevista 6h",
          data: predictedInternal,
          borderColor: "#d92d20",
          backgroundColor: "#d92d20",
          tension: .25,
          pointRadius: 3,
          borderWidth: 3
        },
        {
          label: "Temperatura esterna prevista",
          data: externalData,
          borderColor: "#637083",
          backgroundColor: "#637083",
          tension: .25,
          pointRadius: 2,
          borderDash: [2, 3]
        }
      ]
    },
    options: chartOptions("°C")
  });

  const hist = data.setpoint_history || [];
  upsertChart("setpointChart", {
    type: "line",
    data: {
      labels: hist.map(p => fmtDate(p.time)),
      datasets: [{
        label: "Setpoint proposto ogni 4h",
        data: hist.map(p => p.setpoint_c),
        stepped: true,
        tension: 0,
        pointRadius: 3,
        borderColor: "#1f5eff",
        backgroundColor: "#1f5eff"
      }]
    },
    options: chartOptions("°C")
  });

  const candidates = data.candidate_evaluations || [];
  const mode = d.mode || "";
  upsertChart("savingChart", {
    type: "bar",
    data: {
      labels: candidates.map(r => `${fmt(r.candidate_setpoint_c, 1)} °C`),
      datasets: [{
        label: `Saving stimato (${candidateSavingUnit(mode)})`,
        data: candidates.map(r => candidateSavingValue(r, mode)),
        backgroundColor: candidates.map(r => Math.abs(Number(r.candidate_setpoint_c) - Number(d.recommended_setpoint_c)) < 0.01 ? "rgba(217, 45, 32, 0.55)" : "rgba(31, 94, 255, 0.28)"),
        borderColor: candidates.map(r => Math.abs(Number(r.candidate_setpoint_c) - Number(d.recommended_setpoint_c)) < 0.01 ? "#d92d20" : "#1f5eff"),
        borderWidth: 1
      }]
    },
    options: chartOptions(candidateSavingUnit(mode))
  });

  const energy = data.energy_series || [];
  upsertChart("energyChart", {
    type: "bar",
    data: {
      labels: energy.map(p => fmtDate(p.time)),
      datasets: [
        {
          type: "bar",
          label: "Gruppi frigo [kWh/h]",
          data: energy.map(p => p.chiller_kwh_h),
          backgroundColor: "rgba(31, 94, 255, 0.38)",
          borderColor: "#1f5eff",
          borderWidth: 1,
          yAxisID: "y"
        },
        {
          type: "line",
          label: "Caldaie [Smc/h]",
          data: energy.map(p => p.gas_smc_h),
          borderColor: "#9a5a00",
          backgroundColor: "#9a5a00",
          pointRadius: 3,
          tension: .25,
          yAxisID: "y1"
        }
      ]
    },
    options: dualAxisEnergyOptions()
  });
}

function renderCandidateTable(data) {
  const rec = data.decision.recommended_setpoint_c;
  const rows = data.candidate_evaluations || [];
  if (!rows.length) {
    $("candidateTable").innerHTML = `<tr><td colspan="8">Nessun candidato disponibile.</td></tr>`;
    return;
  }
  const maxEnergy = Math.max(...rows.map(r => Number(r.energy_score || 0)));

  const html = rows.map(r => {
    const selected = Math.abs(r.candidate_setpoint_c - rec) < 0.01;
    const energyBest = Math.abs(Number(r.energy_score || 0) - maxEnergy) < 0.00001 && maxEnergy > 0;
    const cls = selected ? "selected" : (energyBest ? "energy-best" : "");
    const technicalPenalty = Number(r.comfort_penalty || 0) + Number(r.stability_penalty || 0) + Number(r.process_penalty || 0) + Number(r.edge_penalty || 0) + Number(r.persistence_penalty || 0) + Number(r.temperature_penalty || 0);

    let outcome = "";
    if (selected) outcome = "Raccomandato";
    else if (energyBest) outcome = "Migliore energia";

    const coolingSaving = r.saving_unit === "kWh" ? `${fmt(r.estimated_saving_energy, 1)} kWh` : "--";
    const heatingSaving = r.saving_unit === "Smc" ? `${fmt(r.estimated_saving_smc ?? r.estimated_saving_energy, 2)} Smc` : "--";
    const gasKwh = r.estimated_saving_kwh_gas !== null && r.estimated_saving_kwh_gas !== undefined ? `${fmt(r.estimated_saving_kwh_gas, 1)} kWh` : "--";

    return `<tr class="${cls}">
      <td><b>${fmt(r.candidate_setpoint_c, 1)} °C</b></td>
      <td>${coolingSaving}</td>
      <td>${heatingSaving}</td>
      <td>${gasKwh}</td>
      <td>${fmt(r.energy_score, 3)}</td>
      <td>${fmt(technicalPenalty, 3)}</td>
      <td><b>${fmt(r.global_score, 3)}</b></td>
      <td>${outcome}</td>
    </tr>`;
  }).join("");
  $("candidateTable").innerHTML = html;
}

function setMode(mode) {
  const app = $("app");
  if (mode === "mobile") {
    app.classList.remove("desktop-layout");
    app.classList.add("mobile-layout");
    $("mobileMode").classList.add("active");
    $("desktopMode").classList.remove("active");
  } else {
    app.classList.add("desktop-layout");
    app.classList.remove("mobile-layout");
    $("desktopMode").classList.add("active");
    $("mobileMode").classList.remove("active");
  }
  setTimeout(() => dashboardData && renderCharts(dashboardData), 100);
}

$("refreshBtn").addEventListener("click", () => loadDashboard("manual_refresh").catch(console.error));
$("desktopMode").addEventListener("click", () => setMode("desktop"));
$("mobileMode").addEventListener("click", () => setMode("mobile"));

loadDashboard("initial_load").catch(err => {
  console.error(err);
  alert("Impossibile contattare il backend. Verificare il servizio FastAPI.");
});

startPeriodicPolling();
scheduleTopOfHourRefresh();
