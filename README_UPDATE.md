# Aggiornamento 2026-05-05f

Sostituire nel repository GitHub questi file:

- backend/app/config.py
- backend/app/controller.py
- backend/static/index.html
- backend/static/app.js
- backend/static/styles.css

Modifiche:

1. Lo storico setpoint non viene più reso artificialmente piatto: il controllore ricostruito sulle ultime 72 ore pesa anche l'intensità di carico prevista dello slot 4h. In slot di basso carico tende a restare più vicino al nominale; in slot di carico più alto può suggerire +0,5 °C in cooling o -0,5 °C in heating.
2. Non sono stati modificati timer, grafici temperatura, grafico energia con doppio asse o polling.
3. Aggiunto grafico "Saving stimato per candidato" basato sui candidati setpoint correnti.
4. Inserita una regola CSS di sicurezza che nasconde eventuali pannelli legacy di motivazione rimasti in cache o in vecchi HTML.
5. Aggiornato cache-busting degli asset statici a `v=20260505f`.

Dopo il commit su main, Render dovrebbe auto-deployare. Se la UI mostra ancora elementi vecchi, usare Ctrl+F5 o finestra anonima.
