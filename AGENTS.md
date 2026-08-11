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
        ├── scenarios_service.py
        ├── clients_service.py
        ├── session_items_service.py
        ├── sessions_service.py
        ├── results_service.py
        ├── measurement/       # esecuzione delle misure HTTP/2 e HTTP/3
        │   ├── __init__.py
        │   ├── curl_client.py   # motore "curl": processo esterno + parsing -w
        │   ├── chrome_client.py # motore "chrome": Playwright + CDP
        │   └── runner.py        # entità del dominio → motore → Result
        └── session_runner.py  # orchestrazione dell'esecuzione di una sessione
```

> **Nota sulla struttura di `measurement/`.** Una prima stesura di questo
> documento prevedeva `http2.py` e `http3.py` separati. Non è così: nella pratica
> il protocollo è **un solo flag di curl** (`--http2` / `--http3`), quindi due
> moduli sarebbero stati identici a meno di una riga. La differenza è confinata
> in `curl_client._protocol_flag`. La divisione reale è **per motore di misura**
> (`curl_client`, `chrome_client`: ciascuno parla con il proprio processo
> esterno) e **per responsabilità** (`runner` parla con il dominio e sceglie il
> motore, senza sapere come questo lavori). I motori espongono lo stesso
> contratto `measure(url, protocol, timeout_ms) -> Measurement` e sono
> registrati in `runner.MEASUREMENT_BACKENDS`.

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
`POST /session-items/batch`), `sessions` (con `POST /sessions/{id}/start`),
lettura filtrata di `results`, il session runner in background e **due motori
di misura**: `curl` (§5.2) e `chrome` (§5.6).

Non ancora implementato: client diversi da `curl` e `chrome` (es. Firefox), che
sollevano `NOT_IMPLEMENTED`; interruzione di una sessione già avviata.

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
| `status`   | `"online"\|"idle"\|"offline"` | enum, default `offline`                  |
| `tag`      | `str`                    | etichetta breve, ≤ 40 char, default `""`      |

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
| `timeout`    | `int`               | ms, ≥ 1                              |

#### Session — collezione `sessions`

Esecuzione di un insieme ordinato di `SessionItem`.

| Campo          | Tipo                                     | Vincoli                             |
| -------------- | ---------------------------------------- | ----------------------------------- |
| `id`           | `str`                                    | da `_id`                            |
| `name`         | `str`                                    | 1–120 char                          |
| `when`         | `datetime`                               | UTC, ISO-8601                       |
| `status`       | `"pending"\|"running"\|"completed"\|"failed"` | default `pending`; `failed` se almeno un item termina `failed` |
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
| `status`        | `"pending"\|"running"\|"completed"\|"failed"` | stato dell'item; `failed` = mai misurato (config rotta) |

#### Result — collezione `results`

Esito di una singola ripetizione.

| Campo           | Tipo                       | Vincoli                                  |
| --------------- | -------------------------- | ---------------------------------------- |
| `id`            | `str`                      | da `_id`                                 |
| `sessionId`     | `str`                      | riferimento a `sessions`; la sessione che ha prodotto la misura |
| `sessionItemId` | `str`                      | riferimento a `session_items`            |
| `targetId`      | `str`                      | riferimento a `targets`; il target su cui è stata eseguita la misura |
| `clientId`      | `str`                      | riferimento a `clients`; il motore che ha prodotto la misura |
| `idx`           | `int`                      | ≥ 0, indice della ripetizione            |
| `target`        | `str`                      | snapshot leggibile del target            |
| `scenarioPath`  | `str`                      | snapshot del path richiesto              |
| `proto`         | `"HTTP/2" \| "HTTP/3"`     | protocollo richiesto                     |
| `actualProto`   | `"HTTP/2" \| "HTTP/3" \| null` | protocollo negoziato; valorizzato **solo** se `status="completed"`, altrimenti `null` |
| `total`         | `float`                    | ms, ≥ 0, durata totale                   |
| `ttfb`          | `float`                    | ms, ≥ 0, time-to-first-byte              |
| `kb`            | `float`                    | ≥ 0, kilobyte trasferiti                 |
| `status`        | `"completed" \| "failed"`  | esito                                    |
| `time`          | `datetime`                 | UTC, istante di completamento            |

I campi `target` e `scenarioPath` sono **denormalizzati di proposito**: un
risultato deve restare leggibile anche se il target o lo scenario vengono
modificati o eliminati dopo la misura.

**`sessionId` vs `sessionItemId`.** Entrambi sono riferimenti, ma rispondono a
domande diverse. `sessionItemId` dice *quale configurazione* (target+scenario)
ha prodotto la misura; `sessionId` dice *quale esecuzione* l'ha prodotta. La
distinzione è necessaria perché **lo stesso `SessionItem` può essere condiviso
fra più sessioni**: un rilancio o una riproposizione riusano lo stesso
`SessionItem`, quindi filtrare o cancellare i risultati per solo `sessionItemId`
toccherebbe anche misure di altre sessioni ancora esistenti. Solo `sessionId`
identifica senza ambiguità i risultati di una singola esecuzione — è ciò su cui
si basano la cancellazione a cascata (§5.5) e il filtro preferito su
`GET /api/results` (§5.5).

**`targetId` vs `target`.** Stessa distinzione fra riferimento e snapshot già
vista per `sessionItemId`/`target` (denormalizzazione, sopra): `targetId` è il
riferimento diretto a `targets._id`, usato per raggruppare o filtrare i
risultati per target; `target` resta lo snapshot leggibile (`"nome (host:porta)"`),
denormalizzato apposta per restare comprensibile anche se il target viene
rinominato o cancellato. `targetId` è popolato da `measurement.runner` (per le
misure reali, dal `Target` già risolto) e da `session_runner` (per i `Result`
segnaposto degli item saltati, recuperandolo dal `SessionItem` di
configurazione) — mai dal client HTTP.

**`clientId`.** Stessa logica di `targetId`, applicata al motore di misura:
riferimento esplicito a `clients._id`, popolato dagli stessi due punti e con lo
stesso fallback sul `SessionItem`. Esiste perché il confronto **curl vs Chrome**
(§5.6) è una dimensione di analisi di prima classe: senza questa FK andrebbe
ricostruito risalendo `Result → SessionItem → Client`, con lo stesso rischio di
matching ambiguo già visto per il target. È anche il filtro `?clientId=` di
`GET /api/results`.

> **Nota di migrazione.** `targetId` e `clientId` sono **obbligatori**: i
> `Result` scritti prima della loro introduzione non li avevano e la loro
> validazione fallirebbe. Entrambe le volte i documenti esistenti sono stati
> aggiornati con un backfill idempotente che ricava il valore dal `SessionItem`
> referenziato. Se in futuro si aggiungesse un altro riferimento obbligatorio a
> `Result`, va previsto lo stesso passaggio prima di rimettere in servizio l'API.

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
| `NotImplementedFeatureError` | 501 | `NOT_IMPLEMENTED` |
| `DatabaseError`      | 503  | `DATABASE_UNAVAILABLE`|
| *(non gestita)*      | 500  | `INTERNAL_ERROR`      |

`NotImplementedFeatureError` è deliberatamente distinta da un errore generico:
serve a dire "il dominio prevede questo caso, il codice non ancora" — oggi vale
per i client diversi da `curl` e `chrome` (es. Firefox), cioè quelli assenti da
`runner.MEASUREMENT_BACKENDS`. Il frontend può così spiegare la situazione
all'utente invece di mostrare un errore opaco.

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
| `CURL_BINARY_PATH` | `~/curl/src/curl`            | binario curl con HTTP/2 **e** HTTP/3 |
| `CURL_KILL_GRACE_MS` | `2000`                     | margine prima di uccidere il processo|
| `CURL_CA_BUNDLE_PATH` | *(vuoto)*                | opzionale, certificato CA custom (`--cacert`) |
| `CHROME_CERT_SPKI_HASH` | *(vuoto)*              | hash SPKI dei certificati self-signed fidati da Chrome; lista separata da virgole |
| `CHROME_WAIT_UNTIL`  | `load`                        | `load` \| `commit` \| `domcontentloaded` |

Il binario di default è una build custom di curl: quello di sistema in genere
**non** ha HTTP/3. Verificare con `curl --version` che la riga `Features:`
contenga sia `HTTP2` sia `HTTP3`. La `~` è espansa dall'applicazione
(`settings.curl_path`): `subprocess` non passa dalla shell e non la
espanderebbe da sola.

`CURL_CA_BUNDLE_PATH` serve per i target con certificato **self-signed** o
emesso da una CA privata (es. un server di test come `milaz.it`): senza,
curl rifiuta la connessione con `SSL certificate problem: self-signed
certificate` e la misura fallisce prima ancora di partire. Se valorizzata,
`curl_client.build_command` aggiunge `--cacert <path>` (tilde espansa allo
stesso modo di `CURL_BINARY_PATH`, tramite `settings.curl_ca_bundle`).
Deliberatamente **non** si usa `-k`/`--insecure`: quello disabiliterebbe la
verifica TLS per qualunque target, nascondendo anche problemi reali (es. un
certificato scaduto su un target di produzione); `--cacert` estende invece
l'insieme di CA fidate con una aggiuntiva, mantenendo la verifica attiva.

`CHROME_CERT_SPKI_HASH` è l'equivalente di `CURL_CA_BUNDLE_PATH` per il motore
Chrome, ma **non** è intercambiabile: Chrome non accetta un file di
certificato, vuole l'hash SHA-256 (base64) della `SubjectPublicKeyInfo`. Si
calcola dal certificato con:

```bash
openssl x509 -in CERT.crt -pubkey -noout | openssl pkey -pubin -outform der \
  | openssl dgst -sha256 -binary | openssl enc -base64
```

È modellata come **lista separata da virgole** (stesso trattamento `NoDecode`
di `CORS_ORIGINS`) anche se oggi contiene un solo valore: ambienti diversi
(Docker, KVM) usano certificati diversi, e Chrome accetta nativamente più hash
nello stesso flag, quindi l'estensione non richiederà modifiche al codice.
Perché sia obbligatorio proprio questo flag — e non il più ovvio
`--ignore-certificate-errors` — vedi §5.6.

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

## 5. Esecuzione delle misurazioni

### 5.1 Flusso

```
POST /api/sessions/{id}/start
  → status = running, risposta 202 immediata
  → BackgroundTask: session_runner.start_session
      per ogni item (in SEQUENZA):
        currentIndex = i, item.status = running, item.total = SessionItem.reps
        risolve Target / Scenario / Client   (measurement.runner.resolve_context)
        cancella i Result di una precedente run DI QUESTA sessione (sessionId+item)
        per ogni ripetizione:
          curl → Result (con sessionId = questa sessione) salvato → item.done += 1
        item.status = completed SE tutte le ripetizioni hanno prodotto Result "completed"
        item.status = failed    SE almeno una ripetizione ha prodotto Result "failed"
        (se la risoluzione fallisce in modo irrecuperabile PRIMA di eseguire
         qualunque ripetizione: item.status = failed, viene comunque salvato
         un Result "failed" segnaposto — vedi §5.4)
      status = completed SE tutti gli item sono completed
      status = failed    SE almeno un item è failed
```

Le misure sono **sequenziali per scelta**: due richieste concorrenti si
contenderebbero la banda e falserebbero il confronto fra HTTP/2 e HTTP/3, che è
l'unica cosa che questa applicazione deve misurare.

Lo stato finale della sessione riflette l'esito reale dell'esecuzione, non solo
il fatto che sia arrivata in fondo: `session_runner._run_items` tiene traccia di
se **almeno un item** è terminato `failed` e restituisce l'esito complessivo a
`start_session`, che imposta `status="completed"` solo se **tutti** gli item
sono `completed`, altrimenti `status="failed"`. Una sessione `failed` ha
comunque eseguito (e tracciato) tutti i suoi item — nessuno viene saltato per
via del fallimento di un altro — semplicemente il suo esito complessivo non è
positivo. Anche un'eccezione imprevista *prima* che gli item vengano eseguiti
(bug, non un fallimento di dominio) porta la sessione a `failed`, mai a
`completed`: non c'è garanzia che l'esecuzione sia avvenuta correttamente.

`done` è incrementato sul database dopo *ogni* ripetizione, con update mirato
`items.<i>.done` (non riscrittura dell'array): è ciò che permette al polling del
frontend su `GET /api/sessions/{id}` di mostrare l'avanzamento in tempo reale.

### 5.2 Comando curl

```
<curl> -s -S -o /dev/null (--http2|--http3) --max-time <timeout> -w <json> --no-keepalive <url>
```

* L'URL è sempre `https://host:port/path`: HTTP/3 richiede TLS per definizione
  (gira su QUIC) e HTTP/2 lo richiede nella pratica.
* `-o /dev/null` evita che la scrittura su disco falsi i tempi.
* `-w` produce una riga JSON con `http_version`, `response_code`, `time_total`,
  `time_starttransfer`, `size_download`. curl riporta secondi e byte; il modello
  `Result` usa **millisecondi e kilobyte**, la conversione è in `_to_measurement`.
* `--no-keepalive` è **sempre** presente, incondizionatamente: ogni ripetizione
  è un'invocazione di `curl` a sé stante (un processo per rep), quindi non
  esiste connessione da riusare fra una ripetizione e l'altra. Il flag rende
  esplicito nella riga di comando ciò che è già vero nei fatti — vedi la nota
  metodologica sotto — invece di lasciarlo implicito. Per questo motivo
  `SessionItem` **non ha un campo `conn`**: la scelta fra "riusa connessione" e
  "nuova connessione per rep" non esiste nella pratica attuale, quindi non è
  stata modellata.
* Gli argomenti sono passati come **lista**, mai come stringa di shell: host e
  path arrivano dal database e non devono poter essere interpretati come comandi.

**Nota metodologica.** Ogni misura include **sempre** l'overhead completo di
handshake (TCP/QUIC + TLS) perché non c'è mai riuso di connessione fra
ripetizioni: la ripetizione N+1 non eredita nulla dalla N, essendo un processo
`curl` distinto. Chi legge i `Result` per confrontare HTTP/2 e HTTP/3 deve
tenerne conto: `total` e `ttfb` misurano sempre una connessione "a freddo", non
il caso (spesso più realistico in produzione) di richieste su una connessione
già aperta. Questo è coerente fra i due protocolli — nessuno dei due beneficia
di riuso — quindi non falsa il confronto relativo, ma va ricordato se questi
numeri vengono confrontati con misure esterne che invece riusano la connessione.

### 5.3 Fallback di protocollo

`--http2` e `--http3` **non sono vincolanti**: chiedono il protocollo ma
accettano quello che il server negozia. Il fallback fra HTTP/2 e HTTP/3 (i due
protocolli che l'applicazione confronta) resta una misura valida; un fallback
su **HTTP/1.1** (o un `http_version` non determinabile) no, perché non
rappresenta nessuno dei due protocolli sotto confronto: viene trattato come
**fallimento della misura**, non come dato valido con protocollo diverso.
Verificato empiricamente:

| Richiesta   | Server                  | `http_version` | Esito                         |
| ----------- | ----------------------- | -------------- | ----------------------------- |
| `--http2`   | `www.google.com`        | `2`            | `completed`, `actualProto` = HTTP/2 |
| `--http3`   | `www.google.com`        | `3`            | `completed`, `actualProto` = HTTP/3 |
| `--http2`   | `ftp.gnu.org` (no h2)   | `1.1`          | `failed`, `actualProto` = `null` |
| `--http3`   | `ftp.gnu.org` (no QUIC) | `1.1`          | `failed`, `actualProto` = `null` |

Il campo `proto` di `Result` conserva **sempre** il protocollo richiesto.
`actualProto` è valorizzato **solo** quando `status="completed"`, e in quel
caso è sempre HTTP/2 o HTTP/3 — non può contenere HTTP/1.1 né altri valori:
se il protocollo negoziato non è uno dei due, l'intero `Result` è `failed` e
`actualProto` resta `null`. Questo evita l'errore opposto rispetto a prima:
un confronto che leggesse `total`/`ttfb` senza controllare `status` non può
più scambiare per dati validi una richiesta caduta su HTTP/1.1.

Esiste `--http3-only` per la modalità strict (fallire invece di ripiegare): non
è usato, perché il requisito è **rilevare** il fallback fra HTTP/2 e HTTP/3
(tramite `actualProto`), non impedirlo — mentre un fallback fuori da questi
due protocolli è comunque un fallimento, rilevato tramite `status="failed"`.

### 5.4 Fallimenti

Una misura fallita non interrompe mai l'esecuzione: produce un `Result` con
`status="failed"`, tempi a zero e `actualProto=null`. Sono trattati così:

* curl esce con codice ≠ 0 (connessione rifiutata, DNS, TLS, `--max-time` scaduto);
* curl esce con 0 ma `response_code` è 0 (nessuna risposta);
* curl riceve una risposta ma il protocollo negoziato non è HTTP/2 né HTTP/3
  (fallback su HTTP/1.1, o `http_version` non riconosciuto) — vedi §5.3;
* l'output di `-w` non è JSON interpretabile;
* il binario curl non esiste al path configurato;
* il processo non termina entro `--max-time` + `CURL_KILL_GRACE_MS`: viene
  ucciso e atteso, per non lasciare zombie né bloccare la sessione.

**L'esito delle ripetizioni si propaga allo stato dell'item.** `done == total`
non basta più a rendere un item `completed`: se **almeno una** delle sue
ripetizioni ha prodotto un `Result` con `status="failed"` (per una qualunque
delle ragioni sopra), l'intero item termina `status="failed"`, anche se ha
eseguito regolarmente tutte le `reps` previste. Solo se **tutte** le
ripetizioni sono `completed` l'item risulta `completed`. La ragione è la
stessa che vale per la sessione (sotto): un item il cui unico esito è "ho
tentato" senza aver prodotto nemmeno una misura valida non deve poter essere
scambiato per un dato affidabile solo perché `done` ha raggiunto `total`. Un
mix di ripetizioni riuscite e fallite nello stesso item conta comunque come
`failed` — basta *una sola* ripetizione fallita.

Allo stesso modo, un item con configurazione rotta (riferimento inesistente,
client non supportato) viene registrato nei log e saltato **prima di eseguire
qualunque ripetizione**: una configurazione sbagliata su un item non deve
invalidare l'intera sessione. A differenza delle ripetizioni fallite sopra
(che *sono* state eseguite, solo con esito negativo), qui l'item non è mai
stato misurato: `item.status` diventa `failed` allo stesso modo (non
`completed`, per non farlo contare come dato valido nelle statistiche) e
viene comunque salvato un `Result` con `status="failed"`, tempi a zero e
`actualProto=null`, usando `item.label` e il messaggio d'errore al posto dei
campi denormalizzati abituali (`target`/`scenarioPath`) che qui non sono
disponibili — così il fallimento resta tracciato invece di sparire
silenziosamente. `targetId` (obbligatorio in `Result`) viene recuperato da
`session_runner` rileggendo il `SessionItem` di configurazione. **Unico caso
limite**: se anche il `SessionItem` referenziato non esiste più (cancellato
dopo essere stato incluso in questa sessione), non c'è alcun `targetId` da cui
costruire un `Result` valido; in quel caso specifico l'item viene comunque
marcato `failed` e l'errore resta nei log applicativi, ma **nessun `Result`**
viene creato per quell'item.

In sintesi, un item termina `failed` in due casi distinti — mai eseguito
(configurazione rotta) oppure eseguito ma senza successo (una o più
ripetizioni fallite) — e in entrambi si applica la stessa regola di
propagazione verso la sessione: **un solo item `failed` porta l'intera
sessione a `failed`** (§5.1).

### 5.5 Cancellazione di una sessione (cascata sui Result)

`DELETE /api/sessions/{id}` cancella la sessione **e**, a cascata, tutti i suoi
`Result`. Il filtro sui risultati è per `sessionId`, **mai** per `sessionItemId`
(vedi §3.3): lo stesso `SessionItem` può appartenere a più sessioni, e filtrare
per `sessionItemId` cancellerebbe misure di sessioni ancora esistenti.

MongoDB gira qui **standalone**, che non supporta le transazioni multi-documento:
la coerenza è garantita dall'**ordine**, non da una transazione. La cancellazione
avviene in `sessions_service.delete_session` in due passi:

1. `delete_many` dei `Result` con quel `sessionId`;
2. `delete_one` della sessione (→ `404 NOT_FOUND` se non esisteva).

I risultati sono cancellati **prima** della sessione di proposito: se il passo 2
fallisce, la sessione resta e l'operazione è ripetibile; l'ordine inverso
lascerebbe `Result` orfani non più raggiungibili. Nel caso normale di sessione
inesistente non esistono risultati con quel `sessionId`, quindi il passo 1 è un
no-op e il `404` è sollevato correttamente dal passo 2.

> Nota: la pulizia pre-run in `session_runner` (§5.1, "cancella i Result di una
> precedente run") usa lo stesso principio ma con filtro `sessionId`+`sessionItemId`,
> per non cancellare i risultati di altre sessioni al rilancio.

Sul versante lettura vedi §5.7.

### 5.6 Motore di misura "chrome" (Playwright + CDP)

Il secondo motore di misura, selezionabile creando un `Client` di nome
`Chrome`. Guida **Chromium headless** via Playwright e legge i timing dagli
eventi del dominio *Network* del Chrome DevTools Protocol. Espone lo stesso
contratto di `curl_client` — `measure(url, protocol, timeout_ms)` →
`Measurement` — ed è registrato in `runner.MEASUREMENT_BACKENDS`.

#### Flag per protocollo

A differenza di curl, il protocollo non è un flag della richiesta ma va imposto
al **processo browser** all'avvio, quindi ogni misura richiede un browser nuovo:

| Protocollo | Flag |
| ---------- | ---- |
| HTTP/2     | `--disable-quic` |
| HTTP/3     | `--enable-quic --origin-to-force-quic-on=<host>:<porta>` |
| *sempre*   | `--ignore-certificate-errors-spki-list=<hash>`, `--no-sandbox` |

Tre dettagli verificati empiricamente, tutti non ovvi:

* **`--ignore-certificate-errors` NON funziona.** Il flag generico fa fallire
  l'handshake QUIC con `ERR_QUIC_PROTOCOL_ERROR`: HTTP/3 su un target
  self-signed è misurabile **solo** con `--ignore-certificate-errors-spki-list`
  (da `CHROME_CERT_SPKI_HASH`, §4.6). Anche `ignore_https_errors` del context
  Playwright non basta: agisce via CDP a livello di pagina, non sull'handshake.
* **L'origine per QUIC deve includere la porta.** Con `--origin-to-force-quic-on`
  senza porta esplicita Chrome **ignora in silenzio** la forzatura e usa HTTP/2:
  si otterrebbe una misura del protocollo sbagliato invece di un errore.
  `chrome_client.build_browser_args` normalizza quindi a `host:porta`.
* **La scoperta automatica di h3 non è affidabile.** Il target annuncia
  `alt-svc: h3=":443"` anche quando lo si interroga su `:8444`; senza forzatura
  esplicita Chrome resta su HTTP/2.

Si usa il **Chrome completo** (`channel="chromium"`), non l'`headless-shell`:
è la build con lo stack QUIC pieno.

#### Fallimenti: due percorsi distinti

Il fallback si comporta in modo **asimmetrico**, diversamente da curl:

| Forzatura | Server che non parla quel protocollo | Comportamento |
| --------- | ------------------------------------ | ------------- |
| HTTP/2    | http/1.1-only | **fallback silenzioso**: risponde `200` con `protocol="http/1.1"` |
| HTTP/3    | senza QUIC    | **eccezione**: `ERR_QUIC_PROTOCOL_ERROR`, nessuna risposta |

Entrambi producono `status="failed"` ma per vie diverse: il primo dal controllo
sul protocollo negoziato (stessa regola di §5.3 — `protocol` ∉ {`h2`, `h3`} ⇒
misura non valida), il secondo dalla cattura dell'eccezione di navigazione.
Anche l'assenza del browser o delle sue librerie di sistema produce un
fallimento strutturato, mai un crash.

#### Dati estratti dal CDP

Si filtra l'evento con `type == "Document"`: un browser carica anche le
sotto-risorse della pagina, che non vanno confuse con la misura del documento.

| Campo `Result` | Origine CDP |
| -------------- | ----------- |
| `actualProto`  | `response.protocol` (`h2`/`h3` → HTTP/2 / HTTP/3) |
| `ttfb`         | `response.timing.receiveHeadersStart` (già in ms) |
| `total`        | `(loadingFinished.timestamp − timing.requestTime) × 1000` |
| `kb`           | `loadingFinished.encodedDataLength / 1024` |

`response.timing` espone anche la scomposizione completa dell'handshake
(`dnsStart/End`, `connectStart/End`, `sslStart/End`, …), più granulare di quanto
offra curl: su HTTP/3 `connectStart == sslStart`, perché in QUIC trasporto e
crittografia sono lo stesso handshake. Non è oggi mappata su `Result`, ma è
disponibile se servisse un'analisi più fine.

#### Confrontabilità con curl — attenzione

* **I byte NON sono comparabili 1:1.** curl riporta `size_download`, cioè il
  **corpo** della risposta; Chrome riporta `encodedDataLength`, che **include
  gli header compressi**. Sullo stesso documento da 232 B curl misura 232 B,
  Chrome ~392 B su HTTP/2 e ~332 B su HTTP/3 — e la differenza fra i due
  protocolli riflette HPACK vs QPACK, non una diversa quantità di dati utili.
  Confrontare i `kb` fra client diversi è quindi privo di significato; il
  confronto valido resta **fra protocolli a parità di client**.
* **Il costo per misura è molto maggiore.** Avviare e chiudere un browser
  completo costa **~1–2 s** contro i ~20 ms della richiesta vera (misurato:
  4 misure in ~3 s end-to-end). Le sessioni con molte ripetizioni diventano
  sensibilmente più lente che con curl. È il prezzo della fedeltà al
  comportamento di un browser reale.
* **Più varianza.** L'avvio del processo browser introduce rumore assente in
  curl; conviene aumentare `reps` per compensare.

#### Nota metodologica: `wait_until="load"`

`CHROME_WAIT_UNTIL` decide quando la navigazione è considerata conclusa, e
quindi **cosa** si sta misurando:

* **`load` (default, scelta deliberata)** — attende anche le sotto-risorse
  della pagina. È il punto in cui il motore Chrome dice qualcosa che curl non
  può dire: come si comporta il protocollo su una pagina **con più risorse**,
  dove il multiplexing di HTTP/2 e HTTP/3 conta davvero. Con una sola richiesta
  isolata i due protocolli si distinguono poco; è il caricamento completo a
  renderne visibile la differenza. È il motivo per cui questo motore esiste
  accanto a curl.
* `commit` / `domcontentloaded` — misurano il solo documento, più vicino a curl
  e utile per un confronto diretto fra i due client, ma rinunciano proprio a ciò
  che il browser aggiunge.

Attenzione: con `load` su pagine molto pesanti il `timeout` del `SessionItem`
deve essere generoso — è il tempo di caricamento dell'**intera pagina**, non
della singola richiesta.

### 5.7 Lettura dei risultati: filtri e paginazione

`GET /api/results` è **l'unica rotta di elenco paginata**, perché è l'unica che
può restituire volumi grandi: una sessione lunga produce facilmente migliaia di
`Result`, mentre target, scenari e client restano nell'ordine delle decine.

#### Filtri (si combinano in AND)

| Parametro | Significato |
| --------- | ----------- |
| `?sessionId=` | i risultati di **una** esecuzione. Filtro **preferito**: diretto e senza ambiguità |
| `?sessionItemIds=` | lista comma-separated; mantenuto per compatibilità ma ambiguo (un `SessionItem` può essere condiviso fra sessioni, §3.3) |
| `?clientId=` | il motore che ha prodotto la misura, per il confronto curl vs Chrome |
| `?scenarioPath=` | confronto esatto sul path richiesto |

#### Paginazione

| Parametro | Default | Vincoli |
| --------- | ------- | ------- |
| `?page=` | `1` | ≥ 1, **1-based** |
| `?pageSize=` | `50` | 1–200 (`results_service.MAX_PAGE_SIZE`) |

Valori fuori range producono `422` da FastAPI, prima di toccare il database.
Una `page` oltre l'ultima disponibile restituisce `items` vuoto con `total`
invariato — non è un errore.

#### Forma della risposta: envelope, non array

Questa rotta rompe deliberatamente la convenzione delle altre (`list[Xxx]` nudo)
e restituisce un **envelope**:

```json
{ "items": [ … ], "total": 1200, "page": 1, "pageSize": 50 }
```

* `total` è il numero di risultati che soddisfano i **filtri**, non quelli nella
  pagina: senza, il frontend non potrebbe costruire i controlli di paginazione
  se non scorrendo tutte le pagine.
* `page`/`pageSize` sono riecheggiati per rendere la risposta autodescrittiva.

L'envelope è stato preferito a un header `X-Total-Count` per due ragioni: gli
header custom **non sono leggibili dal browser** senza aggiungere
`expose_headers` alla configurazione CORS (una dipendenza nascosta e facile da
rompere), e il progetto ha già un precedente di envelope dove serve davvero
(`SessionItemBatchResult`). Il modello è `ResultPage` in `models/result.py`.

L'ordinamento è per `time` crescente **con `_id` come discriminante secondario**:
serve a rendere la paginazione stabile, perché misure diverse possono condividere
lo stesso istante e senza un ordine totale pagine successive potrebbero ripetere
o saltare documenti.

> **Impatto sul frontend.** Sia la paginazione sia l'envelope sono cambiamenti
> **incompatibili** con un client che si aspettava l'array completo: senza
> `?pageSize=`, `GET /api/results` restituisce ora al massimo 50 elementi
> dentro `items`. Il frontend va aggiornato a leggere `items`/`total` e a
> paginare (o a passare `?pageSize=200` e iterare su `page`).

---

## 6. Comandi

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

### Setup aggiuntivo per il motore "chrome"

`pip install` porta la libreria Playwright, **non** il browser né le librerie di
sistema che gli servono. Servono due passi in più, entrambi obbligatori:

```bash
# 1. scarica il browser (~650 MB in ~/.cache/ms-playwright)
playwright install chromium

# 2. librerie di sistema richieste da Chromium (richiede sudo)
sudo apt-get install -y libnss3 libnspr4 libasound2t64
```

In alternativa al passo 2, `sudo playwright install-deps chromium` installa
l'intero set di dipendenze (più ampio del necessario).

Senza il passo 2 Chromium non si avvia: le misure con client `Chrome`
falliscono in modo pulito (`Result` con `status="failed"` ed errore *"Chromium
non utilizzabile: … error while loading shared libraries: libnspr4.so"*), senza
compromettere il resto della sessione né le misure con curl. Se le misure
Chrome falliscono tutte con quel messaggio, è quasi sempre il passo 2 mancante.

Verifica rapida che l'ambiente sia a posto:

```bash
python -c "import asyncio; from app.services.measurement import chrome_client; from app.models.target import Protocol; print(asyncio.run(chrome_client.measure('https://milaz.it:8444/', Protocol.HTTP3, 15000)))"
```

---

## 7. Aggiungere una nuova entità (checklist)

1. `app/models/<entita>.py` con `XxxCreate`, `XxxUpdate`, `Xxx`.
2. Riesportare in `app/models/__init__.py` (barrel).
3. Nome collezione in `app/db/collections.py`.
4. `app/services/<entita>_service.py` con le operazioni di dominio + docstring.
5. `app/routers/<entita>.py` con il CRUD, `response_model` espliciti + docstring.
6. Registrare il router in `app/main.py`.
7. Aggiornare la sezione 3 di questo documento.
