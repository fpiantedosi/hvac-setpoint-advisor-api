from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.responses import Response

from .config import settings
from .schemas import DashboardResponse, RecomputeRequest
from .controller import compute_dashboard, build_setpoint_sweep
from .weather_service import get_weather_window

app = FastAPI(title="HVAC Setpoint Advisor", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def no_cache_api_responses(request: Request, call_next):
    response: Response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response


@app.get("/api/status")
def status():
    return {
        "service": settings.site_name,
        "status": "ok",
        "mode": "stateless_reconstructed_history",
        "control_interval_h": settings.control_interval_h,
        "thermal_forecast_h": settings.thermal_forecast_h,
        "setpoint_history_lookback_h": settings.setpoint_history_lookback_h,
        "phase1_cooling_candidates": settings.cooling_candidates,
        "phase1_heating_candidates": settings.heating_candidates,
        "units": {
            "cooling": "kWh elettrici",
            "heating": "Smc gas e kWh gas equivalenti",
        },
        "note": "Il backend ricostruisce on-demand lo storico setpoint recente da meteo e controllore deterministico. Nessuna stima economica viene esposta.",
    }


@app.get("/api/dashboard", response_model=DashboardResponse)
def dashboard(current_temperature_c: float | None = None, current_setpoint_c: float | None = None, force_regime: str | None = None):
    return compute_dashboard(current_temperature_c=current_temperature_c, current_setpoint_c=current_setpoint_c, force_regime=force_regime)


@app.get("/api/current-decision")
def current_decision(current_temperature_c: float | None = None, current_setpoint_c: float | None = None, force_regime: str | None = None):
    return compute_dashboard(current_temperature_c=current_temperature_c, current_setpoint_c=current_setpoint_c, force_regime=force_regime).decision


@app.get("/api/history/setpoints")
def setpoint_history(current_temperature_c: float | None = None, current_setpoint_c: float | None = None, force_regime: str | None = None):
    # Stateless: the history is reconstructed on every request, so multiple devices see a coherent view.
    return compute_dashboard(current_temperature_c=current_temperature_c, current_setpoint_c=current_setpoint_c, force_regime=force_regime).setpoint_history


@app.get("/api/forecast")
def forecast(current_temperature_c: float | None = None, current_setpoint_c: float | None = None, force_regime: str | None = None):
    dash = compute_dashboard(current_temperature_c=current_temperature_c, current_setpoint_c=current_setpoint_c, force_regime=force_regime)
    return {
        "temperature": dash.decision.temperature_forecast_6h,
        "weather": dash.weather_forecast,
        "energy_series": dash.energy_series,
    }


@app.get("/api/weather/window")
def weather_window(past_hours: int = 24, forecast_hours: int = 24):
    from datetime import datetime
    return get_weather_window(datetime.now(), past_hours=past_hours, forecast_hours=forecast_hours)


@app.get("/api/sweep")
def sweep():
    return build_setpoint_sweep()


@app.post("/api/recompute")
def recompute(req: RecomputeRequest):
    # In this stateless version recompute is deterministic and does not need persistent storage.
    return compute_dashboard(
        current_temperature_c=req.current_temperature_c,
        current_setpoint_c=req.current_setpoint_c,
        force_regime=req.force_regime,
    )


# Static frontend
app.mount("/static", StaticFiles(directory=str(settings.static_dir)), name="static")


@app.get("/")
def index():
    return FileResponse(settings.static_dir / "index.html")
