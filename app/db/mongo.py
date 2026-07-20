"""Ciclo di vita della connessione a MongoDB tramite il driver asincrono motor."""

import logging

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo.errors import PyMongoError

from app.core.config import settings
from app.core.errors import DatabaseError

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
