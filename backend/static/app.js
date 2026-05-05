let dashboardData = null;
let remainingSeconds = 0;
let timerHandle = null;
let topOfHourHandle = null;
let charts = {};

const $ = (id) => document.getElementById(id);

function fmt(n, digits = 1) {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(digits);
}

function fmtDate(s) {
  if (!s) return "--";
  const d = new Date(s);
  return d.toLocaleString("it-IT", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function fmtTimeLeft(sec) {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}

function startTimer() {
  if (timerHandle) clearInterval(timerHandle);
  $("validTimer").textContent = fmtTimeLeft(remainingSeconds);
  timerHandle = setInterval(() => {
    remainingSeconds -= 1;
    $("validTimer").textContent = fmtTimeLeft(remainingSeconds);
    if (remainingSeconds <= 0) loadDashboard();
  }, 1000);
}

function scheduleTopOfHourRefresh() {
  if (topOfHourHandle) clearTimeout(topOfHourHandle);
  const now = new Date();
  const next = new Date(now);
  next.setHours(now.getHours() + 1, 0, 5, 0);
  const delay = Math.max(10_000, next.getTime() - now.getTime());
  topOfHourHandle = setTimeout(() => {
    loadDashboard().catch(console.error);
    scheduleTopOfHourRefresh();
  }, delay);
}

async function loadDashboard() {
  const res = await fetch(`/api/dashboard?_=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Errore backend");
  dashboardData = await res.json();
  updateUI(dashboardData);
}

function updateUI(data) {
  const d = data.decision;
  remainingSeconds = d.remaining_seconds;
  $("recommendedSetpoint").textContent = fmt(d.recommended_setpoint_c, 1);
  $("currentSetpoint").textContent = fmt(d.current_setpoint_c, 1);
  $("nominalSetpoint").textContent = fmt(d.nominal_setpoint_c, 1);
  $("mode").textContent = d.mode.toUpperCase();
  $("confidence").textContent = d.confidence.toUpperCase();
  $("validWindow").textContent = `${fmtDate(d.valid_from)} → ${fmtDate(d.valid_until)}`;
  $("coolingSaving").textContent = fmt(d.energy.estimated_saving_next_4h_kwh, 1);
  $("heatingSaving").textContent = fmt(d.gas.estimated_saving_next_4h_smc, 1);
  $("currentTemp").textContent = `${fmt(d.current_temperature_c, 1)} °C`;
  $("temperatureSource").textContent = `Sorgente temperatura: ${d.temperature_source}`;
  $("reason").textContent = d.reason;
  if (d.warning) {
    $("warning").textContent = d.warning;
    $("warning").classList.remove("hidden");
  } else {
    $("warning").classList.add("hidden");
  }
  $("scoreEnergy").textContent = fmt(d.scores.energy_score, 3);
  $("scoreComfort").textContent = fmt(d.scores.comfort_penalty, 3);
  $("scoreStability").textContent = fmt(d.scores.stability_penalty, 3);
  $("scoreProcess").textContent = fmt(d.scores.process_penalty, 3);
  $("scoreEdge").textContent = fmt(d.scores.edge_penalty, 3);
  $("scoreGlobal").textContent = fmt(d.scores.global_score, 3);

  renderCharts(data);
  renderCandidateTable(data);
  startTimer();
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

function upsertChart(id, config) {
  if (charts[id]) charts[id].destroy();
  charts[id] = new Chart($(id), config);
}

function renderCharts(data) {
  const d = data.decision;
  const forecast = d.temperature_forecast_6h || [];
  const tLabels = [fmtDate(d.timestamp), ...forecast.map(p => fmtDate(p.time))];

  const actualInternal = [d.current_temperature_c, ...forecast.map(() => null)];
  const predictedInternal = [d.current_temperature_c, ...forecast.map(p => p.predicted_c)];
  const externalData = [forecast.length ? forecast[0].external_c : null, ...forecast.map(p => p.external_c)];
  const spData = tLabels.map(() => d.recommended_setpoint_c);

  upsertChart("temperatureChart", {
    type: "line",
    data: {
      labels: tLabels,
      datasets: [
        {
          label: "Temperatura interna attuale",
          data: actualInternal,
          borderColor: "#1f5eff",
          backgroundColor: "#1f5eff",
          pointRadius: 5,
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
          label: "Setpoint consigliato",
          data: spData,
          borderColor: "#126f48",
          backgroundColor: "#126f48",
          borderDash: [6, 4],
          pointRadius: 0,
          borderWidth: 2
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

  const energy = data.energy_series || [];
  upsertChart("chillerChart", {
    type: "bar",
    data: {
      labels: energy.map(p => fmtDate(p.time)),
      datasets: [{
        label: "Frigo kWh/h",
        data: energy.map(p => p.chiller_kwh_h),
        backgroundColor: "#1f5eff"
      }]
    },
    options: chartOptions("kWh/h")
  });

  upsertChart("gasChart", {
    type: "bar",
    data: {
      labels: energy.map(p => fmtDate(p.time)),
      datasets: [{
        label: "Gas Smc/h",
        data: energy.map(p => p.gas_smc_h),
        backgroundColor: "#9a5a00"
      }]
    },
    options: chartOptions("Smc/h")
  });
}

function renderCandidateTable(data) {
  const rec = data.decision.recommended_setpoint_c;
  const rows = data.candidate_evaluations || [];
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

$("refreshBtn").addEventListener("click", loadDashboard);
$("desktopMode").addEventListener("click", () => setMode("desktop"));
$("mobileMode").addEventListener("click", () => setMode("mobile"));

loadDashboard().catch(err => {
  console.error(err);
  $("reason").textContent = "Impossibile contattare il backend. Verificare il servizio FastAPI.";
});

// Polling leggero per mantenere grafici e raccomandazione sincronizzati tra dispositivi.
setInterval(() => loadDashboard().catch(console.error), 60000);
scheduleTopOfHourRefresh();
