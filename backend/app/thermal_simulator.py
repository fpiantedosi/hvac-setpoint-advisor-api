from __future__ import annotations

from datetime import datetime, timedelta
from typing import List

from .config import settings
from .schemas import TemperatureForecastPoint, WeatherPoint


def default_current_temperature(regime: str) -> float:
    if regime == "heating":
        return settings.default_current_temp_heating_c
    return settings.default_current_temp_cooling_c


def simulate_temperature(
    current_temp_c: float,
    setpoint_c: float,
    weather: List[WeatherPoint],
    regime: str,
    hours: int | None = None,
) -> List[TemperatureForecastPoint]:
    """
    First-order grey-box thermal simulator.

    T(k+1) = T(k)
           + dt/tau_env  * (T_ext(k) - T(k))
           + dt/tau_hvac * (T_sp     - T(k))

    It is intentionally conservative and should be replaced by a trained
    return-air/internal-temperature model when real internal temperatures exist.
    """
    hours = hours or settings.thermal_forecast_h
    dt = 1.0
    temp = float(current_temp_c)
    out: List[TemperatureForecastPoint] = []
    usable_weather = weather[: hours + 1]
    if not usable_weather:
        now = datetime.now().replace(minute=0, second=0, microsecond=0)
        usable_weather = [WeatherPoint(time=now + timedelta(hours=i), temp_c=temp) for i in range(hours + 1)]

    for i in range(1, hours + 1):
        w = usable_weather[min(i - 1, len(usable_weather) - 1)]
        ext = float(w.temp_c)
        env = dt / settings.tau_env_h * (ext - temp)
        hvac = dt / settings.tau_hvac_h * (setpoint_c - temp)
        temp = temp + env + hvac
        out.append(TemperatureForecastPoint(
            time=usable_weather[min(i, len(usable_weather) - 1)].time,
            predicted_c=round(temp, 2),
            external_c=round(ext, 2),
        ))
    return out


def temperature_violation_score(forecast: List[TemperatureForecastPoint], regime: str) -> float:
    if not forecast:
        return 0.0
    score = 0.0
    if regime == "cooling":
        limit = settings.cooling_temperature_limit_c
        for p in forecast:
            score += max(p.predicted_c - limit, 0.0) ** 2
    elif regime == "heating":
        limit = settings.heating_temperature_limit_c
        for p in forecast:
            score += max(limit - p.predicted_c, 0.0) ** 2
    return score
