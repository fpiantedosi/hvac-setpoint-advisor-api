from __future__ import annotations

from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple
from pathlib import Path
import json
import math
import joblib
import numpy as np
import pandas as pd

from .config import settings
from .schemas import WeatherPoint
from .storage import storage


class EnergyModelRegistry:
    """
    Runtime model layer.

    It tries to use saved joblib models. If model inputs are not available, it
    falls back to historical month/hour profiles. This makes the backend usable
    immediately on Render while preserving a clean path to replace/retrain models.
    """

    def __init__(self):
        self.model_dir = settings.model_dir
        self.features = self._load_features()
        self.models = self._load_models()
        self.profile = storage.load_profile()

    def _load_features(self) -> Dict[str, list]:
        path = self.model_dir / "feature_columns.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _load_models(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {}
        mapping = {
            "chiller_forecast": "model_chiller_forecast.joblib",
            "gas_forecast": "model_gas_forecast.joblib",
            "chiller_baseline": "model_chiller_baseline.joblib",
            "gas_baseline": "model_gas_baseline.joblib",
        }
        for key, filename in mapping.items():
            p = self.model_dir / filename
            if p.exists():
                try:
                    out[key] = joblib.load(p)
                except Exception:
                    pass
        return out

    @staticmethod
    def _calendar_features(ts: datetime, regime: str) -> Dict[str, float]:
        hour = ts.hour
        dow = ts.weekday()
        month = ts.month
        doy = int(ts.strftime("%j"))
        return {
            "hour_sin": math.sin(2 * math.pi * hour / 24),
            "hour_cos": math.cos(2 * math.pi * hour / 24),
            "dow_sin": math.sin(2 * math.pi * dow / 7),
            "dow_cos": math.cos(2 * math.pi * dow / 7),
            "month_sin": math.sin(2 * math.pi * month / 12),
            "month_cos": math.cos(2 * math.pi * month / 12),
            "dayofyear_sin": math.sin(2 * math.pi * doy / 365.25),
            "dayofyear_cos": math.cos(2 * math.pi * doy / 365.25),
            "is_weekend": 1.0 if dow >= 5 else 0.0,
            "is_cooling_regime": 1.0 if regime == "cooling" else 0.0,
            "is_heating_regime": 1.0 if regime == "heating" else 0.0,
            "is_neutral_regime": 1.0 if regime == "neutral" else 0.0,
        }

    def _profile_prediction(self, ts: datetime, regime: str) -> Tuple[float, float, float]:
        """Returns (chiller_kwh_h, gas_smc_h, active_chiller_count)."""
        if self.profile is not None and not self.profile.empty:
            p = self.profile
            match = p[(p["regime"] == regime) & (p["month"] == ts.month) & (p["hour"] == ts.hour)]
            if match.empty:
                match = p[(p["month"] == ts.month) & (p["hour"] == ts.hour)]
            if not match.empty:
                row = match.iloc[0]
                return (
                    float(row.get("chiller_forecast_pred_kwh_h", row.get("chiller_kwh_h", 0.0)) or 0.0),
                    float(row.get("gas_forecast_pred_smc_h", row.get("gas_smc_h", 0.0)) or 0.0),
                    float(row.get("active_chiller_count", 0.0) or 0.0),
                )
        # Deterministic fallback
        if regime == "cooling":
            return 1200.0, 80.0, 3.0
        if regime == "heating":
            return 700.0, 220.0, 2.0
        return 500.0, 40.0, 1.0

    def predict_next_hours(self, now: datetime, regime: str, weather: List[WeatherPoint], hours: int = 4) -> Dict[str, Any]:
        rows = []
        chiller_total = 0.0
        gas_total = 0.0
        active_counts = []
        for i in range(hours):
            ts = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=i)
            ch, gas, active = self._profile_prediction(ts, regime)
            # Simple weather correction. Profile is historical average; this nudges it.
            if weather:
                w = weather[min(i, len(weather)-1)]
                if regime == "cooling":
                    ch *= max(0.65, 1.0 + 0.025 * (w.temp_c - 24.0))
                elif regime == "heating":
                    gas *= max(0.55, 1.0 + 0.035 * (18.0 - w.temp_c))
            ch = max(ch, 0.0)
            gas = max(gas, 0.0)
            chiller_total += ch
            gas_total += gas
            active_counts.append(active)
            rows.append({
                "time": ts.isoformat(),
                "chiller_kwh_h": round(ch, 2),
                "gas_smc_h": round(gas, 2),
                "active_chiller_count": round(active, 1),
            })
        return {
            "chiller_next_4h_kwh": round(chiller_total, 2),
            "gas_next_4h_smc": round(gas_total, 2),
            "active_chiller_count_avg": round(float(np.mean(active_counts)) if active_counts else 0.0, 2),
            "series": rows,
            "source": "profile_with_weather_correction",
        }


energy_models = EnergyModelRegistry()


def cooling_saving_kwh(candidate_setpoint_c: float, hours: int, active_hours: int | None = None) -> float:
    active_hours = hours if active_hours is None else active_hours
    k_total = settings.k_min_kw_per_c_per_uta * settings.n_uta_controlled * settings.k_factor_controller
    delta = candidate_setpoint_c - settings.setpoint_cooling_c
    return k_total * delta / settings.eer_centrale * active_hours


def heating_saving_smc(candidate_setpoint_c: float, hours: int, active_hours: int | None = None) -> float:
    active_hours = hours if active_hours is None else active_hours
    k_total = settings.k_min_kw_per_c_per_uta * settings.n_uta_controlled * settings.k_factor_controller
    delta = settings.setpoint_heating_c - candidate_setpoint_c
    return k_total * delta / (settings.gas_kwh_per_smc * settings.eta_caldaia) * active_hours


def smc_to_kwh_gas(smc: float) -> float:
    return smc * settings.gas_kwh_per_smc
