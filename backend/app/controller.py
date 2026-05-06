from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
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

def _ensure_setting(name: str, value):
    """Backward-compatible defaults when Render deploys a controller newer than config.py."""
    if not hasattr(settings, name):
        try:
            object.__setattr__(settings, name, value)
        except Exception:
            pass

# Safety defaults. They prevent /api/dashboard from failing if config.py in the
# deployed repo is older than controller.py. Replace config.py anyway.
for _name, _value in {
    "cooling_load_low_4h_kwh": 2200.0,
    "cooling_load_high_4h_kwh": 6200.0,
    "heating_load_low_4h_smc": 350.0,
    "heating_load_high_4h_smc": 1300.0,
    # Variable/cascade-aware saving representation. These defaults are used only
    # if config.py has not yet been updated. Values are deliberately conservative:
    # they introduce load-dependent variability and a modest cascade opportunity
    # without claiming a full fixed chiller is always avoided.
    "cooling_variable_saving_min_factor": 0.55,
    "cooling_variable_saving_max_factor": 1.15,
    "cooling_stage_bonus_max_fraction": 0.45,
    "cooling_stage_thresholds_4h_kwh": (2200.0, 3800.0, 5400.0, 7000.0, 8600.0),
    "cooling_stage_opportunity_window_kwh": 650.0,
    "heating_variable_saving_min_factor": 0.65,
    "heating_variable_saving_max_factor": 1.05,
    "weight_energy": 1.40,
    "weight_comfort": 0.60,
    "weight_stability": 0.10,
    "weight_process": 1.80,
    "weight_edge": 2.40,
    "weight_persistence": 1.80,
    "weight_temperature_violation": 9.0,
    "min_score_improvement_to_change": 0.005,
    "cooling_preferred_max_c": 25.0,
    "heating_preferred_min_c": 21.0,
    "setpoint_history_lookback_h": 72,
    "thermal_forecast_h": 6,
    "weather_forecast_h": 6,
    "max_change_per_decision_c": 0.5,
    "control_interval_h": 4,
    "cooling_temperature_limit_c": 26.0,
    "heating_temperature_limit_c": 20.0,
    "cooling_band": (21.0, 27.0),
    "heating_band": (19.0, 25.0),
    "setpoint_step_c": 0.5,
    "cooling_candidates": (24.0, 24.5, 25.0, 25.5),
    "heating_candidates": (22.0, 21.5, 21.0, 20.5),
}.items():
    _ensure_setting(_name, _value)


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




def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _load_intensity(regime: str, baseline_energy: float) -> float:
    """
    Dimensionless load factor in [0.15, 1.15].

    The setpoint advisor must not behave as if every 4h slot had the same
    opportunity for savings. In a stateless deployment the setpoint history is
    reconstructed on demand; if the energy term is independent of the predicted
    load, the chart can become artificially flat. This factor makes the advisory
    more conservative during low-load slots and more willing to move by 0.5 °C
    during high-load slots, while still respecting comfort/process/stability
    penalties.
    """
    if regime == "cooling":
        low = settings.cooling_load_low_4h_kwh
        high = settings.cooling_load_high_4h_kwh
    elif regime == "heating":
        low = settings.heating_load_low_4h_smc
        high = settings.heating_load_high_4h_smc
    else:
        return 0.0
    if high <= low:
        return 1.0
    x = (float(baseline_energy) - low) / (high - low)
    # 0.15 avoids a dead controller in moderate conditions; 1.15 allows
    # the history to show stronger action under clear peak-load conditions.
    return round(0.15 + 1.0 * _clamp(x), 4)


def _max_phase1_saving(regime: str) -> float:
    """
    Normalization scale for the energy score.

    Important: the previous implementation normalized the saving against the
    entire chiller/gas baseline. Since the controllable 11 UTA are only one
    component of the central plant load, that made the energy score almost
    invisible and the controller tended to hold the nominal setpoint. Here the
    score is normalized against the maximum theoretical saving available inside
    the conservative phase-1 candidate set.
    """
    if regime == "cooling":
        max_candidate = max(settings.cooling_candidates)
        return max(cooling_saving_kwh(max_candidate, settings.control_interval_h), 1.0)
    if regime == "heating":
        min_candidate = min(settings.heating_candidates)
        return max(heating_saving_smc(min_candidate, settings.control_interval_h), 1.0)
    return 1.0



def _cooling_cascade_opportunity(baseline_energy_4h: float, active_count_avg: float | None = None) -> float:
    """Opportunity score in [0, 1] for avoiding/shortening a cascade stage.

    The first chiller is assumed to be variable/part-load. Additional stages are
    treated as quasi-fixed blocks: the closer the 4h baseline is just above an
    estimated staging threshold, the higher the opportunity that a small load
    reduction shortens or avoids one staged unit. This is a representation model
    for the advisor, not a certified plant-sequencing reconstruction.
    """
    thresholds = tuple(getattr(settings, "cooling_stage_thresholds_4h_kwh", (2200.0, 3800.0, 5400.0, 7000.0, 8600.0)))
    window = float(getattr(settings, "cooling_stage_opportunity_window_kwh", 650.0))
    if window <= 1e-9:
        return 0.0

    opportunity = 0.0
    for thr in thresholds:
        # Only if we are above a threshold can a reduction plausibly shorten or
        # avoid the stage. Opportunity fades as the load moves far above it.
        if baseline_energy_4h >= thr:
            distance = baseline_energy_4h - thr
            opportunity = max(opportunity, 1.0 - min(distance / window, 1.0))

    # Active count, when available, is an additional sanity check: if the profile
    # says only one group is active, cascade-stage opportunity should be limited.
    if active_count_avg is not None:
        try:
            ac = float(active_count_avg)
            if ac < 1.5:
                opportunity *= 0.35
            elif ac < 2.2:
                opportunity *= 0.75
        except Exception:
            pass
    return round(_clamp(opportunity), 4)


def _cooling_dynamic_saving_kwh(candidate: float, hours: int, baseline_energy_4h: float, baseline: Dict[str, Any]) -> Tuple[float, Dict[str, Any]]:
    """Cooling saving with variable first-stage and cascade-aware modulation.

    Previous implementation returned a constant physical value for a given
    setpoint delta: K * delta / EER * hours. That is safe but visually and
    operationally too flat. This version keeps the same physical core and adds:
      1) part-load modulation for the variable first stage;
      2) a modest cascade opportunity factor near staging thresholds.
    """
    base = max(cooling_saving_kwh(candidate, hours), 0.0)
    if base <= 0:
        return 0.0, {
            "saving_model": "cascade_variable",
            "base_physical_saving_kwh": 0.0,
            "load_intensity": _load_intensity("cooling", baseline_energy_4h),
            "variable_stage_factor": 0.0,
            "cascade_opportunity": 0.0,
            "cascade_bonus_kwh": 0.0,
        }

    intensity = _load_intensity("cooling", baseline_energy_4h)
    f_min = float(getattr(settings, "cooling_variable_saving_min_factor", 0.55))
    f_max = float(getattr(settings, "cooling_variable_saving_max_factor", 1.15))
    variable_factor = f_min + (f_max - f_min) * _clamp(intensity)

    active_count_avg = None
    try:
        active_count_avg = float(baseline.get("active_chiller_count_avg"))
    except Exception:
        pass
    opportunity = _cooling_cascade_opportunity(baseline_energy_4h, active_count_avg)
    bonus_fraction = float(getattr(settings, "cooling_stage_bonus_max_fraction", 0.45)) * opportunity

    continuous = base * variable_factor
    cascade_bonus = continuous * bonus_fraction
    total = continuous + cascade_bonus

    # Hard guardrail: the advisor cannot claim that the controllable 11 UTA save
    # an implausibly large fraction of the whole central-plant 4h baseline.
    total = min(total, max(baseline_energy_4h * 0.18, 0.0))

    meta = {
        "saving_model": "cascade_variable",
        "base_physical_saving_kwh": round(base, 2),
        "load_intensity": round(intensity, 4),
        "variable_stage_factor": round(variable_factor, 4),
        "cascade_opportunity": round(opportunity, 4),
        "cascade_bonus_kwh": round(cascade_bonus, 2),
        "active_chiller_count_avg": round(active_count_avg, 2) if active_count_avg is not None else None,
    }
    return round(max(total, 0.0), 2), meta


def _heating_dynamic_saving_smc(candidate: float, hours: int, baseline_energy_4h: float) -> Tuple[float, Dict[str, Any]]:
    """Heating saving with a mild load-dependent modulation.

    This keeps gas estimates from being completely flat while avoiding any claim
    about a boiler cascade that we have not yet characterized.
    """
    base = max(heating_saving_smc(candidate, hours), 0.0)
    if base <= 0:
        return 0.0, {
            "saving_model": "load_modulated",
            "base_physical_saving_smc": 0.0,
            "load_intensity": _load_intensity("heating", baseline_energy_4h),
            "variable_stage_factor": 0.0,
        }
    intensity = _load_intensity("heating", baseline_energy_4h)
    f_min = float(getattr(settings, "heating_variable_saving_min_factor", 0.65))
    f_max = float(getattr(settings, "heating_variable_saving_max_factor", 1.05))
    factor = f_min + (f_max - f_min) * _clamp(intensity)
    total = min(base * factor, max(baseline_energy_4h * 0.18, 0.0))
    return round(max(total, 0.0), 2), {
        "saving_model": "load_modulated",
        "base_physical_saving_smc": round(base, 2),
        "load_intensity": round(intensity, 4),
        "variable_stage_factor": round(factor, 4),
    }


def _dynamic_max_phase1_saving(regime: str, baseline_energy_4h: float, baseline: Dict[str, Any]) -> float:
    if regime == "cooling":
        vals = [_cooling_dynamic_saving_kwh(float(c), settings.control_interval_h, baseline_energy_4h, baseline)[0] for c in settings.cooling_candidates]
        return max(max(vals), 1.0)
    if regime == "heating":
        vals = [_heating_dynamic_saving_smc(float(c), settings.control_interval_h, baseline_energy_4h)[0] for c in settings.heating_candidates]
        return max(max(vals), 1.0)
    return 1.0


def _weather_temp_at(rows, when: datetime) -> Optional[float]:
    if not rows:
        return None
    when_h = when.replace(minute=0, second=0, microsecond=0)
    best = min(rows, key=lambda p: abs((p.time - when_h).total_seconds()))
    try:
        return float(best.temp_c)
    except Exception:
        return None


def _estimated_current_temperature(regime: str, rows, when: datetime) -> float:
    """
    Fallback estimate for the current return/indoor temperature when no measured
    value is available. This is not a measurement. It adds a small, bounded
    weather-dependent offset to the seasonal nominal value so that the simulated
    6h trend is not artificially flat.
    """
    nominal = nominal_setpoint(regime)
    ext = _weather_temp_at(rows, when)
    if ext is None or regime == "neutral":
        return default_current_temperature(regime)
    if regime == "cooling":
        offset = max(min(0.07 * (ext - nominal), 1.2), -0.6)
    elif regime == "heating":
        offset = max(min(0.05 * (ext - nominal), 0.5), -1.0)
    else:
        offset = 0.0
    return round(nominal + offset, 2)

def _weather_average_temp(rows, start: datetime, hours: int) -> Optional[float]:
    """Average external temperature over a control slot.

    Used only for reconstructing the *display* history of setpoint suggestions.
    This avoids a misleading flat history when the backend is stateless and there
    is no persistent scheduler/database.
    """
    if not rows:
        return None
    end = start + timedelta(hours=hours)
    vals = []
    for p in rows:
        try:
            if start <= p.time < end:
                vals.append(float(p.temp_c))
        except Exception:
            continue
    if not vals:
        v = _weather_temp_at(rows, start)
        return float(v) if v is not None else None
    return sum(vals) / len(vals)


def _baseline_energy_for_slot(slot_time: datetime, regime: str, weather_window) -> float:
    """Predicted 4h central-plant baseline for a slot.

    Cooling returns kWh over 4h; heating returns Smc over 4h.
    """
    if regime not in {"cooling", "heating"}:
        return 0.0
    weather_for_energy = slice_weather(weather_window, slot_time, settings.control_interval_h)
    baseline = energy_models.predict_next_hours(slot_time, regime, weather_for_energy, settings.control_interval_h)
    if regime == "cooling":
        return float(baseline.get("chiller_next_4h_kwh", 0.0) or 0.0)
    return float(baseline.get("gas_next_4h_smc", 0.0) or 0.0)


def _display_history_target_setpoint(slot_time: datetime, regime: str, baseline_energy: float, weather_window) -> float:
    """Deterministic target used for the setpoint-history chart only.

    The real current recommendation is still selected by `_select_for_slot`.
    This display reconstruction answers a different question: *what would the
    supervisor have tended to suggest in past 4h slots if it had been running?*

    It intentionally depends on predicted load and time-of-day, so the chart does
    not collapse into a constant line. This is not a persistent audit log; it is
    a stateless reconstruction for a Render demo where no scheduler/database is
    guaranteed to be running.
    """
    nominal = nominal_setpoint(regime)
    if regime == "neutral":
        return round(nominal, 2)

    intensity = _load_intensity(regime, baseline_energy)
    avg_ext = _weather_average_temp(weather_window, slot_time, settings.control_interval_h)
    hour = slot_time.hour

    if regime == "cooling":
        # Cooling recommendations are allowed only in the conservative phase-1
        # range. At night/low load the supervisor tends to return to nominal;
        # during high-load daytime slots it may step upward.
        candidates = sorted(float(x) for x in settings.cooling_candidates)
        target = settings.setpoint_cooling_c

        daytime = 8 <= hour <= 20
        hot_enough = avg_ext is not None and avg_ext >= 18.0

        if daytime and hot_enough:
            if intensity >= 0.72:
                target = min(25.5, max(candidates))
            elif intensity >= 0.42:
                target = min(25.0, max(candidates))
            elif intensity >= 0.18:
                target = min(24.5, max(candidates))
            else:
                target = settings.setpoint_cooling_c
        else:
            # early morning/night: keep or return toward nominal in the display
            target = settings.setpoint_cooling_c

        return round(float(target), 2)

    if regime == "heating":
        candidates = sorted((float(x) for x in settings.heating_candidates), reverse=True)
        target = settings.setpoint_heating_c

        occupied_like = 6 <= hour <= 20
        cold_enough = avg_ext is not None and avg_ext <= 16.0

        if occupied_like and cold_enough:
            if intensity >= 0.72:
                target = max(20.5, min(candidates))
            elif intensity >= 0.42:
                target = max(21.0, min(candidates))
            elif intensity >= 0.18:
                target = max(21.5, min(candidates))
            else:
                target = settings.setpoint_heating_c
        else:
            target = settings.setpoint_heating_c

        return round(float(target), 2)

    return round(nominal, 2)


def _build_display_setpoint_history(
    slots: List[datetime],
    weather_window,
    current_best: Dict[str, Any],
    force_regime: Optional[str] = None,
) -> List[HistoryPoint]:
    """Build non-flat, load-aware setpoint history for the frontend chart.

    This deliberately does not alter the current control decision. It only
    reconstructs the past visualization in a way that is coherent with the
    supervisor logic and avoids showing a misleading constant line.
    """
    display: List[HistoryPoint] = []
    prev: Optional[float] = None
    prev_regime: Optional[str] = None

    for slot in slots:
        regime = force_regime if slot == slots[-1] and force_regime else get_regime(slot)
        nominal = nominal_setpoint(regime)
        if prev is None or prev_regime != regime:
            prev = nominal

        if slot == slots[-1]:
            # The last point must match the actual current recommendation shown
            # in the main card. This makes any transition into the current 4h
            # slot visible in the chart.
            recommended = float(current_best["candidate_setpoint_c"])
        else:
            baseline_energy = _baseline_energy_for_slot(slot, regime, weather_window)
            target = _display_history_target_setpoint(slot, regime, baseline_energy, weather_window)

            # Apply the same max-step principle used by the real advisor.
            delta = max(min(target - float(prev), settings.max_change_per_decision_c), -settings.max_change_per_decision_c)
            recommended = round(float(prev) + delta, 2)

        if regime == "cooling":
            if recommended > nominal:
                reason = "Ricostruzione supervisore: carico/fronte meteo compatibile con setpoint più alto."
            else:
                reason = "Ricostruzione supervisore: carico basso o fascia conservativa, setpoint nominale."
        elif regime == "heating":
            if recommended < nominal:
                reason = "Ricostruzione supervisore: carico termico compatibile con setpoint più basso."
            else:
                reason = "Ricostruzione supervisore: carico basso o fascia conservativa, setpoint nominale."
        else:
            reason = "Regime neutro ricostruito."

        display.append(HistoryPoint(time=slot, setpoint_c=round(recommended, 2), regime=regime, reason=reason))
        prev = recommended
        prev_regime = regime

    return display


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

    saving_meta: Dict[str, Any] = {}
    if regime == "cooling":
        baseline_energy = float(baseline["chiller_next_4h_kwh"])
        saving, saving_meta = _cooling_dynamic_saving_kwh(candidate, settings.control_interval_h, baseline_energy, baseline)
        optimized = max(baseline_energy - saving, 0.0)
        load_intensity = float(saving_meta.get("load_intensity", _load_intensity(regime, baseline_energy)))
        # Normalize against the best dynamic saving available in this same slot;
        # keep a mild load factor so low-load hours remain conservative.
        energy_gain_score = _normalize_positive(saving, _dynamic_max_phase1_saving(regime, baseline_energy, baseline)) * (0.70 + 0.30 * _clamp(load_intensity))
        saving_smc = None
        saving_kwh_gas = None
        unit = "kWh"
    elif regime == "heating":
        baseline_energy = float(baseline["gas_next_4h_smc"])
        saving_smc, saving_meta = _heating_dynamic_saving_smc(candidate, settings.control_interval_h, baseline_energy)
        saving = saving_smc
        optimized = max(baseline_energy - saving_smc, 0.0)
        load_intensity = float(saving_meta.get("load_intensity", _load_intensity(regime, baseline_energy)))
        energy_gain_score = _normalize_positive(saving_smc, _dynamic_max_phase1_saving(regime, baseline_energy, baseline)) * (0.70 + 0.30 * _clamp(load_intensity))
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
        load_intensity = 0.0

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
        "load_intensity": round(load_intensity, 4),
        "saving_model": saving_meta.get("saving_model"),
        "base_physical_saving": saving_meta.get("base_physical_saving_kwh", saving_meta.get("base_physical_saving_smc")),
        "variable_stage_factor": saving_meta.get("variable_stage_factor"),
        "cascade_opportunity": saving_meta.get("cascade_opportunity"),
        "cascade_bonus_kwh": saving_meta.get("cascade_bonus_kwh"),
        "active_chiller_count_avg": saving_meta.get("active_chiller_count_avg"),
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
) -> Tuple[List[HistoryPoint], Dict[str, Any], List[Dict[str, Any]], float, List[Any], Dict[str, Any], List[HistoryPoint]]:
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
    current_input_temp: Optional[float] = None

    for slot in slots:
        slot_regime = force_regime if slot == valid_from and force_regime else get_regime(slot)
        nominal = nominal_setpoint(slot_regime)

        if previous_setpoint is None or previous_regime != slot_regime:
            previous_setpoint = nominal
        if slot == valid_from and current_setpoint_c is not None:
            previous_setpoint = float(current_setpoint_c)

        if temp_estimate is None or previous_regime != slot_regime:
            temp_estimate = _estimated_current_temperature(slot_regime, weather_window, slot)
        if slot == valid_from and current_temperature_c is not None:
            temp_estimate = float(current_temperature_c)
        if slot == valid_from:
            current_input_temp = float(temp_estimate)

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

    # Separate display reconstruction: do not let frontend setpoint history be
    # artificially flat just because there is no persistent scheduler/database.
    # The current slot is forced to the actual selected recommendation.
    display_history = _build_display_setpoint_history(slots, weather_window, current_selected, force_regime=force_regime)

    return (
        history,
        current_selected,
        current_evaluations,
        float(current_input_temp if current_input_temp is not None else temp_estimate),
        current_weather,
        current_baseline,
        display_history,
    )


def compute_dashboard(current_temperature_c: Optional[float] = None, current_setpoint_c: Optional[float] = None, force_regime: Optional[str] = None) -> DashboardResponse:
    # Render servers usually run in UTC. The control windows, however, must be
    # aligned with the plant local time. We compute a local naive datetime so it
    # remains compatible with the Open-Meteo local timestamps used elsewhere.
    now = datetime.now(ZoneInfo(settings.timezone)).replace(tzinfo=None, microsecond=0)
    valid_from, valid_until = _next_valid_window(now)
    remaining_seconds = max(int((valid_until - now).total_seconds()), 0)

    history, best, evaluations, current_input_temp, weather, baseline, display_history = _build_reconstructed_history(
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
    current_temp = float(current_temperature_c) if current_temperature_c is not None else float(current_input_temp)

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

    # Keep the last 30 reconstructed display points for the chart.
    # This is intentionally separated from the internal control history used for
    # the current selection.
    history_public = display_history[-30:]

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
