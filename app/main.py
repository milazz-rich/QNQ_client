"""Costruzione dell'applicazione FastAPI: ciclo di vita, CORS, errori, router."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import settings
from app.core.cors import setup_cors
from app.core.errors import register_exception_handlers
from app.core.session_logging import LOG_FORMAT, setup_session_logging
from app.db.mongo import close_mongo_connection, connect_to_mongo, ensure_indexes
from app.services.measurement.firefox_client import cleanup_stale_run_profiles
from app.routers import (
    clients,
    health,
    results,
    scenarios,
    session_items,
    sessions,
    targets,
)

logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Gestisce il ciclo di vita dell'applicazione.

    Riceve:
        _: l'istanza FastAPI (non usata).

    Restituisce:
        Un async context manager che cede il controllo mentre l'app è in esecuzione.

    Fa:
        All'avvio apre la connessione a MongoDB, verifica gli indici della
        collezione ``results`` (operazione idempotente, vedi
        ``ensure_indexes``), installa l'handler che cattura su file i log di
        ogni sessione (§5.10) e rimuove le copie temporanee di profilo Firefox
        rimaste da un'esecuzione interrotta bruscamente (§5.7): all'avvio
        nessuna misura è in corso, quindi tutto ciò che resta in
        ``.runtime/firefox/runs/`` è per definizione un residuo. Allo
        spegnimento chiude la connessione.
    """
    await connect_to_mongo()
    await ensure_indexes()
    setup_session_logging()
    cleanup_stale_run_profiles()
    yield
    await close_mongo_connection()


def create_app() -> FastAPI:
    """Costruisce e configura l'applicazione FastAPI.

    Riceve:
        Nulla; legge la configurazione da ``settings``.

    Restituisce:
        L'istanza ``FastAPI`` pronta per essere servita da uvicorn.

    Fa:
        Imposta metadati e lifespan, abilita il CORS, registra gli exception
        handler centralizzati e monta tutti i router sotto ``settings.api_prefix``.
    """
    app = FastAPI(
        title=settings.app_name,
        description="Backend per il confronto prestazionale HTTP/2 vs HTTP/3.",
        version="0.1.0",
        lifespan=lifespan,
    )

    setup_cors(app)
    register_exception_handlers(app)

    app.include_router(health.router, prefix=settings.api_prefix)
    app.include_router(targets.router, prefix=settings.api_prefix)
    app.include_router(scenarios.router, prefix=settings.api_prefix)
    app.include_router(clients.router, prefix=settings.api_prefix)
    app.include_router(session_items.router, prefix=settings.api_prefix)
    app.include_router(sessions.router, prefix=settings.api_prefix)
    app.include_router(results.router, prefix=settings.api_prefix)

    logger.info("Applicazione inizializzata (env=%s).", settings.app_env)
    return app


app = create_app()
