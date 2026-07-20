# AGENTS.md — QNQ Client Backend

Backend dell'applicazione di confronto prestazionale **HTTP/2 vs HTTP/3**.
Espone un'API REST (FastAPI) consumata da un frontend Angular; persiste su MongoDB
tramite driver asincrono `motor`.

Questo documento è il contratto di riferimento per chiunque (umano o agente) lavori
sul repository: struttura, modello dati e convenzioni **precedono** il codice.

---

## 1. Stack

| Componente     | Scelta                                    |
| -------------- | ----------------------------------------- |
| Linguaggio     | Python 3.11+                              |
| Web framework  | FastAPI                                   |
| ASGI server    | Uvicorn                                   |
| Database       | MongoDB                                   |
| Driver DB      | `motor` (async), API `AsyncIOMotorClient` |
| Validazione    | Pydantic v2                               |
| Config         | `pydantic-settings` + file `.env`         |
| Frontend (dev) | Angular su `http://localhost:4200`        |

---

## 2. Struttura cartelle

```
.
├── AGENTS.md                  # questo documento
├── .env                       # config locale (NON versionato)
├── .env.example               # template versionato
├── requirements.txt
├── main.py                    # entrypoint di comodo: importa app.main:app
└── app/
    ├── main.py                # costruzione dell'app FastAPI (factory + lifespan)
    ├── core/
    │   ├── config.py          # Settings da .env (pydantic-settings)
    │   ├── cors.py            # configurazione CORS
    │   └── errors.py          # eccezioni applicative + exception handlers
    ├── db/
    │   ├── mongo.py           # ciclo di vita della connessione motor
    │   └── collections.py     # nomi delle collezioni (costanti)
    ├── models/                # Pydantic models, un file per entità + barrel
    │   ├── __init__.py        # barrel: riesporta tutti i modelli
    │   ├── common.py          # tipi condivisi (PyObjectId, MongoModel, ErrorResponse)
    │   ├── target.py
    │   ├── scenario.py
    │   ├── client.py
    │   ├── session_item.py
    │   ├── session.py
    │   └── result.py
    ├── routers/               # un file per entità, solo HTTP
    │   ├── __init__.py
    │   ├── health.py
    │   ├── targets.py
    │   ├── scenarios.py
    │   ├── clients.py
    │   ├── session_items.py
    │   ├── sessions.py
    │   └── results.py
    └── services/              # logica di business, zero dipendenze da FastAPI
        ├── __init__.py
        ├── targets_service.py
        ├── measurement/       # esecuzione delle misure HTTP/2 e HTTP/3
        │   ├── __init__.py
        │   ├── http2.py
        │   └── http3.py
        └── session_runner.py  # orchestrazione dell'esecuzione di una sessione
```

### Regola di stratificazione

```
routers/  →  services/  →  db/
```

* I **router** non parlano mai direttamente con MongoDB: validano l'input,
  chiamano un servizio, mappano il risultato in risposta HTTP.
* I **servizi** non importano nulla da `fastapi` (eccetto niente: solleveranno le
  eccezioni applicative definite in `app/core/errors.py`).
* Il **db layer** espone la `AsyncIOMotorDatabase`; nessuna logica di dominio.

### Stato attuale dell'implementazione

Implementato: config, connessione Mongo, CORS, error handling, `/api/health`,
CRUD completo per `targets`, `scenarios`, `clients`, `session_items` (con
`POST /session-items/batch` per la creazione in blocco del prodotto cartesiano
Target × Scenario generato dal wizard di sessione).
Da implementare nei prompt successivi: `sessions`, `results`,
`services/measurement`, `services/session_runner`.

---

## 3. Modello dati

### 3.1 Convenzione `_id` ⇄ `id`

MongoDB usa `_id` di tipo `ObjectId`. L'API espone sempre **`id` come stringa**.

* Il campo è definito una sola volta, in `MongoDocument` (`models/common.py`):
  `Field(alias="_id", serialization_alias="id")`. L'`alias` governa la
  **validazione** (i documenti arrivano dal driver con `_id`), il
  `serialization_alias` governa la **risposta HTTP** — senza il secondo, FastAPI
  (che serializza con `by_alias=True`) restituirebbe `_id` al client.
* L'`ObjectId` è convertito in stringa da un `BeforeValidator` sul tipo `MongoId`,
  che valida anche il formato (24 caratteri esadecimali).
* I modelli completi ereditano da `MongoDocument`: `class Target(TargetBase, MongoDocument)`.
* In scrittura: `id` non è mai accettato dal client (i modelli `*Create` /
  `*Update` non lo contengono); è Mongo a generarlo.
* I riferimenti fra entità (`targetId`, `scenarioId`, …) sono **stringhe** in API
  e `ObjectId` su Mongo, convertiti nel layer service.

### 3.2 Naming

* Campi JSON in **camelCase** (`targetId`, `currentIndex`, `scenarioPath`) —
  coerente con il client Angular.
* Nomi Python in `snake_case`, mappati con `alias`/`serialization_alias`.
* Le collezioni Mongo sono al plurale, lowercase: `targets`, `scenarios`,
  `clients`, `session_items`, `sessions`, `results`.

### 3.3 Entità

#### Target — collezione `targets`

Server sotto test.

| Campo      | Tipo                     | Vincoli                                       |
| ---------- | ------------------------ | --------------------------------------------- |
| `id`       | `str`                    | da `_id`, read-only                           |
| `name`     | `str`                    | 1–120 char, non vuoto                         |
| `host`     | `str`                    | 1–255 char, hostname o IP, senza schema `://` |
| `port`     | `int`                    | 1–65535                                       |
| `protocol` | `"HTTP/2" \| "HTTP/3"`   | enum                                          |
| `maxc`     | `int`                    | ≥ 1, concorrenza massima supportata           |
| `status`   | `"online"\|"idle"\|"offline"` | enum, default `offline`                  |
| `latency`  | `float`                  | ≥ 0, millisecondi, default `0`                |

#### Scenario — collezione `scenarios`

Percorso/payload da richiedere al target.

| Campo  | Tipo  | Vincoli                        |
| ------ | ----- | ------------------------------ |
| `id`   | `str` | da `_id`                       |
| `name` | `str` | 1–120 char                     |
| `path` | `str` | deve iniziare con `/`          |
| `desc` | `str` | descrizione, ≤ 500 char        |
| `tag`  | `str` | etichetta breve, ≤ 40 char     |

#### Client — collezione `clients`

Agente che esegue le misure.

| Campo  | Tipo  | Vincoli    |
| ------ | ----- | ---------- |
| `id`   | `str` | da `_id`   |
| `name` | `str` | 1–120 char |

#### SessionItem — collezione `session_items`

Unità di lavoro configurata: "misura *questo* scenario su *questo* target".

| Campo        | Tipo                | Vincoli                              |
| ------------ | ------------------- | ------------------------------------ |
| `id`         | `str`               | da `_id`                             |
| `targetId`   | `str`               | riferimento a `targets._id`          |
| `scenarioId` | `str`               | riferimento a `scenarios._id`        |
| `clientId`   | `str`               | riferimento a `clients._id`          |
| `reps`       | `int`               | ≥ 1, numero di ripetizioni           |
| `conn`       | `"reuse" \| "new"`  | riuso connessione o nuova per rep    |
| `timeout`    | `int`               | ms, ≥ 1                              |

#### Session — collezione `sessions`

Esecuzione di un insieme ordinato di `SessionItem`.

| Campo          | Tipo                                     | Vincoli                             |
| -------------- | ---------------------------------------- | ----------------------------------- |
| `id`           | `str`                                    | da `_id`                            |
| `name`         | `str`                                    | 1–120 char                          |
| `when`         | `datetime`                               | UTC, ISO-8601                       |
| `status`       | `"pending"\|"running"\|"completed"`      | default `pending`                   |
| `currentIndex` | `int`                                    | ≥ 0, indice dell'item in esecuzione |
| `items`        | `SessionProgressItem[]`                  | embedded, vedi sotto                |

`SessionProgressItem` (documento embedded, non una collezione):

| Campo           | Tipo                                | Vincoli                        |
| --------------- | ----------------------------------- | ------------------------------ |
| `sessionItemId` | `str`                               | riferimento a `session_items`  |
| `label`         | `str`                               | etichetta leggibile            |
| `proto`         | `"HTTP/2" \| "HTTP/3"`              | protocollo richiesto           |
| `total`         | `int`                               | ≥ 0, ripetizioni previste      |
| `done`          | `int`                               | ≥ 0, ≤ `total`                 |
| `status`        | `"pending"\|"running"\|"completed"` | stato dell'item                |

#### Result — collezione `results`

Esito di una singola ripetizione.

| Campo           | Tipo                       | Vincoli                                  |
| --------------- | -------------------------- | ---------------------------------------- |
| `id`            | `str`                      | da `_id`                                 |
| `sessionItemId` | `str`                      | riferimento a `session_items`            |
| `idx`           | `int`                      | ≥ 0, indice della ripetizione            |
| `target`        | `str`                      | snapshot leggibile del target            |
| `scenarioPath`  | `str`                      | snapshot del path richiesto              |
| `proto`         | `"HTTP/2" \| "HTTP/3"`     | protocollo richiesto                     |
| `actualProto`   | `str`                      | protocollo effettivamente negoziato      |
| `total`         | `float`                    | ms, ≥ 0, durata totale                   |
| `ttfb`          | `float`                    | ms, ≥ 0, time-to-first-byte              |
| `kb`            | `float`                    | ≥ 0, kilobyte trasferiti                 |
| `status`        | `"completed" \| "failed"`  | esito                                    |
| `time`          | `datetime`                 | UTC, istante di completamento            |

I campi `target` e `scenarioPath` sono **denormalizzati di proposito**: un
risultato deve restare leggibile anche se il target o lo scenario vengono
modificati o eliminati dopo la misura.

### 3.4 Pattern dei modelli Pydantic

Per ogni entità, tre modelli nello stesso file:

* `XxxCreate` — payload di `POST`, tutti i campi obbligatori tranne quelli con default.
* `XxxUpdate` — payload di `PUT`/`PATCH`, tutti i campi opzionali (`None` = non toccare).
* `Xxx` — rappresentazione completa restituita dall'API, include `id`.

Tutti ereditano da `MongoModel` (in `models/common.py`) che imposta
`populate_by_name=True`, `str_strip_whitespace=True` ed `extra="forbid"`;
i modelli completi ereditano inoltre da `MongoDocument`, che aggiunge `id`.

`extra="forbid"` è deliberato: un campo scritto male dal client deve produrre un
`422` esplicito, non essere silenziosamente ignorato.

---

## 4. Convenzioni di codice

### 4.1 Docstring

**Ogni** funzione di router e di service ha una docstring che dichiara
esplicitamente *cosa riceve*, *cosa restituisce*, *cosa fa*:

```python
async def get_target(target_id: str) -> Target:
    """Recupera un singolo target per identificativo.

    Riceve:
        target_id: identificativo del target come stringa esadecimale a 24 caratteri.

    Restituisce:
        Il modello ``Target`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId``, interroga la collezione ``targets`` e
        solleva ``NotFoundError`` se il documento non esiste.
    """
```

Formato: prima riga imperativa e sintetica, poi riga vuota, poi le tre sezioni
`Riceve:` / `Restituisce:` / `Fa:`. Documentare anche le eccezioni sollevate
dentro `Fa:`.

### 4.2 Stile

* Type hints **obbligatori** su ogni firma pubblica (parametri e ritorno).
* `async def` per tutto ciò che tocca il database o la rete.
* Import assoluti dal package `app` (`from app.core.config import settings`).
* Nessun `print`: usare il logger (`logging.getLogger(__name__)`).
* Nessun segreto o indirizzo hardcoded: tutto da `Settings` (`.env`).
* Righe ≤ 100 caratteri.
* Nomi in inglese nel codice; commenti e docstring in italiano.

### 4.3 Router

* Prefisso globale `/api`, definito una sola volta in `app/main.py`.
* Ogni router dichiara il proprio `prefix` (es. `/targets`) e `tags`.
* `response_model` **sempre** esplicito.
* Codici di stato: `200` GET/PUT, `201` POST, `204` DELETE.
* I router non contengono `try/except` per errori di dominio: sollevano
  eccezioni applicative e lasciano gestire agli handler centralizzati.

### 4.4 CORS

Origini consentite lette da `settings.cors_origins` (default
`http://localhost:4200`). Credenziali abilitate; metodi e header liberi.

### 4.5 Gestione errori centralizzata

Tutte le risposte di errore hanno la **stessa forma**:

```json
{
  "error": {
    "code": "NOT_FOUND",
    "message": "Target 665f1c2e8a1b2c3d4e5f6a7b non trovato.",
    "details": null
  }
}
```

* `code`: stringa stabile in `SCREAMING_SNAKE_CASE`, pensata per il client.
* `message`: messaggio leggibile.
* `details`: `null` oppure oggetto/array con informazioni aggiuntive
  (es. la lista degli errori di validazione Pydantic).

Gerarchia in `app/core/errors.py`:

| Eccezione            | HTTP | `code`                |
| -------------------- | ---- | --------------------- |
| `NotFoundError`      | 404  | `NOT_FOUND`           |
| `ValidationError`    | 422  | `VALIDATION_ERROR`    |
| `ConflictError`      | 409  | `CONFLICT`            |
| `DatabaseError`      | 503  | `DATABASE_UNAVAILABLE`|
| *(non gestita)*      | 500  | `INTERNAL_ERROR`      |

Gli handler sono registrati in `register_exception_handlers(app)` e coprono
anche `RequestValidationError` di FastAPI e `HTTPException`, così che **nessuna**
risposta di errore sfugga al formato comune.

### 4.6 Configurazione

`app/core/config.py` definisce `Settings` (pydantic-settings) letto da `.env`:

| Variabile        | Default                        | Significato                          |
| ---------------- | ------------------------------ | ------------------------------------ |
| `MONGO_HOST`     | `172.17.32.1`                  | gateway WSL→Windows, **cambia** al riavvio |
| `MONGO_PORT`     | `27017`                        | porta MongoDB                        |
| `MONGO_DB`       | `qnq`                          | nome del database                    |
| `MONGO_URI`      | *(vuoto)*                      | se valorizzata, vince su host/porta  |
| `CORS_ORIGINS`   | `http://localhost:4200`        | lista separata da virgole (vedi nota)|
| `API_PREFIX`     | `/api`                         | prefisso delle rotte                 |
| `APP_ENV`        | `development`                  | `development` \| `production`        |

`CORS_ORIGINS` è annotato `NoDecode`: senza, pydantic-settings tenterebbe di
interpretare il valore come JSON prima dei validator e `CORS_ORIGINS=http://a`
farebbe fallire l'avvio con un `JSONDecodeError`.

L'indirizzo `172.17.32.1` è il gateway della rete WSL verso l'host Windows e
**non è stabile fra riavvii**. Se il backend non raggiunge più Mongo, rileggerlo
con `ip route show | grep default` dentro WSL e aggiornare `MONGO_HOST` in `.env`.

### 4.7 Health check

`GET /api/health` esegue un `ping` reale sul database e restituisce:

```json
{ "status": "ok", "database": "connected", "detail": null }
```

Se il ping fallisce risponde `503` con `status: "degraded"` e
`database: "disconnected"`. L'endpoint non deve mai sollevare eccezioni non
gestite: è usato dagli health probe.

---

## 5. Comandi

```bash
# Setup
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate su WSL
pip install -r requirements.txt
cp .env.example .env            # poi correggere MONGO_HOST

# Avvio (dev, con reload)
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Documentazione interattiva
# http://localhost:8000/docs
```

---

## 6. Aggiungere una nuova entità (checklist)

1. `app/models/<entita>.py` con `XxxCreate`, `XxxUpdate`, `Xxx`.
2. Riesportare in `app/models/__init__.py` (barrel).
3. Nome collezione in `app/db/collections.py`.
4. `app/services/<entita>_service.py` con le operazioni di dominio + docstring.
5. `app/routers/<entita>.py` con il CRUD, `response_model` espliciti + docstring.
6. Registrare il router in `app/main.py`.
7. Aggiornare la sezione 3 di questo documento.
