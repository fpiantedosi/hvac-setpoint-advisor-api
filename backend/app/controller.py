from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
import math

from .config import settings
from .regime import get_regime, nominal_setpoint
from .weather_service import get_weather_window, slice_weather
from .thermal_simulator import default_current_temperature, simulate_temperature, temperature_violation_score
from .energy_models import energy_models, cooling_saving_kwh, heating_saving_smc, smc_to_kwh_gas
from .schemas import (
    ConstraintBlock,
    DashboardResponse,
    Decision,
    EnergyBlock,
    GasBlock,
    HistoryPoint,
    ScoreBlock,
)


def _floor_control_window(dt: datetime) -> datetime:
    base_hour = (dt.hour // settings.control_interval_h) * settings.control_interval_h
    return dt.replace(hour=base_hour, minute=0, second=0, microsecond=0)


def _next_valid_window(now: datetime) -> tuple[datetime, datetime]:
    valid_from = _floor_control_window(now)
    valid_until = valid_from + timedelta(hours=settings.control_interval_h)
    return valid_from, valid_until


def _candidate_setpoints(regime: str, previous_setpoint: float) -> List[float]:
    if regime == "cooling":
        candidates = list(settings.cooling_candidates)
    elif regime == "heating":
        candidates = list(settings.heating_candidates)
    else:
        return [previous_setpoint]

    low = previous_setpoint - settings.max_change_per_decision_c
    high = previous_setpoint + settings.max_change_per_decision_c
    out = [round(c, 2) for c in candidates if low - 1e-9 <= c <= high + 1e-9]
    return out or [round(previous_setpoint, 2)]


def _normalize_positive(value: float, scale: float) -> float:
    if scale <= 1e-9:
        return 0.0
    return max(value / scale, 0.0)


def _comfort_penalty(candidate: float, nominal: float, regime: str) -> float:
    # Conservative quadratic penalty from nominal.
    denom = 1.25 if regime in ["cooling", "heating"] else 1.0
    return ((candidate - nominal) / denom) ** 2


def _stability_penalty(candidate: float, previous: float) -> float:
    return ((candidate - previous) / max(settings.max_change_per_decision_c, 0.1)) ** 2


def _process_penalty(candidate: float, regime: str) -> float:
    # Phase-1 preferred zone: this discourages staying too close to the operational edge.
    if regime == "cooling":
        excess = max(candidate - settings.cooling_preferred_max_c, 0.0)
        return (excess / 0.5) ** 2
    if regime == "heating":
        excess = max(settings.heating_preferred_min_c - candidate, 0.0)
        return (excess / 0.5) ** 2
    return 0.0


def _edge_penalty(candidate: float, regime: str) -> float:
    if regime == "cooling":
        edge = max(settings.cooling_candidates)
        distance = edge - candidate
        return max(1.0 - distance / 0.5, 0.0) ** 2
    if regime == "heating":
        edge = min(settings.heating_candidates)
        distance = candidate - edge
        return max(1.0 - distance / 0.5, 0.0) ** 2
    return 0.0


def _aggressive_streak_from_history(history: List[HistoryPoint], regime: str) -> int:
    if not history:
        return 0
    threshold = settings.cooling_preferred_max_c if regime == "cooling" else settings.heating_preferred_min_c
    streak = 0
    for p in reversed(history[-8:]):
        if p.regime != regime:
            break
        if regime == "cooling" and p.setpoint_c >= threshold:
            streak += 1
        elif regime == "heating" and p.setpoint_c <= threshold:
            streak += 1
        else:
            break
    return streak


def _persistence_penalty(candidate: float, regime: str, streak: int) -> float:
    if regime == "cooling" and candidate >= settings.cooling_preferred_max_c:
        return min((streak + 1) / 3.0, 2.5)
    if regime == "heating" and candidate <= settings.heating_preferred_min_c:
        return min((streak + 1) / 3.0, 2.5)
    return 0.0


def _confidence(best: Dict[str, Any], warning: Optional[str]) -> str:
    if warning:
        return "medium"
    if best["temperature_penalty"] > 0 or best["process_penalty"] > 0.5 or best["edge_penalty"] > 0:
        return "medium"
    if abs(best["candidate_setpoint_c"] - best["previous_setpoint_c"]) < 1e-9:
        return "high"
    return "medium"


def evaluate_candidate(
    slot_time: datetime,
    regime: str,
    candidate: float,
    previous_setpoint: float,
    current_temperature_c: float,
    weather_window,
    persistence_streak: int,
) -> Dict[str, Any]:
    nominal = nominal_setpoint(regime)
    weather_for_temp = slice_weather(weather_window, slot_time, settings.thermal_forecast_h)
    weather_for_energy = slice_weather(weather_window, slot_time, settings.control_interval_h)
    baseline = energy_models.predict_next_hours(slot_time, regime, weather_for_energy, settings.control_interval_h)

    temp_forecast = simulate_temperature(
        current_temperature_c,
        candidate,
        weather_for_temp,
        regime,
        settings.thermal_forecast_h,
    )
    temp_penalty_raw = temperature_violation_score(temp_forecast, regime)
    temp_penalty = min(temp_penalty_raw, 9.0)

    if regime == "cooling":
        baseline_energy = float(baseline["chiller_next_4h_kwh"])
        saving = max(cooling_saving_kwh(candidate, settings.control_interval_h), 0.0)
        optimized = max(baseline_energy - saving, 0.0)
        energy_gain_score = _normalize_positive(saving, max(baseline_energy, 1.0))
        saving_smc = None
        saving_kwh_gas = None
        unit = "kWh"
    elif regime == "heating":
        baseline_energy = float(baseline["gas_next_4h_smc"])
        saving_smc = max(heating_saving_smc(candidate, settings.control_interval_h), 0.0)
        saving = saving_smc
        optimized = max(baseline_energy - saving_smc, 0.0)
        energy_gain_score = _normalize_positive(saving_smc, max(baseline_energy, 1.0))
        saving_kwh_gas = smc_to_kwh_gas(saving_smc)
        unit = "Smc"
    else:
        baseline_energy = 0.0
        saving = 0.0
        optimized = 0.0
        energy_gain_score = 0.0
        saving_smc = None
        saving_kwh_gas = None
        unit = "none"

    comfort = _comfort_penalty(candidate, nominal, regime)
    stability = _stability_penalty(candidate, previous_setpoint)
    process = _process_penalty(candidate, regime)
    edge = _edge_penalty(candidate, regime)
    persistence = _persistence_penalty(candidate, regime, persistence_streak)

    global_score = (
        settings.weight_comfort * comfort
        + settings.weight_stability * stability
        + settings.weight_process * process
        + settings.weight_edge * edge
        + settings.weight_persistence * persistence
        + settings.weight_temperature_violation * temp_penalty
        - settings.weight_energy * energy_gain_score
    )

    return {
        "slot_time": slot_time,
        "candidate_setpoint_c": round(candidate, 2),
        "previous_setpoint_c": round(previous_setpoint, 2),
        "nominal_setpoint_c": round(nominal, 2),
        "regime": regime,
        "baseline_energy": round(baseline_energy, 2),
        "optimized_energy": round(optimized, 2),
        "estimated_saving_energy": round(saving, 2),
        "estimated_saving_smc": round(saving_smc, 2) if saving_smc is not None else None,
        "estimated_saving_kwh_gas": round(saving_kwh_gas, 2) if saving_kwh_gas is not None else None,
        "saving_unit": unit,
        "energy_score": round(energy_gain_score, 4),
        "comfort_penalty": round(comfort, 4),
        "stability_penalty": round(stability, 4),
        "process_penalty": round(process, 4),
        "edge_penalty": round(edge, 4),
        "persistence_penalty": round(persistence, 4),
        "temperature_penalty": round(temp_penalty, 4),
        "global_score": round(global_score, 4),
        "temperature_forecast": temp_forecast,
        "baseline": baseline,
    }


def _select_for_slot(
    slot_time: datetime,
    previous_setpoint: float,
    current_temperature_c: float,
    weather_window,
    history: List[HistoryPoint],
    force_regime: Optional[str] = None,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    regime = force_regime or get_regime(slot_time)
    if regime == "neutral":
        previous_setpoint = nominal_setpoint(regime)

    streak = _aggressive_streak_from_history(history, regime)
    candidates = _candidate_setpoints(regime, previous_setpoint)
    evaluations = [
        evaluate_candidate(slot_time, regime, c, previous_setpoint, current_temperature_c, weather_window, streak)
        for c in candidates
    ]
    evaluations = sorted(evaluations, key=lambda x: x["global_score"])
    best = dict(evaluations[0])

    # Conservative no-change rule.
    hold_eval = next((ev for ev in evaluations if abs(ev["candidate_setpoint_c"] - previous_setpoint) < 1e-9), None)
    if hold_eval and abs(best["candidate_setpoint_c"] - previous_setpoint) > 1e-9:
        improvement = hold_eval["global_score"] - best["global_score"]
        if improvement < settings.min_score_improvement_to_change:
            best = dict(hold_eval)
            best["hold_reason"] = "miglioramento tecnico insufficiente per modificare il setpoint"

    return best, evaluations


def _next_temperature_for_chain(selected: Dict[str, Any], current_temp: float) -> float:
    forecast = selected.get("temperature_forecast") or []
    if not forecast:
        return current_temp
    # Take the simulated temperature at the end of the 4h validity window if available.
    idx = min(settings.control_interval_h - 1, len(forecast) - 1)
    return float(forecast[idx].predicted_c)


def _reason(regime: str, recommended: float, previous: float, selected: Dict[str, Any]) -> str:
    if regime == "cooling":
        if recommended > previous:
            reason = "Setpoint aumentato con logica conservativa: riduzione richiesta frigorifera stimata e temperatura simulata entro limite."
        else:
            reason = "Setpoint mantenuto: beneficio energetico non sufficiente rispetto ai vincoli tecnici."
    elif regime == "heating":
        if recommended < previous:
            reason = "Setpoint ridotto con logica conservativa: riduzione richiesta termica stimata e temperatura simulata entro limite."
        else:
            reason = "Setpoint mantenuto: beneficio energetico non sufficiente rispetto ai vincoli tecnici."
    else:
        reason = "Regime neutro: nessuna raccomandazione energetica aggressiva."
    if selected.get("hold_reason"):
        reason += " " + str(selected["hold_reason"]) + "."
    return reason


def _build_reconstructed_history(
    now: datetime,
    valid_from: datetime,
    current_temperature_c: Optional[float],
    current_setpoint_c: Optional[float],
    force_regime: Optional[str],
) -> Tuple[List[HistoryPoint], Dict[str, Any], List[Dict[str, Any]], float, List[Any], Dict[str, Any]]:
    """
    Rebuilds recent advisory history from weather + deterministic controller.

    This is the key Render-friendly change: the dashboard remains coherent even if
    no scheduler has been running and even if local CSV files are not persistent.
    """
    lookback_h = settings.setpoint_history_lookback_h
    start = _floor_control_window(valid_from - timedelta(hours=lookback_h))
    weather_window = get_weather_window(
        now=valid_from,
        past_hours=lookback_h + settings.control_interval_h,
        forecast_hours=settings.thermal_forecast_h,
    )

    slots: List[datetime] = []
    t = start
    while t <= valid_from:
        slots.append(t)
        t += timedelta(hours=settings.control_interval_h)

    history: List[HistoryPoint] = []
    previous_setpoint: Optional[float] = None
    previous_regime: Optional[str] = None
    temp_estimate: Optional[float] = None
    current_selected: Optional[Dict[str, Any]] = None
    current_evaluations: List[Dict[str, Any]] = []

    for slot in slots:
        slot_regime = force_regime if slot == valid_from and force_regime else get_regime(slot)
        nominal = nominal_setpoint(slot_regime)

        if previous_setpoint is None or previous_regime != slot_regime:
            previous_setpoint = nominal
        if slot == valid_from and current_setpoint_c is not None:
            previous_setpoint = float(current_setpoint_c)

        if temp_estimate is None or previous_regime != slot_regime:
            temp_estimate = default_current_temperature(slot_regime)
        if slot == valid_from and current_temperature_c is not None:
            temp_estimate = float(current_temperature_c)

        selected, evaluations = _select_for_slot(
            slot,
            float(previous_setpoint),
            float(temp_estimate),
            weather_window,
            history,
            force_regime=slot_regime,
        )
        recommended = float(selected["candidate_setpoint_c"])
        reason = _reason(slot_regime, recommended, float(previous_setpoint), selected)

        history.append(
            HistoryPoint(
                time=slot,
                setpoint_c=recommended,
                regime=slot_regime,
                reason=reason,
            )
        )

        if slot == valid_from:
            current_selected = selected
            current_evaluations = evaluations

        temp_estimate = _next_temperature_for_chain(selected, float(temp_estimate))
        previous_setpoint = recommended
        previous_regime = slot_regime

    assert current_selected is not None
    current_weather = slice_weather(weather_window, valid_from, settings.weather_forecast_h)
    current_baseline = energy_models.predict_next_hours(valid_from, current_selected["regime"], current_weather, settings.control_interval_h)
    return history, current_selected, current_evaluations, float(temp_estimate), current_weather, current_baseline


def compute_dashboard(current_temperature_c: Optional[float] = None, current_setpoint_c: Optional[float] = None, force_regime: Optional[str] = None) -> DashboardResponse:
    now = datetime.now().replace(microsecond=0)
    valid_from, valid_until = _next_valid_window(now)
    remaining_seconds = max(int((valid_until - now).total_seconds()), 0)

    history, best, evaluations, _temp_chain, weather, baseline = _build_reconstructed_history(
        now=now,
        valid_from=valid_from,
        current_temperature_c=current_temperature_c,
        current_setpoint_c=current_setpoint_c,
        force_regime=force_regime,
    )

    regime = best["regime"]
    nominal = nominal_setpoint(regime)
    previous = float(best["previous_setpoint_c"])
    recommended = float(best["candidate_setpoint_c"])
    temp_source = "operator_input" if current_temperature_c is not None else "simulated"
    current_temp = float(current_temperature_c) if current_temperature_c is not None else default_current_temperature(regime)

    if regime == "cooling":
        energy = EnergyBlock(
            baseline_next_4h_kwh=round(float(baseline["chiller_next_4h_kwh"]), 2),
            optimized_next_4h_kwh=round(max(float(baseline["chiller_next_4h_kwh"]) - float(best["estimated_saving_energy"]), 0), 2),
            estimated_saving_next_4h_kwh=round(float(best["estimated_saving_energy"]), 2),
        )
        gas = GasBlock()
    elif regime == "heating":
        saving_smc = float(best["estimated_saving_smc"] or 0.0)
        gas = GasBlock(
            baseline_next_4h_smc=round(float(baseline["gas_next_4h_smc"]), 2),
            optimized_next_4h_smc=round(max(float(baseline["gas_next_4h_smc"]) - saving_smc, 0), 2),
            estimated_saving_next_4h_smc=round(saving_smc, 2),
            estimated_saving_next_4h_kwh_gas=round(smc_to_kwh_gas(saving_smc), 2),
        )
        energy = EnergyBlock()
    else:
        energy = EnergyBlock()
        gas = GasBlock()

    warning = None
    if temp_source == "simulated":
        warning = "Temperatura interna/ripresa simulata: integrare dato reale quando disponibile."

    reason = _reason(regime, recommended, previous, best)

    decision = Decision(
        timestamp=now,
        mode=regime,
        valid_from=valid_from,
        valid_until=valid_until,
        remaining_seconds=remaining_seconds,
        current_setpoint_c=round(previous, 2),
        recommended_setpoint_c=round(recommended, 2),
        nominal_setpoint_c=round(nominal, 2),
        confidence=_confidence(best, warning),
        reason=reason,
        warning=warning,
        energy=energy,
        gas=gas,
        temperature_source=temp_source,
        current_temperature_c=round(current_temp, 2),
        temperature_forecast_6h=best["temperature_forecast"],
        constraints=ConstraintBlock(
            decision_interval_h=settings.control_interval_h,
            thermal_forecast_h=settings.thermal_forecast_h,
            max_change_per_decision_c=settings.max_change_per_decision_c,
            phase1_cooling_max_c=max(settings.cooling_candidates),
            phase1_heating_min_c=min(settings.heating_candidates),
            cooling_temperature_limit_c=settings.cooling_temperature_limit_c,
            heating_temperature_limit_c=settings.heating_temperature_limit_c,
        ),
        scores=ScoreBlock(
            energy_score=float(best["energy_score"]),
            comfort_penalty=float(best["comfort_penalty"]),
            stability_penalty=float(best["stability_penalty"]),
            process_penalty=float(best["process_penalty"]),
            edge_penalty=float(best["edge_penalty"]),
            persistence_penalty=float(best["persistence_penalty"]),
            temperature_penalty=float(best["temperature_penalty"]),
            global_score=float(best["global_score"]),
        ),
    )

    candidate_public = []
    for ev in evaluations:
        candidate_public.append({k: v for k, v in ev.items() if k not in ["temperature_forecast", "baseline", "slot_time"]})

    # Keep the last 30 setpoint points for the chart.
    history_public = history[-30:]

    return DashboardResponse(
        decision=decision,
        setpoint_history=history_public,
        weather_forecast=weather,
        energy_series=baseline["series"],
        candidate_evaluations=candidate_public,
    )


def build_setpoint_sweep() -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []

    def grid(start: float, end: float, step: float) -> List[float]:
        n = int(round((end - start) / step))
        return [round(start + i * step, 2) for i in range(n + 1)]

    for tsp in grid(settings.cooling_band[0], settings.cooling_band[1], settings.setpoint_step_c):
        rows.append({
            "regime": "cooling",
            "nominal_setpoint_c": settings.setpoint_cooling_c,
            "candidate_setpoint_c": tsp,
            "delta_vs_nominal_c": round(tsp - settings.setpoint_cooling_c, 2),
            "estimated_saving_per_4h_kwh": round(cooling_saving_kwh(tsp, settings.control_interval_h), 2),
            "interpretation": "saving" if tsp > settings.setpoint_cooling_c else "extra_consumption" if tsp < settings.setpoint_cooling_c else "neutral",
        })

    for tsp in grid(settings.heating_band[0], settings.heating_band[1], settings.setpoint_step_c):
        smc = heating_saving_smc(tsp, settings.control_interval_h)
        rows.append({
            "regime": "heating",
            "nominal_setpoint_c": settings.setpoint_heating_c,
            "candidate_setpoint_c": tsp,
            "delta_vs_nominal_c": round(tsp - settings.setpoint_heating_c, 2),
            "estimated_saving_per_4h_smc": round(smc, 2),
            "estimated_saving_per_4h_kwh_gas": round(smc_to_kwh_gas(smc), 2),
            "interpretation": "saving" if tsp < settings.setpoint_heating_c else "extra_consumption" if tsp > settings.setpoint_heating_c else "neutral",
        })
    return rows
