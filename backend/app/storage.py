from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Optional
import pandas as pd

from .config import settings


class Storage:
    def __init__(self):
        self.data_dir = settings.data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.history_path = self.data_dir / "decision_history.csv"
        self.seed_path = self.data_dir / "decision_history_seed.csv"
        self.profile_path = self.data_dir / "historical_profile.csv"
        self.energy_history_path = self.data_dir / "recent_energy_history.csv"
        self._ensure_history()

    def _ensure_history(self):
        if self.history_path.exists():
            return
        if self.seed_path.exists():
            df = pd.read_csv(self.seed_path)
            # Normalize past decisions to current time so the frontend has recent history.
            if "decision_time" in df.columns and len(df):
                df = df.tail(30).copy()
                now = datetime.now().replace(minute=0, second=0, microsecond=0)
                times = [now - timedelta(hours=4 * (len(df) - i - 1)) for i in range(len(df))]
                df["decision_time"] = [t.isoformat() for t in times]
            df.to_csv(self.history_path, index=False)
        else:
            now = datetime.now().replace(minute=0, second=0, microsecond=0)
            rows = []
            for i in range(12):
                t = now - timedelta(hours=4 * (11 - i))
                rows.append({
                    "decision_time": t.isoformat(),
                    "regime": "neutral",
                    "previous_setpoint_c": 24.0,
                    "recommended_setpoint_c": 24.0,
                    "nominal_setpoint_c": 24.0,
                    "action": "seed",
                    "estimated_saving_energy": 0.0,
                    "saving_unit": "none",
                    "reason": "storico iniziale simulato",
                })
            pd.DataFrame(rows).to_csv(self.history_path, index=False)

    def read_decision_history(self) -> pd.DataFrame:
        try:
            df = pd.read_csv(self.history_path)
            if "decision_time" in df.columns:
                df["decision_time"] = pd.to_datetime(df["decision_time"], errors="coerce")
                df = df.dropna(subset=["decision_time"]).sort_values("decision_time")
            return df
        except Exception:
            return pd.DataFrame()

    def append_decision(self, row: dict) -> None:
        df = self.read_decision_history()
        row = dict(row)
        if isinstance(row.get("decision_time"), datetime):
            row["decision_time"] = row["decision_time"].isoformat()
        add = pd.DataFrame([row])
        if not df.empty:
            df["decision_time"] = df["decision_time"].astype(str)
            out = pd.concat([df, add], ignore_index=True)
        else:
            out = add
        out = out.tail(500)
        out.to_csv(self.history_path, index=False)

    def last_setpoint(self, regime: str, default: float) -> float:
        df = self.read_decision_history()
        if df.empty or "recommended_setpoint_c" not in df.columns:
            return default
        sub = df[df.get("regime", "") == regime]
        if sub.empty:
            return default
        return float(sub.iloc[-1]["recommended_setpoint_c"])

    def aggressive_streak(self, regime: str, threshold: float) -> int:
        df = self.read_decision_history()
        if df.empty:
            return 0
        sub = df[df.get("regime", "") == regime].tail(6)
        streak = 0
        for _, row in sub.iloc[::-1].iterrows():
            sp = float(row.get("recommended_setpoint_c", 0))
            if regime == "cooling" and sp >= threshold:
                streak += 1
            elif regime == "heating" and sp <= threshold:
                streak += 1
            else:
                break
        return streak

    def load_profile(self) -> pd.DataFrame:
        if self.profile_path.exists():
            return pd.read_csv(self.profile_path)
        return pd.DataFrame()

    def load_recent_energy(self) -> pd.DataFrame:
        if self.energy_history_path.exists():
            df = pd.read_csv(self.energy_history_path)
            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
                df = df.dropna(subset=["timestamp"]).sort_values("timestamp")
            return df
        return pd.DataFrame()


storage = Storage()
