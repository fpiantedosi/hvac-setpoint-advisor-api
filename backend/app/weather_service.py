from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict, List, Tuple
import math
import requests

from .config import settings
from .schemas import WeatherPoint

# Simple in-memory cache. It is deliberately not a persistent cache: on Render free
# the filesystem may be ephemeral. This is enough to avoid hammering the weather API
# while multiple browsers poll the dashboard.
_WEATHER_CACHE: Dict[Tuple[str, int, int], tuple[datetime, List[WeatherPoint]]] = {}
_CACHE_TTL_SECONDS = 10 * 60


def _floor_hour(dt: datetime) -> datetime:
    return dt.replace(minute=0, second=0, microsecond=0)


def _fallback_window(now: datetime, past_hours: int, forecast_hours: int) -> List[WeatherPoint]:
    """Deterministic fallback used when the network is unavailable."""
    now_h = _floor_hour(now)
    out: List[WeatherPoint] = []
    base = 18.0 + 8.0 * math.sin(2 * math.pi * (now_h.timetuple().tm_yday - 80) / 365.25)
    for i in range(-past_hours, forecast_hours + 1):
        t = now_h + timedelta(hours=i)
        diurnal = 4.0 * math.sin(2 * math.pi * (t.hour - 6) / 24.0)
        temp = base + diurnal
        out.append(WeatherPoint(time=t, temp_c=round(temp, 2), rh_pct=None, dewpoint_c=None))
    return out


def _parse_open_meteo_hourly(payload: dict) -> List[WeatherPoint]:
    hourly = payload.get("hourly", {})
    times = hourly.get("time", []) or []
    temps = hourly.get("temperature_2m", []) or []
    rh = hourly.get("relative_humidity_2m", []) or []
    dwpt = hourly.get("dew_point_2m", []) or []

    rows: List[WeatherPoint] = []
    n = min(len(times), len(temps))
    for idx in range(n):
        try:
            t = datetime.fromisoformat(str(times[idx]))
            temp = float(temps[idx])
        except Exception:
            continue
        rows.append(
            WeatherPoint(
                time=t,
                temp_c=temp,
                rh_pct=float(rh[idx]) if idx < len(rh) and rh[idx] is not None else None,
                dewpoint_c=float(dwpt[idx]) if idx < len(dwpt) and dwpt[idx] is not None else None,
            )
        )
    rows.sort(key=lambda p: p.time)
    return rows


def get_weather_window(now: datetime, past_hours: int = 0, forecast_hours: int | None = None) -> List[WeatherPoint]:
    """
    Returns hourly external weather from Open-Meteo over a window:
    [now - past_hours, now + forecast_hours].

    Important interpretation:
    - for current/future slots, these are weather forecasts;
    - for the recent past, Open-Meteo Forecast API provides archived/past forecast values,
      not guaranteed measured observations. That is acceptable here because the values are
      used to rebuild a coherent advisory history for the dashboard, not to certify meteo data.
    """
    forecast_hours = forecast_hours if forecast_hours is not None else settings.weather_forecast_h
    now_h = _floor_hour(now)
    key = (now_h.isoformat(), int(past_hours), int(forecast_hours))

    cached = _WEATHER_CACHE.get(key)
    if cached:
        cached_at, rows = cached
        if (datetime.now() - cached_at).total_seconds() < _CACHE_TTL_SECONDS:
            return rows

    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": settings.latitude,
        "longitude": settings.longitude,
        "hourly": "temperature_2m,relative_humidity_2m,dew_point_2m",
        "past_hours": max(int(past_hours), 0),
        "forecast_hours": max(int(forecast_hours), 1),
        "timezone": settings.timezone,
    }

    try:
        r = requests.get(url, params=params, timeout=settings.weather_timeout_s)
        r.raise_for_status()
        rows = _parse_open_meteo_hourly(r.json())
        if rows:
            # Restrict and pad only if necessary.
            start = now_h - timedelta(hours=past_hours)
            end = now_h + timedelta(hours=forecast_hours)
            rows = [p for p in rows if start <= p.time <= end]
            if rows:
                _WEATHER_CACHE[key] = (datetime.now(), rows)
                return rows
    except Exception:
        pass

    rows = _fallback_window(now_h, past_hours, forecast_hours)
    _WEATHER_CACHE[key] = (datetime.now(), rows)
    return rows


def get_weather_forecast(now: datetime, hours: int | None = None) -> List[WeatherPoint]:
    """Compatibility wrapper used by older backend code."""
    hours = hours or settings.weather_forecast_h
    now_h = _floor_hour(now)
    rows = get_weather_window(now_h, past_hours=0, forecast_hours=hours)
    return [p for p in rows if p.time >= now_h][: hours + 1]


def slice_weather(rows: List[WeatherPoint], start: datetime, hours: int) -> List[WeatherPoint]:
    """Extracts a local forecast/historical slice starting at start for hours ahead."""
    start_h = _floor_hour(start)
    end_h = start_h + timedelta(hours=hours)
    selected = [p for p in rows if start_h <= p.time <= end_h]
    if selected:
        return selected[: hours + 1]
    # fallback if the requested slice is absent
    return _fallback_window(start_h, past_hours=0, forecast_hours=hours)
