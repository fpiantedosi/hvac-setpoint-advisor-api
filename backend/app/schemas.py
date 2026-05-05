from __future__ import annotations

from datetime import datetime
from typing import List, Optional, Literal
from pydantic import BaseModel, Field


Regime = Literal["cooling", "heating", "neutral"]
Confidence = Literal["low", "medium", "high"]


class WeatherPoint(BaseModel):
    time: datetime
    temp_c: float
    rh_pct: Optional[float] = None
    dewpoint_c: Optional[float] = None


class TemperatureForecastPoint(BaseModel):
    time: datetime
    predicted_c: float
    external_c: Optional[float] = None


class EnergyBlock(BaseModel):
    baseline_next_4h_kwh: Optional[float] = None
    optimized_next_4h_kwh: Optional[float] = None
    estimated_saving_next_4h_kwh: Optional[float] = None


class GasBlock(BaseModel):
    baseline_next_4h_smc: Optional[float] = None
    optimized_next_4h_smc: Optional[float] = None
    estimated_saving_next_4h_smc: Optional[float] = None
    estimated_saving_next_4h_kwh_gas: Optional[float] = None


class ScoreBlock(BaseModel):
    energy_score: float
    comfort_penalty: float
    stability_penalty: float
    process_penalty: float
    edge_penalty: float
    persistence_penalty: float
    temperature_penalty: float
    global_score: float


class ConstraintBlock(BaseModel):
    decision_interval_h: int
    thermal_forecast_h: int
    max_change_per_decision_c: float
    phase1_cooling_max_c: float
    phase1_heating_min_c: float
    cooling_temperature_limit_c: float
    heating_temperature_limit_c: float


class Decision(BaseModel):
    timestamp: datetime
    mode: Regime
    valid_from: datetime
    valid_until: datetime
    remaining_seconds: int
    current_setpoint_c: float
    recommended_setpoint_c: float
    nominal_setpoint_c: float
    confidence: Confidence
    reason: str
    warning: Optional[str] = None
    energy: EnergyBlock
    gas: GasBlock
    temperature_source: Literal["measured", "operator_input", "simulated"]
    current_temperature_c: float
    temperature_forecast_6h: List[TemperatureForecastPoint]
    constraints: ConstraintBlock
    scores: ScoreBlock


class HistoryPoint(BaseModel):
    time: datetime
    setpoint_c: float
    regime: Regime
    reason: Optional[str] = None


class DashboardResponse(BaseModel):
    decision: Decision
    setpoint_history: List[HistoryPoint]
    weather_forecast: List[WeatherPoint]
    energy_series: List[dict]
    candidate_evaluations: List[dict]


class RecomputeRequest(BaseModel):
    current_temperature_c: Optional[float] = Field(default=None, description="Current return/indoor temperature. If absent, the backend uses the simulator default.")
    current_setpoint_c: Optional[float] = None
    force_regime: Optional[Regime] = None
