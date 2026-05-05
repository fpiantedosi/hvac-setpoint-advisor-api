# HVAC Setpoint Advisor

Backend FastAPI + frontend statico per raccomandare un setpoint comune alle 11 UTA.

## Logica del progetto

La prima release usa un supervisore ibrido:

1. **Baseline energetica**: modelli dati/profili storici stimano il consumo atteso di gruppi frigo e caldaie.
2. **Effetto setpoint**: calcolo fisico-parametrico prudente, perché lo storico ha setpoint fisso stagionale.
3. **Simulatore termico 6h**: fallback finché non saranno disponibili temperature interne/ripresa timestampate.
4. **Controllore conservativo 4h**: genera una proposta valida per 4 ore, senza spingersi ai bordi teorici della banda.

Unità esposte:

- freddo: kWh elettrici;
- caldo: Smc gas e kWh gas equivalenti;
- nessun valore economico viene calcolato o mostrato.

## Limiti fase 1

Il controllore operativo usa una banda conservativa:

- cooling: 24.0 / 24.5 / 25.0 / 25.5 °C;
- heating: 22.0 / 21.5 / 21.0 / 20.5 °C.

Lo sweep teorico resta invece disponibile sull'intera banda:

- cooling: 21–27 °C;
- heating: 19–25 °C.

## Avvio locale

```bash
cd backend
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Aprire:

```text
http://127.0.0.1:8000
```

API principali:

```text
GET  /api/status
GET  /api/dashboard
GET  /api/current-decision
GET  /api/history/setpoints
GET  /api/forecast
GET  /api/sweep
POST /api/recompute
```

## Deploy Render

La repo contiene `render.yaml`. Su Render:

1. collega il repository GitHub;
2. scegli "Blueprint" oppure crea un Web Service;
3. root directory: `backend`;
4. build command: `pip install -r requirements.txt`;
5. start command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

## Training modelli

Lo script offline è:

```text
scripts/train_models_colab.py
```

Da Colab:

1. caricare `gas.csv` e `frigo.csv`;
2. modificare i path nello script;
3. eseguire;
4. copiare in `backend/models/` i `.joblib` e `feature_columns.json` generati;
5. copiare in `backend/data/` `historical_profile.csv`.

## Temperatura interna/ripresa

Oggi manca il dato timestampato di temperatura interna/ripresa. Il backend usa quindi un simulatore termico di primo ordine. Quando il dato sarà disponibile, il simulatore dovrà diventare fallback e verrà aggiunto un modello predittivo reale.
