# Aggiornamento storico setpoint 20260505h

Sostituire **solo**:

- `backend/app/controller.py`

Non modifica altri pannelli del frontend.

## Cosa corregge

Il grafico “Storico setpoint proposti” non usa più la stessa catena interna che seleziona il setpoint corrente, perché in deploy stateless su Render può diventare artificialmente piatta. Ora viene costruita una ricostruzione deterministica dedicata al grafico, basata su:

- slot da 4 ore;
- carico energetico previsto nello slot;
- temperatura esterna media dello slot;
- fascia oraria;
- vincolo di variazione massima ±0,5 °C per slot.

L’ultimo punto dello storico coincide sempre con il setpoint attualmente raccomandato nella card principale.

Il controllo corrente e gli altri pannelli non vengono modificati.
