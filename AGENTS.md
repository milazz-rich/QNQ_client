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
    │   ├── mongo.py           # ciclo di vita della connessione motor + indici
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
        │   ├── firefox_client.py # motore "firefox": Playwright + Resource Timing
        │   └── runner.py        # entità del dominio → motore → Result
        └── session_runner.py  # orchestrazione dell'esecuzione di una sessione
└── tests/
    └── test_firefox_client.py # test unitari del client Firefox
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
lettura filtrata e paginata di `results` (§5.8) con endpoint di aggregazione
(§5.9), il session runner in background e **tre motori di misura**: `curl`
(§5.2), `chrome` (§5.6) e `firefox` (§5.7).

Non ancora implementato: client diversi da quelli registrati in
`runner.MEASUREMENT_BACKENDS`, che sollevano `NOT_IMPLEMENTED`; interruzione di
una sessione già avviata.

Nota su `firefox`: il motore production è completo e in servizio per **HTTP/2
e HTTP/3**. Per HTTP/3 usa un precondizionamento esplicito del profilo Firefox:
Alt-Svc persistito per origin, inizializzazione DataStorage tramite localhost e
misura successiva come prima richiesta QNQ del nuovo processo (§5.7).

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

Il **motore web** sotto test (Caddy, nginx, OpenLiteSpeed), con i suoi indirizzi
in ciascun ambiente di deploy.

| Campo       | Tipo                                  | Vincoli                     |
| ----------- | ------------------------------------- | --------------------------- |
| `id`        | `str`                                 | da `_id`, read-only         |
| `name`      | `str`                                 | 1–120 char, nome del motore |
| `endpoints` | `{ "docker": Endpoint, "kvm": Endpoint }` | dizionario a chiavi chiuse (`Environment`) |

`Endpoint` (documento embedded, uno per ambiente):

| Campo    | Tipo                          | Vincoli                                       |
| -------- | ----------------------------- | --------------------------------------------- |
| `host`   | `str`                         | 1–255 char, hostname o IP, senza schema `://` |
| `port`   | `int`                         | 1–65535                                       |
| `status` | `"online"\|"idle"\|"offline"` | enum, default `offline`                     |

Esempio:

```json
{
  "id": "…", "name": "nginx",
  "endpoints": {
    "docker": { "host": "milaz.it", "port": 8445, "status": "online" },
    "kvm":    { "host": "milaz.it", "port": 9445, "status": "online" }
  }
}
```

> **Né il protocollo né l'ambiente sono campi del Target** — sono parametri
> della misura e vivono su `SessionItem`; vedi la nota sul refactoring in fondo
> a questa sezione. Lo `status` è **per endpoint**, non per motore: la stessa
> build può essere raggiungibile in Docker e ferma in KVM, e un solo stato
> complessivo perderebbe l'informazione.

#### Scenario — collezione `scenarios`

Percorso/payload da richiedere al target.

| Campo  | Tipo  | Vincoli                        |
| ------ | ----- | ------------------------------ |
| `id`   | `str` | da `_id`                       |
| `name` | `str` | 1–120 char                     |
| `path` | `str` | deve iniziare con `/`          |
| `desc` | `str` | descrizione, ≤ 500 char        |

`Scenario` **non ha un campo `tag`**: era un'etichetta libera senza un uso
strutturale (non filtrava, non aggregava — a differenza di `environment`, che
quel ruolo lo ricopre davvero, vedi §5.9), quindi è stata rimossa dallo schema
invece di restare come metadato morto. I documenti `scenarios` preesistenti
sono stati aggiornati con un `$unset` idempotente, per lo stesso motivo di
`extra="forbid"` visto altrove (§3.4): un campo fuori schema nei documenti
esistenti farebbe fallire la validazione in lettura.

#### Client — collezione `clients`

Agente che esegue le misure.

| Campo  | Tipo  | Vincoli    |
| ------ | ----- | ---------- |
| `id`   | `str` | da `_id`   |
| `name` | `str` | 1–120 char |

#### SessionItem — collezione `session_items`

Una **variante da misurare** dentro una sessione: "*questo* scenario, con
*questo* protocollo, su *questo* ambiente".

| Campo         | Tipo                   | Vincoli                          |
| ------------- | ---------------------- | -------------------------------- |
| `id`          | `str`                  | da `_id`                         |
| `scenarioId`  | `str`                  | riferimento a `scenarios._id`    |
| `protocol`    | `"HTTP/2" \| "HTTP/3"` | enum, **obbligatorio**           |
| `environment` | `"docker" \| "kvm"`    | enum, **obbligatorio**; seleziona l'endpoint del target |

**Non ha `targetId`, `clientId`, `reps` né `timeout`**: sono della `Session`
che lo esegue, uguali per tutti i suoi item. Qui resta solo ciò che *varia* fra
un item e l'altro — la terna *(scenario, protocollo, ambiente)*, cioè le tre
dimensioni del confronto. `reps` e `timeout` in particolare sono impostati una
volta sola nello step di configurazione del wizard: non ha senso ripeterli
identici su ogni combinazione generata dal batch (§3.5).

È il `SessionItem` — non il `Target` — a decidere protocollo e ambiente: è lui
la fonte di verità che `measurement.runner` legge per scegliere il flag di curl
o di Chrome **e** per risolvere l'endpoint da interrogare (§3.6).

**Vincolo di integrità referenziale.** `DELETE /api/session-items/{id}` rifiuta
(`409 CONFLICT`) la cancellazione di un `SessionItem` se almeno una `Session`
lo referenzia ancora nel proprio array `items` (`items.sessionItemId`,
verificato con `$elemMatch` prima del `delete_one`). Un `SessionItem` è quindi
eliminabile solo se non appartiene a nessuna sessione esistente — il che rende
strutturalmente impossibile il caso di un `Result` segnaposto che debba
recuperare `targetId`/`scenarioId`/`environment` da un `SessionItem` già
cancellato (§5.4).

#### Session — collezione `sessions`

Esecuzione di un insieme ordinato di `SessionItem`: **un motore, misurato da un
client**, declinato su più scenari, protocolli e ambienti.

| Campo          | Tipo                                     | Vincoli                             |
| -------------- | ---------------------------------------- | ----------------------------------- |
| `id`           | `str`                                    | da `_id`                            |
| `name`         | `str`                                    | 1–120 char                          |
| `targetId`     | `str`                                    | riferimento a `targets._id`; il motore sotto test |
| `clientId`     | `str`                                    | riferimento a `clients._id`; il motore di misura |
| `reps`         | `int`                                    | ≥ 1, ripetizioni per item, uguali per tutta la sessione |
| `timeout`      | `int`                                    | ms, ≥ 1, uguale per tutta la sessione |
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
| `scenarioId`    | `str`                      | riferimento a `scenarios`; lo scenario misurato |
| `environment`   | `"docker" \| "kvm"`        | ambiente su cui è stata eseguita la misura |
| `idx`           | `int`                      | ≥ 0, indice della ripetizione            |
| `target`        | `str`                      | snapshot leggibile del target            |
| `scenarioPath`  | `str`                      | snapshot del path richiesto              |
| `proto`         | `"HTTP/2" \| "HTTP/3"`     | protocollo richiesto                     |
| `actualProto`   | `"HTTP/2" \| "HTTP/3" \| null` | protocollo negoziato; valorizzato **solo** se `status="completed"`, altrimenti `null` |
| `total`         | `float`                    | ms, ≥ 0, durata totale                   |
| `ttfb`          | `float`                    | ms, ≥ 0, time-to-first-byte              |
| `kb`            | `float`                    | ≥ 0, kilobyte trasferiti                 |
| `responseCode`  | `int \| null`              | ≥ 0, codice HTTP effettivo; `null` solo sui `Result` precedenti all'introduzione del campo |
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
stesso fallback sul `SessionItem`. Esiste perché il confronto **fra motori di
misura** (curl, Chrome, Firefox)
(§5.6) è una dimensione di analisi di prima classe: senza questa FK andrebbe
ricostruito risalendo `Result → SessionItem → Client`, con lo stesso rischio di
matching ambiguo già visto per il target. È anche il filtro `?clientId=` di
`GET /api/results`.

**`scenarioId`.** Completa la terna delle FK esplicite (`targetId`,
`clientId`, `scenarioId`), con la stessa logica e gli stessi due punti di
popolamento. `scenarioPath` resta lo snapshot testuale, ma **non** identifica
lo scenario: due scenari distinti possono condividere lo stesso path, e un
path può essere modificato dopo la misura. È la dimensione `scenario`
dell'aggregazione (§5.9) e il filtro `?scenarioId=` di `GET /api/results`.

> **Nota di migrazione.** `targetId`, `clientId` e `scenarioId` sono
> **obbligatori**: i `Result` scritti prima della loro introduzione non li
> avevano e la loro validazione fallirebbe. Ogni volta i documenti esistenti
> sono stati aggiornati con un backfill idempotente che ricava il valore dal
> `SessionItem` referenziato — possibile perché quel riferimento è sempre
> risultato ancora risolvibile (per `scenarioId`: 3500/3500 record migrati, 0
> orfani). Se in futuro si aggiungesse un altro riferimento obbligatorio a
> `Result`, va previsto lo stesso passaggio prima di rimettere in servizio
> l'API: **prima** verificare che il valore sia ricostruibile, e solo allora
> renderlo obbligatorio.
>
> `responseCode` ha seguito un percorso diverso, deliberatamente: è
> **opzionale** (`int | None`), non obbligatorio. Un backfill avrebbe dovuto
> *indovinare* il valore storico — e non è un'ipotesi neutra: prima
> dell'introduzione di questo campo il criterio di successo non controllava lo
> status HTTP (vedi sotto), quindi un `Result` "completed" storico potrebbe in
> realtà essere stato un `403` non rilevato. Presumere `200` per tutti i
> `completed` avrebbe nascosto esattamente il problema che questo campo esiste
> per rendere visibile. Meglio `null` onesto ("non registrato all'epoca") che
> un numero plausibile ma inventato su un dataset di misure reali.

#### Refactoring: protocollo e ambiente sono parametri della misura

Inizialmente il `Target` portava **sia** il protocollo **sia** l'ambiente
(quest'ultimo come `tag`, una stringa libera). Era modellazione sbagliata, con
un costo concreto e crescente: lo stesso motore andava censito una volta per
ogni combinazione. Tre motori × due ambienti × due protocolli = **dodici**
`Target` per tre software reali, con nomi duplicati nell'interfaccia, `tag` da
tenere allineati a mano, e un `targetId` che non identificava il motore ma la
tripla *(motore, ambiente, protocollo)* — rendendo ambigua ogni aggregazione
"per target".

Il principio che risolve tutto: **né il protocollo né l'ambiente sono attributi
del server**. Sono *parametri della misura*. Lo stesso motore, sullo stesso
codice, può essere interrogato in HTTP/2 o HTTP/3, e può girare in container o
su VM — ed è esattamente ciò che l'applicazione esiste per confrontare.
Stanno quindi su `SessionItem`, che rappresenta la singola variante — a
differenza di `reps` e `timeout`, che pur essendo anch'essi "parametri della
misura" non variano *fra* le combinazioni della stessa sessione, e per questo
sono risaliti sulla `Session` (dettaglio consolidato in una seconda passata,
vedi sotto).

Il modello che ne risulta:

```
Target (il motore)          "nginx", endpoints: { docker: …, kvm: … }
  └── Session               un Target + un Client, una tornata di misure
        └── SessionItem     scenario × protocollo × ambiente
              └── Result    l'esito di una singola ripetizione
```

Conseguenze:

* **`Target`** perde `protocol`, `tag`, `host`, `port` e `status` di primo
  livello; guadagna `endpoints`, un dizionario a chiavi chiuse (`docker`,
  `kvm`) di `{host, port, status}`. Dodici righe sono state consolidate in
  **tre**, una per motore.
* **`Session`** guadagna `targetId` e `clientId`: una sessione è un motore
  misurato da un client, e ripeterli su ogni item sarebbe stata duplicazione
  pura (oltre che un invito all'incoerenza).
* **`SessionItem`** perde `targetId`/`clientId` e guadagna `environment`.
  Resta solo ciò che varia fra un item e l'altro — e in una seconda passata di
  consolidamento gli sono stati tolti anche `reps` e `timeout` (risaliti sulla
  `Session`, vedi il paragrafo sopra e il dettaglio più sotto): non variavano
  comunque fra le combinazioni di una stessa sessione, quindi ripeterli su ogni
  item era la stessa duplicazione già corretta per target e client.
* **`Result`** guadagna `environment`, allo stesso titolo di
  `targetId`/`clientId`/`scenarioId`: è la dimensione del confronto
  containerizzato vs virtualizzato, e averla sul risultato evita di risalire al
  `SessionItem` a ogni aggregazione.
* **La creazione batch** genera ora Scenario × Protocollo × Ambiente (§3.5):
  target e client non sono più nel prodotto perché li fissa la sessione.
* **L'aggregazione** sostituisce la dimensione `tag` con `environment` (§5.9).
  Non è una ridenominazione di comodo: `tag` era una stringa libera sul
  `Target`, `environment` è un enum chiuso sul `Result` — niente più `$lookup`,
  niente più valori incoerenti fra righe duplicate.
* **Gli enum `Protocol` ed `Environment`** vivono in `models/common.py`:
  sono tipi trasversali (parametro della misura, dato dell'esito, chiave di
  risoluzione dell'endpoint), non attributi di una singola entità.
* **I dati storici** (`results`, `sessions`, `session_items`) sono stati
  **svuotati**, non migrati: nel nuovo modello ogni item ha protocollo e
  ambiente, e ogni sessione ha target e client — informazioni che nei dati
  preesistenti erano distribuite su entità che il consolidamento stava
  eliminando. Scelta concordata, trattandosi di misure riproducibili
  rilanciando le sessioni. `scenarios` e `clients` sono stati preservati.
* **Un target è stato perso nel consolidamento**: `Google` (`google.it:443`)
  aveva `tag="extra"`, non mappabile su `{docker, kvm}`. Nel nuovo schema un
  target esiste in uno o entrambi gli ambienti del confronto; un endpoint
  pubblico di controllo non ha più una casella. Se dovesse tornare utile, va
  ricreato con l'ambiente in cui lo si vuole collocare.

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

### 3.5 Creazione batch dei SessionItem

`POST /api/session-items/batch` è l'endpoint del wizard "Nuova sessione". Non
riceve una lista di item già espansa: riceve una **specifica**, e il prodotto
cartesiano lo costruisce il backend.

```json
{
  "scenarioIds":  ["…", "…", "…"],
  "protocols":    ["HTTP/2", "HTTP/3"],
  "environments": ["docker", "kvm"]
}
```

Genera **Scenario × Protocollo × Ambiente** — nell'esempio 3 × 2 × 2 = 12
`SessionItem`. Risponde `201` con `{"ids": [...]}` in ordine deterministico:
scenario esterno, poi protocollo, poi ambiente, così il chiamante può
correlare gli id alle combinazioni richieste senza rileggerli.

Dettagli non ovvi:

* **Target, client, `reps` e `timeout` non compaiono** nella specifica: sono
  scelte della `Session` che raccoglierà questi item (§3.3), uguali per tutti.
  Il wizard li imposta una volta sola nello step di configurazione, non per
  ogni combinazione generata qui.
* **`protocols` ed `environments` sono assi espliciti** per effetto del
  refactoring (§3.3): finché protocollo e ambiente stavano sul `Target`, il
  prodotto era Target × Scenario e le due dimensioni erano implicite nella
  selezione dei target duplicati. Ora sono scelte dichiarate.
* **I tre insiemi sono deduplicati** preservando l'ordine: lo stesso scenario o
  protocollo indicato due volte genererebbe altrimenti item identici che
  l'utente non ha chiesto.
* **Lista vuota → `422`** (`min_length=1` sui tre array), prima di toccare il
  database.
* **Gli scenari non sono verificati** al momento della creazione: un id
  inesistente produce un item che fallirà con `NOT_FOUND` alla prima
  esecuzione, tracciato come tale (§5.4). Validarli qui costerebbe N query per
  un errore che il runner intercetta comunque.

> **Cambiamento incompatibile.** Il frontend va aggiornato: il wizard deve ora
> scegliere target, client, `reps` e `timeout` **una volta per l'intera
> sessione** (inclusi in `POST /api/sessions`, non nel batch), e
> scenari/protocolli/ambienti come selezioni multiple per il prodotto.

### 3.6 Risoluzione dell'endpoint in fase di misura

L'`environment` del `SessionItem` non è un'etichetta descrittiva: è la **chiave
con cui si sceglie l'indirizzo da interrogare**. La risoluzione avviene una
sola volta per item, in `measurement.runner.resolve_context`:

```python
target   = await targets_service.get_target(target_id)      # dalla Session
endpoint = target.endpoints[session_item.environment]        # docker | kvm
url      = build_url(endpoint.host, endpoint.port, scenario.path)
```

Concretamente, con il target `nginx` dell'esempio in §3.3:

| `SessionItem` | endpoint risolto | URL misurato |
| ------------- | ---------------- | ------------ |
| `environment="docker"`, `protocol="HTTP/2"` | `milaz.it:8445` | `https://milaz.it:8445/…` con `--http2` |
| `environment="kvm"`, `protocol="HTTP/3"` | `milaz.it:9445` | `https://milaz.it:9445/…` con `--http3` |

Due proprietà che ne derivano:

* **Un target che non espone l'ambiente richiesto** solleva `NotFoundError`:
  l'item viene marcato `failed` e tracciato con un `Result` segnaposto (§5.4),
  senza interrompere gli altri item della sessione.
* **Lo snapshot `Result.target` include l'ambiente** —
  `"nginx [docker] (milaz.it:8445)"` — perché lo stesso `Target` produce misure
  su due endpoint diversi, e un'etichetta che non li distinguesse renderebbe i
  risultati ambigui a colpo d'occhio.

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
per i client assenti da `runner.MEASUREMENT_BACKENDS`, cioè diversi da `curl`,
`chrome` e `firefox`. Il frontend può così spiegare la situazione
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
| `FIREFOX_WAIT_UNTIL` | `load`                        | idem, per il motore Firefox (§5.7) |
| `MEASUREMENT_DELAY_MS` | `300`                       | pausa fra ripetizioni, uniforme per ogni client/target/protocollo (§5.1) |

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

Il motore Firefox **non ha** un'impostazione corrispondente: il certificato
self-signed è gestito da `ignore_https_errors` del context Playwright,
verificato necessario e sufficiente (§5.7). L'unica configurazione Firefox è
quindi `FIREFOX_WAIT_UNTIL`, con lo stesso significato del corrispettivo
Chrome.

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
        currentIndex = i, item.status = running, item.total = Session.reps
        risolve Target / Scenario / Client   (measurement.runner.resolve_context)
        cancella i Result di una precedente run DI QUESTA sessione (sessionId+item)
        per ogni ripetizione:
          curl → Result (con sessionId = questa sessione) salvato → item.done += 1
          attesa fissa di MEASUREMENT_DELAY_MS prima della ripetizione successiva
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

**Pausa fra ripetizioni (`MEASUREMENT_DELAY_MS`).** Dopo ogni ripetizione —
riuscita o fallita — `session_runner._run_single_item` attende
`settings.measurement_delay_ms` (default `300` ms) prima di lanciare la
successiva. La pausa è **sempre la stessa**, per qualunque combinazione di
client (curl, Chrome o Firefox), target e protocollo: non è condizionata al caso
specifico (es. "solo su OpenLiteSpeed", o "solo se la precedente è fallita"),
perché differenziarla introdurrebbe una variabile in più fra i dati raccolti
su target diversi, minando proprio la comparabilità che l'applicazione esiste
per garantire. Motivata da un rate limiter per-IP verificato empiricamente su
OpenLiteSpeed (§5.3): senza pausa, ripetizioni ravvicinate su quel target
ricevevano regolarmente `403` pur negoziando il protocollo corretto.

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

### 5.3 Fallback di protocollo e criterio di successo

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

**Il protocollo negoziato da solo non basta.** Serve anche uno status HTTP di
successo (`is_http_success`, in `curl_client.py` e condivisa da
`chrome_client.py`): **2xx**, altrimenti la misura è `failed` anche se
`actualProto` è HTTP/2 o HTTP/3. Verificato empiricamente durante l'indagine
su un rate limiter per-IP di LiteSpeed: connessioni HTTP/2 ravvicinate possono
ricevere un `403` mantenendo `http_version=2` — protocollo corretto, ma non è
una misura valida della pagina richiesta.

| Richiesta   | Server                          | `http_version` | `response_code` | Esito |
| ----------- | -------------------------------- | -------------- | ---------------- | ----- |
| `--http2`   | rate limiter attivo (`403`)      | `2`             | `403`             | `failed`, `actualProto` = `null` |
| `--http2`   | risposta normale (`200`)         | `2`             | `200`             | `completed`, `actualProto` = HTTP/2 |

Questo criterio **rileva** il rate limiter, ma da solo non lo evita. La
mitigazione è `MEASUREMENT_DELAY_MS` (§5.1): una pausa fissa fra ripetizioni,
applicata sempre e per ogni target — non solo su OpenLiteSpeed — per non
introdurre una differenza metodologica fra i target che invaliderebbe il
confronto.

Il campo `proto` di `Result` conserva **sempre** il protocollo richiesto.
`actualProto` è valorizzato **solo** quando `status="completed"` (protocollo
corretto **e** 2xx), e in quel caso è sempre HTTP/2 o HTTP/3 — non può
contenere HTTP/1.1 né altri valori. `responseCode` invece è popolato **sempre**,
riuscita o no, con il codice HTTP effettivo (`0` se nessuna risposta è
arrivata): è il campo da guardare per distinguere un errore di rete da un
errore applicativo del server, cosa che `status="failed"` da solo non dice.
Questo evita l'errore opposto rispetto a prima: un confronto che leggesse
`total`/`ttfb` senza controllare `status` non può più scambiare per dati validi
né una richiesta caduta su HTTP/1.1 né una pagina di errore del server.

Esiste `--http3-only` per la modalità strict (fallire invece di ripiegare): non
è usato, perché il requisito è **rilevare** il fallback fra HTTP/2 e HTTP/3
(tramite `actualProto`), non impedirlo — mentre un fallback fuori da questi
due protocolli, o una risposta di errore, sono comunque un fallimento,
rilevato tramite `status="failed"`.

### 5.4 Fallimenti

Una misura fallita non interrompe mai l'esecuzione: produce un `Result` con
`status="failed"`, tempi a zero e `actualProto=null`. Sono trattati così:

* curl esce con codice ≠ 0 (connessione rifiutata, DNS, TLS, `--max-time` scaduto);
* curl esce con 0 ma `response_code` è 0 (nessuna risposta);
* curl riceve una risposta ma il protocollo negoziato non è HTTP/2 né HTTP/3
  (fallback su HTTP/1.1, o `http_version` non riconosciuto) — vedi §5.3;
* curl riceve una risposta con protocollo corretto ma `response_code` non 2xx
  (es. `403`, `500`): il protocollo da solo non certifica una misura valida —
  vedi §5.3;
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
`session_runner` rileggendo il `SessionItem` di configurazione — rilettura che
non fallisce mai per id inesistente, perché `DELETE /api/session-items/{id}`
rifiuta la cancellazione di un `SessionItem` ancora referenziato da una
`Session` (§3.3).

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

Sul versante lettura vedi §5.8.

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

#### Fallimento noto: `ERR_CERT_VERIFIER_CHANGED`

Occasionalmente una misura fallisce con `net::ERR_CERT_VERIFIER_CHANGED`, un
errore Chromium legato al servizio interno di verifica dei certificati.
Indagato empiricamente perché a prima vista sembrerebbe collegato al lancio
ravvicinato di istanze Chrome (ogni ripetizione ne avvia una nuova, §5.1) o a
una `user-data-dir` non isolata fra ripetizioni — nessuna delle due ipotesi ha
retto alla verifica diretta:

* **Tasso**: `1/300 = 0,33%` (IC95% 0,06–1,86%), su misure reali contro un
  target di test (Caddy, HTTP/2), con lo stesso `chrome_client.measure()` in
  uso in produzione.
* **Concorrenza fra istanze — esclusa.** Un poller di processi in background
  (campionamento ogni 20ms della cmdline reale via `ps`, non un'ipotesi) non
  ha mai rilevato il processo di una ripetizione ancora vivo al lancio della
  successiva, incluso l'istante esatto dell'unico fallimento osservato:
  `browser.close()` risulta aver atteso davvero la terminazione del processo
  prima di ritornare.
* **Isolamento `user-data-dir` — confermato.** Verificato dalla cmdline reale
  dei processi (non assunto): ogni lancio riceve una directory temporanea con
  suffisso casuale generata da Playwright, mai passata esplicitamente da
  `chrome_client.py`. Su 450 misure osservate, nessun riuso genuino della
  stessa directory fra ripetizioni non adiacenti.
* **Ritardo fra un lancio e l'altro — nessun beneficio misurabile per QUESTO
  errore.** Testato con 150ms di pausa fra la chiusura di un'istanza e l'avvio
  della successiva: `0/150` fallimenti, contro l'`1/300` del caso base. Fisher
  exact test `p = 1,000`: nessuna differenza statisticamente significativa (con
  un evento così raro servirebbero migliaia di campioni per condizione per
  distinguere in modo affidabile un eventuale effetto reale dal rumore
  campionario). All'epoca di questa indagine nessun ritardo sistematico era
  stato introdotto proprio per `ERR_CERT_VERIFIER_CHANGED`: non c'era prova
  che aiutasse, a fronte di un costo certo per mitigare un evento che capita 3
  volte su 1000. Da allora `MEASUREMENT_DELAY_MS` (§5.1) è stato comunque
  introdotto, ma per un problema **diverso e confermato** (il rate limiter di
  OpenLiteSpeed, §5.3) — come effetto collaterale riduce anche il rischio di
  qualunque race legata al lancio ravvicinato, incluso in teoria questo caso,
  ma resta un fallimento noto e occasionale da tenere presente, non qualcosa
  che ci si aspetta di eliminare del tutto.

**Causa più probabile**: una race condition rara interna al network stack di
Chromium durante l'inizializzazione del servizio di verifica certificati su un
profilo nuovo — plausibile perché ogni misura è, per costruzione (§5.1), un
profilo Chrome mai usato prima, quindi quel servizio parte da zero a ogni
singola ripetizione. Non è quindi imputabile a come l'applicazione gestisce il
ciclo di vita delle istanze, né al target di test.

**Nessun fix applicativo necessario.** Il fallimento è già gestito
correttamente dal percorso HTTP/3 esistente (eccezione di navigazione
catturata, vedi tabella sopra): produce un `Measurement` con
`succeeded=False`, che diventa un `Result` con `status="failed"` — nessun
crash, nessuna sessione bloccata, nessun dato silenziosamente perso o
scambiato per una misura valida.

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

Attenzione: con `load` su pagine molto pesanti il `timeout` della `Session`
deve essere generoso — è il tempo di caricamento dell'**intera pagina**, non
della singola richiesta.

### 5.7 Motore di misura "firefox" (Playwright + Resource Timing)

Il terzo motore di misura, selezionabile creando un `Client` di nome `Firefox`,
guida **Firefox headless** via Playwright e legge protocollo/timing dall'API
cross-browser disponibile anche su Firefox. Espone lo stesso contratto di
`curl_client` e `chrome_client`:

```python
async def measure(url: str, protocol: Protocol, timeout_ms: int) -> Measurement
```

E registrato in `runner.MEASUREMENT_BACKENDS` e produce sempre un `Measurement`
strutturato: un errore di browser, rete, timeout, protocollo negoziato errato o
status HTTP non 2xx diventa un `Result` `failed`, non un'eccezione che blocca la
sessione.

#### Stato production

Firefox e operativo in production sia per **HTTP/2** sia per **HTTP/3** sui
target QNQ. HTTP/3 richiede una fase di precondizionamento del profilo separata
dalla misura, perche Firefox non ha un flag equivalente a `curl --http3` o a
`--origin-to-force-quic-on` di Chrome.

Il principio metodologico e: protocollo noto prima della misura, connessione
verso il target creata durante la misura. Curl conosce HTTP/3 dal flag
`--http3`, Chrome lo conosce da `--origin-to-force-quic-on`, Firefox lo conosce
dall'`Alt-Svc` persistito nel profilo. In tutti e tre i casi la connessione
verso il target nasce nella misura reale.

#### HTTP/2

Il percorso HTTP/2 resta volutamente semplice: `measure()` avvia un nuovo
browser Firefox non persistente, crea un context con `ignore_https_errors=True`,
imposta `network.http.http3.enable=false` e naviga una sola volta verso l'URL
misurato. Non usa profili persistenti, non fa priming localhost e non prepara
Alt-Svc. Il controllo finale resta il protocollo effettivamente negoziato:
richiesta HTTP/2 + `nextHopProtocol="h2"` + status 2xx produce `completed`;
qualunque altro protocollo produce `failed`.

#### HTTP/3: profilo preparato

Firefox scopre HTTP/3 sui target QNQ tramite l'header `Alt-Svc` del server.
Questa conoscenza viene persistita nel profilo Firefox in `AlternateServices.bin`,
ma non e disponibile a un profilo completamente vergine prima della prima
risposta del target. Il client production gestisce quindi un profilo preparato
per ogni origin, con chiave derivata da `scheme + host + port`.

I profili persistenti stanno sotto `.runtime/firefox/profiles/`, directory
ignorata da Git. La directory di ogni origin include anche un digest SHA-256
della chiave, cosi porte diverse dello stesso hostname non condividono lo stesso
profilo. L'accesso alla preparazione e protetto da un lock asincrono keyed per
origin: oggi le misure sono sequenziali, ma due preparazioni future della stessa
origin non devono poter corrompere il profilo base.

Se `AlternateServices.bin` esiste gia e contiene host e token `h3`, il
precondizionamento viene saltato. Se manca, Firefox viene avviato con un
`launch_persistent_context` sul profilo base e viene fatta una navigazione non
misurata per apprendere l'Alt-Svc. Il client preferisce la root della origin
(`https://host:port/`) perche e il path piu neutro disponibile; se non produce
un Alt-Svc h3 utilizzabile, prova l'URL effettivo passato a `measure()`. Questa
fase e esplicitamente precondizionamento: non alimenta `total`, `ttfb`, `kb` e
non viene registrata come risultato.

Le preference HTTP/3 production sono solo quelle verificate:

```python
{
    "network.http.http3.enable": True,
    "network.http.http3.disable_when_third_party_roots_found": False,
    "network.http.http3.enable_0rtt": False,
    "security.tls.enable_0rtt_data": False,
    "security.ssl.disable_session_identifiers": True,
    "browser.cache.disk.enable": False,
    "browser.cache.memory.enable": False,
}
```

`disable_when_third_party_roots_found=false` e necessario con i certificati non
pubblici dei target QNQ; senza questa preference Firefox puo rifiutare l'upgrade
a HTTP/3 anche quando Alt-Svc e corretto. Le preference su 0-RTT e session
identifiers sono state verificate nella build Playwright usata e mantengono la
misura compatibile con l'invariante cold-connection.

#### HTTP/3: misura e priming DataStorage

Un dettaglio specifico di Firefox rende insufficiente il solo profilo
persistente: subito dopo l'avvio `AlternateServices.bin` esiste, ma il
DataStorage Alt-Svc puo non essere ancora pronto. In quel caso Firefox logga
`storage is not ready` e la prima richiesta QNQ resta HTTP/2. Una semplice
attesa dopo l'avvio non risolve il problema; il caricamento e lazy e serve
attivare lo stack HTTP.

Per ogni misura HTTP/3 reale il client:

1. copia il profilo preparato in `.runtime/firefox/runs/measure-*`;
2. avvia un nuovo processo Firefox con quella copia temporanea;
3. avvia un piccolo server HTTP/1.1 locale su `127.0.0.1` e porta dinamica;
4. naviga una volta su localhost per inizializzare HTTP/DataStorage;
5. chiude il server locale;
6. naviga una sola volta verso il target QNQ;
7. chiude Firefox e cancella la copia temporanea del profilo.

Il server localhost risponde solo `200 ok`, non usa HTTPS, non restituisce
`Alt-Svc`, non usa HTTP/3, non condivide hostname o certificati con QNQ e viene
sempre chiuso in `finally`. Questa richiesta non viene misurata: i valori
`total`, `ttfb` e `kb` vengono letti solo dalla navigazione successiva verso il
target. La validazione sperimentale su questa build ha mostrato che il priming
locale non contatta `milaz.it`, non apre connessioni verso le porte QNQ, non
apprende Alt-Svc QNQ e non prepara TLS/QUIC verso il target; serve solo a
rendere pronto lo storage Firefox.

Poiche ogni misura usa un nuovo processo Firefox e una copia temporanea del
profilo, non ci sono connessioni QNQ attive da riusare fra ripetizioni. I log
Mozilla raccolti durante la validazione hanno mostrato assenza di connessioni
attive preesistenti, creazione `DnsAndConnectSocket`, ALPN `h3`,
`Http3Session::Init` e dispatch `isHttp3=1` dopo l'inizio della misura.

#### Dati estratti

Non esistendo il CDP, il protocollo negoziato e i byte si leggono dalla
**Navigation Timing** del documento (`performance.getEntriesByType('navigation')[0]`,
via `page.evaluate`) e i tempi da `response.request.timing` di Playwright:

| Campo `Result` | Origine |
| -------------- | ------- |
| `actualProto`  | `nextHopProtocol` (`h2`/`h3` -> HTTP/2 / HTTP/3) |
| `ttfb`         | `timing.responseStart` (ms dall'inizio della richiesta) |
| `total`        | `timing.responseEnd` |
| `kb`           | `transferSize / 1024` dalla Navigation Timing |

`transferSize` include gli header, come `encodedDataLength` di Chrome, quindi
non e confrontabile 1:1 con `size_download` di curl. Il confronto dei byte resta
valido fra protocolli a parita di client, non fra client diversi.

#### Criterio di successo

Il client Firefox dichiara una misura riuscita solo se valgono tutte queste
condizioni:

| Richiesta | Protocollo negoziato | Status | Esito |
| --------- | -------------------- | ------ | ----- |
| HTTP/2 | `h2` | 2xx | `completed` |
| HTTP/3 | `h3` | 2xx | `completed` |
| HTTP/3 | `h2` | qualunque | `failed` |
| qualunque | HTTP/1.1 o sconosciuto | qualunque | `failed` |
| qualunque | `h2`/`h3` corretto | non 2xx | `failed` |

Questo controllo e piu severo di curl/Chrome perche Firefox non puo forzare il
protocollo con un flag di processo: una navigazione che termina bene ma negozia
il protocollo sbagliato non e la misura richiesta.

#### Verifiche production

Il consolidamento production e stato validato con `firefox_client.measure()` e
con il normale `session_runner`:

| Endpoint QNQ | Esito HTTP/3 production |
| ------------ | ----------------------- |
| OpenLiteSpeed Docker `8443` | `HTTP/3`, `200` |
| Caddy Docker `8444` | `HTTP/3`, `200` |
| nginx Docker `8445` | `HTTP/3`, `200` |
| OpenLiteSpeed KVM `9443` | `HTTP/3`, `200` |
| Caddy KVM `9444` | `HTTP/3`, `200` |
| nginx KVM `9445` | `HTTP/3`, `200` |

Sono state inoltre eseguite quattro ripetizioni consecutive su Caddy Docker
`8444`, tutte `HTTP/3` con status `200`, e due misure HTTP/2 di controllo su
Docker/KVM, entrambe `HTTP/2` con status `200`. Una sessione reale con client
Firefox, protocollo HTTP/3, target Caddy Docker e scenario `/` e terminata
`completed` e ha salvato un `Result` con `proto="HTTP/3"`,
`actualProto="HTTP/3"`, `status="completed"`, `responseCode=200` e valori
`total`/`ttfb`/`kb` positivi.

#### Limitazioni note

Le vecchie strade escluse restano escluse: il nome corretto della pref e
`network.http.http3.enable` (non `enabled`), le mapping Alt-Svc forzate via pref
non rendono utilizzabile la primissima navigazione a freddo, il warm-up verso il
target nello stesso context non e comparabile perche riusa la connessione, e
DNS HTTPS/SVCB non e disponibile nell'infrastruttura QNQ corrente. Questi punti
spiegano perche il client production usa profilo precondizionato e priming
localhost invece di aggiungere preference casuali o navigazioni QNQ nascoste.

### 5.8 Lettura dei risultati: filtri e paginazione

`GET /api/results` è **l'unica rotta di elenco paginata**, perché è l'unica che
può restituire volumi grandi: una sessione lunga produce facilmente migliaia di
`Result`, mentre target, scenari e client restano nell'ordine delle decine.

#### Filtri (si combinano in AND)

| Parametro | Significato |
| --------- | ----------- |
| `?sessionId=` | i risultati di **una** esecuzione. Filtro **preferito**: diretto e senza ambiguità |
| `?sessionItemIds=` | lista comma-separated; mantenuto per compatibilità ma ambiguo (un `SessionItem` può essere condiviso fra sessioni, §3.3) |
| `?clientId=` | il motore che ha prodotto la misura, per il confronto fra curl, Chrome e Firefox |
| `?targetId=` | il server sotto test |
| `?scenarioId=` | lo scenario, per riferimento diretto |
| `?scenarioPath=` | confronto esatto sul path richiesto; è uno snapshot testuale, `?scenarioId=` è più preciso |

Gli stessi identici filtri valgono per `GET /api/results/aggregate` (§5.9):
sono costruiti da un'unica funzione condivisa, `results_service.build_filter_query`,
proprio perché due copie divergerebbero alla prima aggiunta.

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

### 5.9 Aggregazione e indici

#### `GET /api/results/aggregate`

Restituisce **solo medie**, mai risultati grezzi: è pensato per alimentare i
grafici comparativi senza trasferire decine di migliaia di documenti al
frontend. Tutto il calcolo avviene nel database, con una pipeline di
aggregazione MongoDB (`$match` → eventuale `$lookup` → `$group` → `$sort`).

| Parametro | Valori | Note |
| --------- | ------ | ---- |
| `?groupBy=` | `target` \| `environment` \| `client` \| `scenario` | **obbligatorio** |
| `?metric=` | `total` \| `ttfb` \| `kb` | default `total` |
| *filtri* | gli **stessi** di `GET /api/results` (§5.8) | in AND |

Tre proprietà non ovvie, tutte deliberate:

* **Il protocollo è sempre parte della chiave di raggruppamento.** Ogni gruppo
  è una coppia *dimensione × protocollo*, mai una dimensione sola: il confronto
  HTTP/2 vs HTTP/3 è l'oggetto stesso dell'applicazione, e una media che li
  mescolasse non avrebbe alcun significato.
* **Solo `status="completed"` entra nel calcolo.** Una misura fallita ha
  `total`/`ttfb`/`kb` azzerati per costruzione (§5.3): includerla abbasserebbe
  le medie **in proporzione al tasso di fallimento**, facendo sembrare più
  veloce un target che sta semplicemente fallendo di più — l'esatto contrario
  della verità. Misurato sui dati reali: media di `total` 28,00 ms includendo
  i fallimenti contro 29,28 ms sui soli `completed`. Il campo `considered`
  della risposta dichiara quante misure sono entrate nel calcolo, così lo
  scarto rispetto al totale filtrato resta visibile.
* **`groupBy=environment`** confronta Docker e KVM a parità di tutto il resto.
  Sostituisce il vecchio `groupBy=tag`: `tag` era una stringa libera sul
  `Target`, `environment` è un enum chiuso salvato direttamente sul `Result`
  (§3.3) — il che elimina sia il `$lookup` sia il rischio di valori incoerenti.

L'etichetta leggibile di ogni gruppo viene, dove possibile, dai campi già
presenti nel `Result` (`target`, `scenarioPath`, `environment`): nessun
`$lookup` per `target`, `scenario` ed `environment`. Solo `client` richiede una
join — con `$toObjectId`, perché nei `Result` gli id sono **stringhe** mentre
gli `_id` sono `ObjectId` e Mongo non converte implicitamente.

> **Ordine delle rotte.** `GET /results/aggregate` è dichiarato **prima** di
> `GET /results/{result_id}` in `routers/results.py`: sono entrambe `GET` sullo
> stesso livello di path e FastAPI risolve nell'ordine di dichiarazione.
> Invertendole, `/aggregate` verrebbe interpretato come un `result_id` e
> produrrebbe un `422`.

#### Indici della collezione `results`

Creati all'avvio da `db.mongo.ensure_indexes()` (idempotente: `create_index`
non fa nulla se l'indice esiste già). Solo su `results`, l'unica collezione che
cresce senza limite; su target, scenari e client — decine di documenti — un
indice costerebbe più della scansione che evita. Un fallimento nella creazione
viene loggato ma **non** blocca l'avvio, coerentemente con `connect_to_mongo`.

| Indice | Chiave | Serve a |
| ------ | ------ | ------- |
| `ix_session_status` | `{sessionId, status}` | filtro per esecuzione, cascata di cancellazione |
| `ix_target_status` | `{targetId, status}` | aggregazione/confronto per server |
| `ix_scenario_client_status` | `{scenarioId, clientId, status}` | aggregazione per scenario incrociata col motore |
| `ix_session_time_id` | `{sessionId, time, _id}` | paginazione di `GET /api/results` |
| `ix_session_item` | `{sessionId, sessionItemId}` | pulizia pre-run di un singolo item |

Verificato con `explain()` che ogni query principale usi effettivamente
l'indice previsto, con `totalDocsExamined == nReturned` (nessun documento letto
inutilmente):

| Query | Piano | Indice |
| ----- | ----- | ------ |
| `?sessionId=` + sort + `skip/limit` | `IXSCAN`, nessun sort in memoria | `ix_session_time_id` |
| `{sessionId, status}` | `IXSCAN` | `ix_session_status` |
| `{targetId, status}` | `IXSCAN` | `ix_target_status` |
| `{scenarioId, clientId, status}` | `IXSCAN` | `ix_scenario_client_status` |
| `{sessionId, sessionItemId}` | `IXSCAN` | `ix_session_item` |

`ix_session_time_id` **non** era fra gli indici pensati inizialmente, ma
l'`explain()` ne ha mostrato la necessità: la paginazione ordina per
`(time, _id)`, e con il solo `ix_session_status` Mongo eseguiva un `SORT` in
memoria esaminando **282** documenti per restituirne 50. Con l'indice che
include anche le chiavi di ordinamento ne esamina esattamente **50**, senza
stage di sort. È il caso tipico in cui un indice sui soli campi di filtro non
basta: se la query ordina, l'ordinamento va fatto entrare nell'indice.

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

### Setup aggiuntivo per il motore "firefox"

Stessa logica di Chrome: `pip install` porta la libreria, non il browser.

```bash
# scarica il browser (~90 MB in ~/.cache/ms-playwright)
playwright install firefox
```

A differenza di Chromium, sul WSL di sviluppo Firefox **non ha richiesto
librerie di sistema aggiuntive**: `playwright install firefox` è bastato. Se su
un altro ambiente il browser non si avviasse, vale lo stesso rimedio:

```bash
sudo playwright install-deps firefox
```

Come per Chrome, un browser mancante non compromette la sessione: le misure con
client `Firefox` falliscono in modo pulito (`Result` con `status="failed"` ed
errore *"Firefox non utilizzabile: …"*).

Verifica rapida del client production — **usare `Protocol.HTTP2`**, non HTTP/3:
HTTP/3 richiede la fase di precondizionamento descritta in §5.7 e non è ancora
integrata in `firefox_client.py`.

```bash
python -c "import asyncio; from app.services.measurement import firefox_client; from app.models.common import Protocol; print(asyncio.run(firefox_client.measure('https://milaz.it:8444/', Protocol.HTTP2, 20000)))"
```

### Client di misura nel database

Un motore è selezionabile in una `Session` solo se esiste un `Client` con il
nome corrispondente (confronto case-insensitive contro
`runner.MEASUREMENT_BACKENDS`). Per aggiungere Firefox:

```bash
curl -X POST http://localhost:8000/api/clients \
  -H 'Content-Type: application/json' -d '{"name": "Firefox"}'
```

I tre `Client` previsti sono quindi `curl`, `Chrome` e `Firefox`.

---

## 7. Aggiungere una nuova entità (checklist)

1. `app/models/<entita>.py` con `XxxCreate`, `XxxUpdate`, `Xxx`.
2. Riesportare in `app/models/__init__.py` (barrel).
3. Nome collezione in `app/db/collections.py`.
4. `app/services/<entita>_service.py` con le operazioni di dominio + docstring.
5. `app/routers/<entita>.py` con il CRUD, `response_model` espliciti + docstring.
6. Registrare il router in `app/main.py`.
7. Aggiornare la sezione 3 di questo documento.
