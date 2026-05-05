let dashboardData = null;
let remainingSeconds = 0;
let timerHandle = null;
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

async function loadDashboard() {
  const res = await fetch("/api/dashboard");
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
  const tLabels = ["Ora", ...d.temperature_forecast_6h.map(p => fmtDate(p.time))];
  const tData = [d.current_temperature_c, ...d.temperature_forecast_6h.map(p => p.predicted_c)];
  const extData = [null, ...d.temperature_forecast_6h.map(p => p.external_c)];
  const spData = tLabels.map(() => d.recommended_setpoint_c);

  upsertChart("temperatureChart", {
    type: "line",
    data: {
      labels: tLabels,
      datasets: [
        { label: "Temperatura interna/ripresa", data: tData, tension: .3, pointRadius: 3 },
        { label: "Setpoint consigliato", data: spData, borderDash: [6, 4], pointRadius: 0 },
        { label: "Temperatura esterna", data: extData, tension: .3, pointRadius: 2 }
      ]
    },
    options: chartOptions("°C")
  });

  const hist = data.setpoint_history || [];
  upsertChart("setpointChart", {
    type: "line",
    data: {
      labels: hist.map(p => fmtDate(p.time)),
      datasets: [{ label: "Setpoint proposto", data: hist.map(p => p.setpoint_c), stepped: true, tension: 0, pointRadius: 3 }]
    },
    options: chartOptions("°C")
  });

  const energy = data.energy_series || [];
  upsertChart("energyChart", {
    type: "bar",
    data: {
      labels: energy.map(p => fmtDate(p.time)),
      datasets: [
        { label: "Frigo kWh/h", data: energy.map(p => p.chiller_kwh_h) },
        { label: "Gas Smc/h", data: energy.map(p => p.gas_smc_h) }
      ]
    },
    options: chartOptions("Energia / consumo")
  });
}

function renderCandidateTable(data) {
  const rec = data.decision.recommended_setpoint_c;
  const rows = data.candidate_evaluations || [];
  const html = rows.map(r => {
    const selected = Math.abs(r.candidate_setpoint_c - rec) < 0.01 ? "selected" : "";
    return `<tr class="${selected}">
      <td><b>${fmt(r.candidate_setpoint_c, 1)} °C</b></td>
      <td>${fmt(r.estimated_saving_energy, 2)}</td>
      <td>${r.saving_unit}</td>
      <td>${fmt(r.energy_score, 3)}</td>
      <td>${fmt(r.comfort_penalty, 3)}</td>
      <td>${fmt(r.stability_penalty, 3)}</td>
      <td>${fmt(r.process_penalty, 3)}</td>
      <td>${fmt(r.temperature_penalty, 3)}</td>
      <td><b>${fmt(r.global_score, 3)}</b></td>
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
setInterval(() => loadDashboard().catch(console.error), 60000);
