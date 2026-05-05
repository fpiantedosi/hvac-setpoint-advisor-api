from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Tuple


@dataclass(frozen=True)
class Settings:
    # Site
    site_name: str = "Grottaglie HVAC Setpoint Advisor"
    latitude: float = 40.5369
    longitude: float = 17.4372
    timezone: str = "Europe/Rome"

    # Seasonal regime
    heating_months: Tuple[int, ...] = (1, 2, 3, 11, 12)
    cooling_months: Tuple[int, ...] = (5, 6, 7, 8, 9, 10)
    setpoint_heating_c: float = 22.0
    setpoint_cooling_c: float = 24.0

    # Full theoretical bands, used for sweep only
    cooling_band: Tuple[float, float] = (21.0, 27.0)
    heating_band: Tuple[float, float] = (19.0, 25.0)
    setpoint_step_c: float = 0.5

    # Conservative phase-1 controller candidates
    # The controller does NOT use the whole theoretical band in phase 1.
    cooling_candidates: Tuple[float, ...] = (24.0, 24.5, 25.0, 25.5)
    heating_candidates: Tuple[float, ...] = (22.0, 21.5, 21.0, 20.5)
    max_change_per_decision_c: float = 0.5

    # Timing
    control_interval_h: int = 4
    thermal_forecast_h: int = 6
    weather_forecast_h: int = 6

    # Stateless reconstruction: how many past hours to rebuild for the frontend
    # when there is no persistent scheduler/database.
    setpoint_history_lookback_h: int = 72

    # Weather service
    # Open-Meteo is used through plain HTTP requests because it needs no API key and
    # is convenient on Render. The service returns hourly values; 15-min values can
    # be added later for frontend-only visualization.
    weather_provider: str = "open_meteo"
    weather_timeout_s: int = 12

    # Physical parameters
    n_uta_controlled: int = 11
    k_min_kw_per_c_per_uta: float = 6.36
    k_factor_controller: float = 1.0
    eer_centrale: float = 3.2
    gas_kwh_per_smc: float = 10.69
    eta_caldaia: float = 0.90

    # Thermal simulator parameters
    tau_env_h: float = 24.0
    tau_hvac_h: float = 8.0
    default_current_temp_cooling_c: float = 24.0
    default_current_temp_heating_c: float = 22.0
    cooling_temperature_limit_c: float = 26.0
    heating_temperature_limit_c: float = 20.0

    # Load-intensity thresholds used by the controller.
    # The energy score is activated more strongly when the predicted central plant
    # load is materially high. This avoids flat, always-at-limit histories and makes
    # the reconstructed 4h recommendations react to weather/load conditions.
    cooling_load_low_4h_kwh: float = 2200.0
    cooling_load_high_4h_kwh: float = 6200.0
    heating_load_low_4h_smc: float = 350.0
    heating_load_high_4h_smc: float = 1300.0

    # Technical score weights: dimensionless, not monetary
    weight_energy: float = 1.40
    weight_comfort: float = 0.60
    weight_stability: float = 0.10
    weight_process: float = 1.80
    weight_edge: float = 2.40
    weight_persistence: float = 1.80
    weight_temperature_violation: float = 9.0
    min_score_improvement_to_change: float = 0.005

    # Conservative preferred region inside phase-1 limits
    # These values make the controller less eager to stay near phase-1 edges.
    cooling_preferred_max_c: float = 25.0
    heating_preferred_min_c: float = 21.0

    # Model/data paths
    base_dir: Path = Path(__file__).resolve().parents[1]

    @property
    def model_dir(self) -> Path:
        return self.base_dir / "models"

    @property
    def data_dir(self) -> Path:
        return self.base_dir / "data"

    @property
    def static_dir(self) -> Path:
        return self.base_dir / "static"


settings = Settings()
