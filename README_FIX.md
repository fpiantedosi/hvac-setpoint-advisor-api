# Fix backend /api/dashboard 500

Sostituisci questi file nel repository GitHub:

- backend/app/config.py
- backend/app/controller.py

Il problema era una disallineamento tra controller.py aggiornato e config.py non aggiornato: il controller cercava `settings.cooling_load_low_4h_kwh`, assente nella versione di config.py attualmente deployata su Render.

Questa patch include sia il config.py corretto sia una protezione nel controller.py, così /api/dashboard non va in errore anche se per cache/deploy resta temporaneamente un config più vecchio.
