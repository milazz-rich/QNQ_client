# QNQ Backend

Backend FastAPI che orchestra misure comparative tra **HTTP/2 e HTTP/3** su tre
motori web (Caddy, OpenLiteSpeed, nginx), tre client di misura (curl, Chrome,
Firefox) e due ambienti di deploy (Docker, KVM). Espone un'API REST usata per
configurare, avviare e leggere i risultati di queste campagne di misura, che
vengono salvati su MongoDB.

Questa guida serve solo a far partire il progetto. Per capire come è fatto,
vedi [AGENTS.md](AGENTS.md).

## Prerequisiti

- **Python 3.11 o superiore.**
- **MongoDB** raggiungibile (locale o remoto). Il server deve poter connettersi
  a Mongo **all'avvio**: se Mongo non è raggiungibile, l'avvio fallisce.
- **Playwright**: la libreria si installa con `pip`, ma i browser (Chromium e
  Firefox) vanno scaricati con un comando separato — vedi il passo dedicato
  più sotto.
- **Un binario `curl` compilato con supporto HTTP/2 e HTTP/3.** Il `curl` di
  sistema tipicamente non ha HTTP/3: serve una build custom. Verifica quale
  `curl` stai per usare con:

  ```bash
  curl --version
  ```

  Nella riga `Features:` devono comparire sia `HTTP2` sia `HTTP3`. Se mancano,
  procurati (o compila) una build che li includa entrambi prima di configurare
  il progetto.

## Setup

Clona il repository:

```bash
git clone https://github.com/milazz-rich/QNQ_client.git
cd QNQ_client
```

Crea e attiva un virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Su Windows (PowerShell), il comando di attivazione è invece:

```bash
.venv\Scripts\Activate.ps1
```

Installa le dipendenze:

```bash
pip install -r requirements.txt
```

Installa i browser richiesti da Playwright (non li scarica `pip install`):

```bash
playwright install chromium firefox
```

Copia il file di configurazione:

```bash
cp .env.example .env
```

Poi apri `.env` e imposta almeno queste variabili, senza le quali il progetto
non parte o non misura correttamente:

- `MONGO_HOST` (oppure `MONGO_URI`, che ha la precedenza se valorizzata) —
  deve puntare a un'istanza MongoDB realmente raggiungibile. **Il server non
  si avvia se questa connessione fallisce.**
- `CURL_BINARY_PATH` — percorso del binario `curl` con supporto HTTP/2 e
  HTTP/3 verificato nei prerequisiti. Senza questo, il server si avvia
  comunque, ma ogni misura fatta con il client `curl` fallirà.

Tutte le altre variabili in `.env.example` hanno un default sensato e sono
opzionali per un primo avvio (certificati custom, timeout, CORS, ecc.).

Infine, assicurati che MongoDB sia in esecuzione e raggiungibile all'indirizzo
configurato **prima** di avviare il server (è un servizio esterno: questo
progetto non lo installa né lo avvia per te).

## Avvio

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Il server risponde su `http://localhost:8000`. La documentazione interattiva
delle API (Swagger UI) è su:

```
http://localhost:8000/docs
```

## Verifica che funzioni

```bash
curl http://127.0.0.1:8000/api/health
```

Se tutto è a posto, la risposta è:

```json
{"status":"ok","database":"connected","detail":null}
```

Se vedi `"status":"degraded"` e `"database":"disconnected"`, il server è
partito ma non riesce più a raggiungere MongoDB in quel momento: ricontrolla
`MONGO_HOST`/`MONGO_URI` in `.env`.

## Eseguire i test

```bash
python -m unittest discover -s tests
```

## Per saperne di più

Questa guida copre solo l'avvio. Per il modello dati, le convenzioni di
codice e le decisioni tecniche del progetto, vedi [AGENTS.md](AGENTS.md).
