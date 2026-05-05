# Aggiornamento frontend + controllore

Copia questi file nella stessa struttura del repository GitHub:

- `backend/app/config.py`
- `backend/app/controller.py`
- `backend/app/thermal_simulator.py`
- `backend/static/index.html`
- `backend/static/app.js`
- `backend/static/styles.css`

Dopo il commit su `main`, Render avvierà automaticamente un nuovo deploy se Auto Deploy è attivo.
In alternativa, su Render: `Manual Deploy -> Deploy latest commit`.

Modifiche principali:

1. Grafico temperatura separa chiaramente:
   - temperatura interna attuale;
   - temperatura interna/ripresa prevista 6h in rosso;
   - temperatura esterna prevista;
   - setpoint consigliato.

2. Baseline energia divisa in due grafici:
   - gruppi frigo in kWh/h;
   - caldaie in Smc/h.

3. Tabella candidati più chiara:
   - saving freddo;
   - saving caldo;
   - kWh gas equivalenti;
   - energia score;
   - penalità tecniche;
   - score globale;
   - esito.

4. Controllore corretto:
   - l'energy score non viene più normalizzato sul consumo totale centrale, ma sul massimo saving disponibile nella fase conservativa;
   - questo evita che il contributo controllabile delle 11 UTA sia schiacciato dal carico complessivo della centrale;
   - il setpoint raccomandato resta scelto sullo score globale, non sul solo saving.

5. Simulatore termico:
   - meno piatto;
   - inizializzazione stimata da meteo quando non è disponibile temperatura interna reale;
   - orizzonte previsione 6h.
