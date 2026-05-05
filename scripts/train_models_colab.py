"""
Training utility for Colab.

Purpose:
- read gas.csv and frigo.csv with Date/Hour columns;
- download Meteostat/Open-Meteo weather when available;
- train energy baseline/forecast models;
- export joblib models and feature_columns.json for the FastAPI backend.

This script is intentionally separate from the Render backend: training is offline;
Render performs inference only.
"""
from __future__ import annotations

import os, re, json, math, joblib, warnings
from pathlib import Path
import numpy as np
import pandas as pd
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import StandardScaler
from sklearn.linear_model import Ridge
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, r2_score

warnings.filterwarnings("ignore")

GAS_FILE = "/content/gas.csv"
CHILLER_FILE = "/content/frigo.csv"
OUTPUT_DIR = "/content/hvac_model_export"
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

HEATING_MONTHS = [1,2,3,11,12]
COOLING_MONTHS = [5,6,7,8,9,10]
CHILLER_ACTIVE_THRESHOLD_KWH_H = 20.0
PCS_GAS_KWH_PER_SMC = 10.69


def norm_name(c):
    m = re.search(r"(GF\d+)", str(c).upper())
    if m: return m.group(1)
    m = re.search(r"Caldaia\s+ICI[_\s-]*(\d+)", str(c), flags=re.I)
    if m: return f"CALDAIA_{m.group(1)}"
    m = re.search(r"GN\d+", str(c).upper())
    if m: return m.group(0)
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(c)[:40]).strip("_")


def read_csv(path):
    df = pd.read_csv(path, sep=None, engine="python", encoding="utf-8-sig", dtype=str)
    df.columns = [str(c).strip() for c in df.columns]
    if "Date" not in df.columns or "Hour" not in df.columns:
        raise ValueError(f"File {path}: servono colonne Date e Hour")
    ts = pd.to_datetime(df["Date"].astype(str) + " " + df["Hour"].astype(str), dayfirst=True, errors="coerce")
    df = df.drop(columns=["Date", "Hour"])
    df.insert(0, "timestamp", ts)
    df = df.dropna(subset=["timestamp"]).set_index("timestamp").sort_index()
    for c in df.columns:
        s = df[c].astype(str).str.replace(".", "", regex=False).str.replace(",", ".", regex=False)
        df[c] = pd.to_numeric(s, errors="coerce")
    return df.groupby(df.index).sum(min_count=1).resample("h").sum(min_count=1)


def add_features(df):
    feat = df.copy()
    feat["hour"] = feat.index.hour
    feat["dow"] = feat.index.dayofweek
    feat["month"] = feat.index.month
    feat["dayofyear"] = feat.index.dayofyear
    feat["is_weekend"] = (feat["dow"] >= 5).astype(int)
    for period, col in [(24,"hour"),(7,"dow"),(12,"month"),(365.25,"dayofyear")]:
        feat[f"{col}_sin"] = np.sin(2*np.pi*feat[col]/period)
        feat[f"{col}_cos"] = np.cos(2*np.pi*feat[col]/period)
    feat["regime"] = "neutral"
    feat.loc[feat.index.month.isin(COOLING_MONTHS), "regime"] = "cooling"
    feat.loc[feat.index.month.isin(HEATING_MONTHS), "regime"] = "heating"
    feat["is_cooling_regime"] = (feat.regime == "cooling").astype(int)
    feat["is_heating_regime"] = (feat.regime == "heating").astype(int)
    feat["is_neutral_regime"] = (feat.regime == "neutral").astype(int)

    for target in ["chiller_kwh_h", "gas_smc_h", "active_chiller_count"]:
        if target not in feat.columns:
            continue
        for lag in [1,2,3,6,12,24,48]: feat[f"{target}_lag_{lag}h"] = feat[target].shift(lag)
        for win in [3,6,12,24,48]: feat[f"{target}_mean_{win}h"] = feat[target].rolling(win, min_periods=max(2, win//3)).mean()
    return feat


def train(feat, target, features, name):
    features = [c for c in features if c in feat.columns]
    mdf = feat[[target] + features].dropna()
    split = int(len(mdf) * 0.8)
    tr, te = mdf.iloc[:split], mdf.iloc[split:]
    model = make_pipeline(StandardScaler(), Ridge(alpha=10.0))
    model.fit(tr[features], tr[target])
    pred = np.maximum(model.predict(te[features]), 0)
    metrics = {
        "model": name,
        "target": target,
        "mae": float(mean_absolute_error(te[target], pred)),
        "rmse": float(np.sqrt(np.mean((te[target].values - pred)**2))),
        "r2": float(r2_score(te[target], pred)),
    }
    return model, features, metrics


def main():
    gas = read_csv(GAS_FILE).rename(columns=lambda c: norm_name(c))
    gas = gas.T.groupby(level=0).sum().T
    gas_cols = list(gas.columns)
    gas["gas_smc_h"] = gas[gas_cols].sum(axis=1, min_count=1)
    gas["gas_kwh_input_h"] = gas["gas_smc_h"] * PCS_GAS_KWH_PER_SMC

    ch = read_csv(CHILLER_FILE).rename(columns=lambda c: norm_name(c))
    ch = ch.T.groupby(level=0).sum().T
    gf_cols = list(ch.columns)
    ch["chiller_kwh_h"] = ch[gf_cols].sum(axis=1, min_count=1)
    for c in gf_cols:
        ch[f"{c}_active"] = (ch[c] > CHILLER_ACTIVE_THRESHOLD_KWH_H).astype(int)
    ch["active_chiller_count"] = ch[[f"{c}_active" for c in gf_cols]].sum(axis=1)

    idx = pd.date_range(min(gas.index.min(), ch.index.min()), max(gas.index.max(), ch.index.max()), freq="h")
    df = pd.DataFrame(index=idx).join(gas).join(ch, rsuffix="_chiller")
    feat = add_features(df)

    base = ["hour_sin","hour_cos","dow_sin","dow_cos","month_sin","month_cos","dayofyear_sin","dayofyear_cos","is_weekend","is_cooling_regime","is_heating_regime","is_neutral_regime"]
    ch_extra = [f"chiller_kwh_h_lag_{l}h" for l in [1,2,3,6,12,24,48]] + [f"chiller_kwh_h_mean_{w}h" for w in [3,6,12,24,48]] + [f"active_chiller_count_lag_{l}h" for l in [1,2,3,6,12,24]] + [f"active_chiller_count_mean_{w}h" for w in [3,6,12,24]]
    ga_extra = [f"gas_smc_h_lag_{l}h" for l in [1,2,3,6,12,24,48]] + [f"gas_smc_h_mean_{w}h" for w in [3,6,12,24,48]]

    specs = [
        ("chiller_kwh_h", base, "model_chiller_baseline.joblib", "chiller_baseline"),
        ("chiller_kwh_h", base+ch_extra, "model_chiller_forecast.joblib", "chiller_forecast"),
        ("gas_smc_h", base, "model_gas_baseline.joblib", "gas_baseline"),
        ("gas_smc_h", base+ga_extra, "model_gas_forecast.joblib", "gas_forecast"),
    ]
    feature_export = {}
    metrics = []
    for target, features, filename, key in specs:
        model, used_features, m = train(feat, target, features, key)
        joblib.dump(model, Path(OUTPUT_DIR)/filename)
        feature_export[key] = used_features
        metrics.append(m)
    (Path(OUTPUT_DIR)/"feature_columns.json").write_text(json.dumps(feature_export, indent=2), encoding="utf-8")
    pd.DataFrame(metrics).to_csv(Path(OUTPUT_DIR)/"model_metrics.csv", index=False)

    profile_cols = ["chiller_kwh_h", "gas_smc_h", "active_chiller_count"]
    profile = feat.groupby(["regime", "month", "hour"])[profile_cols].mean().reset_index()
    profile.to_csv(Path(OUTPUT_DIR)/"historical_profile.csv", index=False)
    print(pd.DataFrame(metrics))
    print(f"Export completato in {OUTPUT_DIR}")

if __name__ == "__main__":
    main()
