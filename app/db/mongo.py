"""Ciclo di vita della connessione a MongoDB tramite il driver asincrono motor."""

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from app.core.config import settings
from app.core.errors import DatabaseError
from app.db.collections import RESULTS, SESSIONS
from app.models.session import RunStatus

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None
_database: AsyncIOMotorDatabase | None = None


async def connect_to_mongo() -> None:
    """Apre la connessione a MongoDB e ne verifica la raggiungibilità.

    Riceve:
        Nulla; usa ``settings.mongo_dsn`` e ``settings.mongo_db``.

    Restituisce:
        ``None``.

    Fa:
        Istanzia il client motor e tenta un ``ping``. Un fallimento viene
        registrato come warning ma **non** blocca l'avvio: l'applicazione parte
        comunque e ``/api/health`` segnalerà lo stato ``degraded``. Questo è
        voluto, perché l'IP del gateway WSL cambia fra i riavvii e il backend
        deve poter essere avviato per diagnosticare il problema.
    """
    global _client, _database

    logger.info("Connessione a MongoDB: %s (db=%s)", settings.mongo_dsn, settings.mongo_db)
    _client = AsyncIOMotorClient(
        settings.mongo_dsn,
        serverSelectionTimeoutMS=settings.mongo_timeout_ms,
        connectTimeoutMS=settings.mongo_timeout_ms,
        uuidRepresentation="standard",
    )
    _database = _client[settings.mongo_db]

    try:
        await _client.admin.command("ping")
    except PyMongoError as exc:
        logger.warning(
            "MongoDB non raggiungibile su %s: %s. "
            "Se si esegue da WSL, verificare MONGO_HOST nel file .env "
            "(l'IP del gateway cambia fra i riavvii).",
            settings.mongo_dsn,
            exc,
        )
    else:
        logger.info("MongoDB connesso.")


async def ensure_indexes() -> None:
    """Crea gli indici delle collezioni ``results`` e ``sessions``, se non esistono già.

    Riceve:
        Nulla.

    Restituisce:
        ``None``.

    Fa:
        ``create_index`` è idempotente: se l'indice esiste con la stessa
        definizione non fa nulla, quindi la funzione può girare a ogni avvio.
        Gli indici su ``results`` esistono perché è l'unica collezione che
        cresce senza limite (migliaia di documenti per sessione), mentre
        target, scenari e client restano nell'ordine delle decine e una
        scansione completa lì è più economica di un indice da mantenere.
        L'indice su ``sessions`` non è invece per le prestazioni ma per
        **integrità**: un indice unico parziale che vieta più di una Session
        con ``status="running"`` contemporaneamente (§5.1 di AGENTS.md).

        Un fallimento viene registrato ma **non** blocca l'avvio, coerentemente
        con ``connect_to_mongo``: senza indici l'applicazione funziona comunque
        (nel caso di ``results``, solo più lentamente; nel caso del vincolo su
        ``sessions``, senza la garanzia atomica — resta comunque il controllo
        applicativo in ``sessions_service.get_running_session``), e deve poter
        partire per essere diagnosticata.

        Le combinazioni scelte per ``results`` rispecchiano i filtri realmente
        usati da ``results_service`` (vedi AGENTS.md §5.9).
    """
    if _database is None:
        logger.warning("Indici non creati: connessione al database non inizializzata.")
        return

    collection = _database[RESULTS]
    try:
        # Filtro per singola esecuzione, il più frequente (lettura dei risultati
        # di una sessione, cancellazione a cascata, pulizia pre-run).
        await collection.create_index([("sessionId", 1), ("status", 1)], name="ix_session_status")
        # Confronto fra protocolli/ambienti sullo stesso server sotto test.
        await collection.create_index([("targetId", 1), ("status", 1)], name="ix_target_status")
        # Aggregazioni per scenario, tipicamente incrociate col motore di misura.
        await collection.create_index(
            [("scenarioId", 1), ("clientId", 1), ("status", 1)], name="ix_scenario_client_status"
        )
        # Paginazione di GET /api/results: l'ordinamento (time, _id) è parte
        # dell'indice, altrimenti Mongo dovrebbe ordinare in memoria l'intero
        # risultato del filtro prima di applicare skip/limit.
        await collection.create_index(
            [("sessionId", 1), ("time", 1), ("_id", 1)], name="ix_session_time_id"
        )
        # Pulizia pre-run di un singolo item all'interno di una sessione.
        await collection.create_index(
            [("sessionId", 1), ("sessionItemId", 1)], name="ix_session_item"
        )
    except PyMongoError as exc:
        logger.warning("Impossibile creare gli indici su '%s': %s", RESULTS, exc)
    else:
        logger.info("Indici su '%s' verificati.", RESULTS)

    sessions_collection = _database[SESSIONS]
    try:
        # Al più una Session con status="running" in tutta la collezione: le
        # misure sono sequenziali per metodologia (AGENTS.md §5.1), quindi due
        # sessioni "running" contemporaneamente non sono uno stato valido, non
        # solo uno sconsigliato. L'indice è parziale (si applica solo ai
        # documenti con status="running", non a tutta la collezione) e rende
        # la garanzia atomica a livello di database: un secondo tentativo di
        # scrittura concorrente su un'altra sessione fallisce con
        # DuplicateKeyError invece di lasciare una finestra di race fra un
        # controllo applicativo e la scrittura successiva.
        await sessions_collection.create_index(
            [("status", 1)],
            name="ix_single_running_session",
            unique=True,
            partialFilterExpression={"status": RunStatus.RUNNING.value},
        )
    except PyMongoError as exc:
        logger.warning("Impossibile creare gli indici su '%s': %s", SESSIONS, exc)
    else:
        logger.info("Indici su '%s' verificati.", SESSIONS)


async def close_mongo_connection() -> None:
    """Chiude la connessione a MongoDB.

    Riceve:
        Nulla.

    Restituisce:
        ``None``.

    Fa:
        Rilascia il client motor e azzera i riferimenti globali. Invocata allo
        shutdown dell'applicazione.
    """
    global _client, _database

    if _client is not None:
        _client.close()
        logger.info("Connessione a MongoDB chiusa.")
    _client = None
    _database = None


def get_database() -> AsyncIOMotorDatabase:
    """Restituisce l'handle del database applicativo.

    Riceve:
        Nulla.

    Restituisce:
        L'oggetto ``AsyncIOMotorDatabase`` corrente.

    Fa:
        Solleva ``DatabaseError`` se la connessione non è stata inizializzata
        (chiamata fuori dal ciclo di vita dell'applicazione).
    """
    if _database is None:
        raise DatabaseError("Connessione al database non inizializzata.")
    return _database


def get_collection(name: str) -> AsyncIOMotorCollection:
    """Restituisce una collezione del database applicativo.

    Riceve:
        name: nome della collezione (usare le costanti di ``app.db.collections``).

    Restituisce:
        L'oggetto ``AsyncIOMotorCollection`` richiesto.

    Fa:
        Delega a ``get_database`` e indicizza per nome.
    """
    return get_database()[name]


async def ping() -> None:
    """Verifica che il database risponda.

    Riceve:
        Nulla.

    Restituisce:
        ``None`` se il database risponde.

    Fa:
        Esegue il comando ``ping`` sul db ``admin`` e converte qualunque errore
        di pymongo in ``DatabaseError``. Usato dall'endpoint di health check.
    """
    if _client is None:
        raise DatabaseError("Connessione al database non inizializzata.")
    try:
        await _client.admin.command("ping")
    except PyMongoError as exc:
        raise DatabaseError(f"MongoDB non raggiungibile su {settings.mongo_dsn}.") from exc
