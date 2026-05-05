# Aggiornamento Render - HVAC Setpoint Advisor

Questi file vanno copiati dentro la struttura reale del pacchetto:

```text
hvac_setpoint_advisor/
├── backend/
│   └── app/
│       ├── config.py              <-- sostituire
│       ├── controller.py          <-- sostituire
│       ├── main.py                <-- sostituire
│       └── weather_service.py     <-- sostituire
└── render.yaml                    <-- già corretto nel pacchetto originale
```

Non devi spostare `static`, `data` e `models`: nel tuo zip sono correttamente sotto `backend/`.

## Cosa cambia

1. Lo storico setpoint non dipende più da un file CSV aggiornato da uno scheduler.
2. Ogni apertura della dashboard ricostruisce deterministicamente le ultime 72 ore di raccomandazioni a passo 4h.
3. La raccomandazione corrente è valida fino alla fine dello slot 4h in corso.
4. La previsione termica resta a 6h.
5. Il backend usa Open-Meteo via HTTP per dati meteo orari passati/recenti e previsionali.
6. Non vengono esposti valori economici.

## Deploy su Render

Carica su GitHub questa struttura:

```text
repo/
├── backend/
├── scripts/
├── render.yaml
└── README.md
```

Nel Web Service Render, se non usi Blueprint:

```text
Environment: Python
Root directory: backend
Build command: pip install -r requirements.txt
Start command: uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

## Endpoint principali

```text
/
/api/status
/api/dashboard
/api/current-decision
/api/history/setpoints
/api/forecast
/api/weather/window?past_hours=24&forecast_hours=24
/api/sweep
```

## Nota

La versione è stateless: è adatta a Render Free perché non richiede filesystem persistente per aggiornare lo storico setpoint. Per una versione industriale serve comunque database/audit log.
