"""Configurazione CORS per il dev server Angular."""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings

logger = logging.getLogger(__name__)


def setup_cors(app: FastAPI) -> None:
    """Abilita il CORS sull'applicazione.

    Riceve:
        app: l'istanza FastAPI da configurare.

    Restituisce:
        ``None``.

    Fa:
        Installa ``CORSMiddleware`` consentendo le origini elencate in
        ``settings.cors_origins`` (di default ``http://localhost:4200``, il dev
        server Angular), con credenziali, metodi e header liberi.
    """
    logger.info("CORS abilitato per le origini: %s", ", ".join(settings.cors_origins))
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
