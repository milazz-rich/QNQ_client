"""Eccezioni applicative e gestione centralizzata degli errori.

Ogni risposta di errore dell'API ha la stessa forma::

    {"error": {"code": "NOT_FOUND", "message": "...", "details": null}}
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError as PydanticValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings

logger = logging.getLogger(__name__)


class AppError(Exception):
    """Errore applicativo di dominio, base di tutte le eccezioni gestite.

    Attributi:
        status_code: codice HTTP con cui rispondere.
        code: codice stabile in SCREAMING_SNAKE_CASE destinato al client.
        message: messaggio leggibile.
        details: informazioni aggiuntive opzionali.
    """

    status_code: int = 500
    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFoundError(AppError):
    """Risorsa richiesta inesistente."""

    status_code = 404
    code = "NOT_FOUND"


class ValidationError(AppError):
    """Input sintatticamente valido ma non accettabile dal dominio."""

    status_code = 422
    code = "VALIDATION_ERROR"


class ConflictError(AppError):
    """Operazione in conflitto con lo stato corrente (es. duplicato)."""

    status_code = 409
    code = "CONFLICT"


class DatabaseError(AppError):
    """Il database non è raggiungibile o ha rifiutato l'operazione."""

    status_code = 503
    code = "DATABASE_UNAVAILABLE"


def error_body(code: str, message: str, details: Any = None) -> dict[str, Any]:
    """Costruisce il corpo di una risposta di errore.

    Riceve:
        code: codice stabile dell'errore.
        message: messaggio leggibile.
        details: informazioni aggiuntive opzionali (default ``None``).

    Restituisce:
        Il dizionario nel formato di errore comune a tutta l'API.

    Fa:
        Centralizza la forma della risposta così che nessun handler la reinventi.
    """
    return {"error": {"code": code, "message": message, "details": details}}


def register_exception_handlers(app: FastAPI) -> None:
    """Registra tutti gli exception handler sull'applicazione.

    Riceve:
        app: l'istanza FastAPI su cui installare gli handler.

    Restituisce:
        ``None``.

    Fa:
        Aggancia gli handler per ``AppError``, ``RequestValidationError``,
        ``HTTPException``, ``pydantic.ValidationError`` e per qualunque eccezione
        non prevista, garantendo che ogni errore esca nel formato comune.
    """

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        """Traduce un errore di dominio nella risposta HTTP corrispondente.

        Riceve:
            _: la richiesta in corso (non usata).
            exc: l'eccezione applicativa sollevata da router o service.

        Restituisce:
            Una ``JSONResponse`` con lo status code dell'eccezione.

        Fa:
            Usa ``status_code`` e ``code`` dichiarati dalla classe di eccezione.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(exc.code, exc.message, exc.details),
        )

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        """Uniforma gli errori di validazione del payload in ingresso.

        Riceve:
            _: la richiesta in corso (non usata).
            exc: l'errore di validazione prodotto da FastAPI/Pydantic.

        Restituisce:
            Una ``JSONResponse`` 422 con l'elenco dei campi non validi in ``details``.

        Fa:
            Sostituisce il formato di default di FastAPI (``{"detail": [...]}``)
            con quello comune dell'API.
        """
        return JSONResponse(
            status_code=422,
            content=error_body(
                "VALIDATION_ERROR",
                "Il payload della richiesta non è valido.",
                _serialize_errors(exc.errors()),
            ),
        )

    @app.exception_handler(PydanticValidationError)
    async def _handle_pydantic_validation(
        _: Request, exc: PydanticValidationError
    ) -> JSONResponse:
        """Gestisce errori di validazione Pydantic sollevati fuori dal parsing HTTP.

        Riceve:
            _: la richiesta in corso (non usata).
            exc: l'errore di validazione (tipicamente su dati letti dal database).

        Restituisce:
            Una ``JSONResponse`` 422 con l'elenco dei problemi.

        Fa:
            Copre il caso di documenti Mongo non conformi al modello, che
            altrimenti degenererebbe in un 500 opaco.
        """
        return JSONResponse(
            status_code=422,
            content=error_body(
                "VALIDATION_ERROR",
                "I dati non rispettano il modello atteso.",
                _serialize_errors(exc.errors()),
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(
        _: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        """Riformatta le ``HTTPException`` (incluso il 404 di rotta inesistente).

        Riceve:
            _: la richiesta in corso (non usata).
            exc: l'eccezione HTTP sollevata da FastAPI o da Starlette.

        Restituisce:
            Una ``JSONResponse`` con lo status code originale e il corpo comune.

        Fa:
            Deriva il ``code`` dallo status (404 → ``NOT_FOUND``, ecc.) così che
            anche gli errori del framework rispettino il contratto.
        """
        return JSONResponse(
            status_code=exc.status_code,
            content=error_body(
                _CODE_BY_STATUS.get(exc.status_code, "HTTP_ERROR"),
                str(exc.detail),
            ),
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
        """Rete di sicurezza per qualunque eccezione non prevista.

        Riceve:
            request: la richiesta in corso, usata per il logging.
            exc: l'eccezione non gestita.

        Restituisce:
            Una ``JSONResponse`` 500 nel formato comune.

        Fa:
            Registra lo stack trace completo nei log ed espone il messaggio
            tecnico solo in ambiente di sviluppo, per non far trapelare
            dettagli interni in produzione.
        """
        logger.exception("Errore non gestito su %s %s", request.method, request.url.path)
        details = {"exception": type(exc).__name__, "message": str(exc)}
        return JSONResponse(
            status_code=500,
            content=error_body(
                "INTERNAL_ERROR",
                "Errore interno del server.",
                details if settings.is_development else None,
            ),
        )


_CODE_BY_STATUS: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "UNAUTHORIZED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
    503: "SERVICE_UNAVAILABLE",
}


def _serialize_errors(errors: list[Any]) -> list[dict[str, Any]]:
    """Rende serializzabili in JSON gli errori di validazione Pydantic.

    Riceve:
        errors: la lista restituita da ``exc.errors()``.

    Restituisce:
        Una lista di dizionari con ``field``, ``message`` e ``type``.

    Fa:
        Elimina la chiave ``ctx``/``input``, che può contenere oggetti non
        serializzabili (es. ``ObjectId`` o eccezioni), e appiattisce ``loc``
        in un percorso puntato leggibile dal client.
    """
    return [
        {
            "field": ".".join(str(part) for part in err.get("loc", ())),
            "message": err.get("msg", ""),
            "type": err.get("type", ""),
        }
        for err in errors
    ]
