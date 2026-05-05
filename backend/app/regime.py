from __future__ import annotations

from datetime import datetime
from .config import settings


def get_regime(ts: datetime) -> str:
    month = ts.month
    if month in settings.cooling_months:
        return "cooling"
    if month in settings.heating_months:
        return "heating"
    return "neutral"


def nominal_setpoint(regime: str) -> float:
    if regime == "cooling":
        return settings.setpoint_cooling_c
    if regime == "heating":
        return settings.setpoint_heating_c
    return settings.setpoint_cooling_c
