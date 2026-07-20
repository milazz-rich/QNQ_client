"""Router HTTP dell'entità SessionItem: CRUD su ``/api/session-items``.

Il router non tocca MongoDB: valida l'input, delega a ``session_items_service``
e lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.

L'endpoint ``POST /session-items/batch`` è dichiarato *prima* di
``POST /session-items/{session_item_id}`` nel file, ma la sua rotta è statica
(``/batch``) e quella parametrica (``/{session_item_id}``) è su ``GET``, quindi
non c'è ambiguità di instradamento fra le due.
"""

from fastapi import APIRouter, Path, status

from app.models.common import ErrorResponse
from app.models.session_item import (
    SessionItem,
    SessionItemBatchResult,
    SessionItemCreate,
    SessionItemUpdate,
)
from app.services import session_items_service

router = APIRouter(
    prefix="/session-items",
    tags=["session-items"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_SessionItemId = Path(description="Identificativo del session item (24 caratteri esadecimali)")


@router.get("", response_model=list[SessionItem], summary="Elenca i session item")
async def list_session_items() -> list[SessionItem]:
    """Restituisce tutti i session item registrati.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista dei session item.

    Fa:
        Delega a ``session_items_service.list_session_items``.
    """
    return await session_items_service.list_session_items()


@router.get(
    "/{session_item_id}",
    response_model=SessionItem,
    summary="Recupera un session item",
    responses={404: {"model": ErrorResponse, "description": "SessionItem inesistente"}},
)
async def get_session_item(session_item_id: str = _SessionItemId) -> SessionItem:
    """Restituisce un singolo session item.

    Riceve:
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``200`` con il session item richiesto.

    Fa:
        Delega a ``session_items_service.get_session_item``; un id inesistente
        produce un ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await session_items_service.get_session_item(session_item_id)


@router.post(
    "",
    response_model=SessionItem,
    status_code=status.HTTP_201_CREATED,
    summary="Crea un session item",
)
async def create_session_item(payload: SessionItemCreate) -> SessionItem:
    """Crea un nuovo session item.

    Riceve:
        payload: il corpo della richiesta, validato come ``SessionItemCreate``.

    Restituisce:
        ``201`` con il session item creato, comprensivo dell'``id`` generato da
        MongoDB.

    Fa:
        Delega a ``session_items_service.create_session_item``.
    """
    return await session_items_service.create_session_item(payload)


@router.post(
    "/batch",
    response_model=SessionItemBatchResult,
    status_code=status.HTTP_201_CREATED,
    summary="Crea più session item in un'unica chiamata",
)
async def create_session_items_batch(payload: list[SessionItemCreate]) -> SessionItemBatchResult:
    """Crea più session item in un'unica operazione.

    Riceve:
        payload: la lista di ``SessionItemCreate`` da creare, tipicamente il
            prodotto cartesiano Target × Scenario generato dal wizard di
            creazione sessione nel frontend.

    Restituisce:
        ``201`` con gli ``id`` creati, nello stesso ordine dei payload in ingresso.

    Fa:
        Delega a ``session_items_service.create_session_items_batch``. Una
        lista vuota produce un ``422 VALIDATION_ERROR`` senza toccare il database.
    """
    ids = await session_items_service.create_session_items_batch(payload)
    return SessionItemBatchResult(ids=ids)


@router.put(
    "/{session_item_id}",
    response_model=SessionItem,
    summary="Aggiorna un session item",
    responses={404: {"model": ErrorResponse, "description": "SessionItem inesistente"}},
)
async def update_session_item(
    payload: SessionItemUpdate, session_item_id: str = _SessionItemId
) -> SessionItem:
    """Aggiorna i campi valorizzati di un session item esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``200`` con il session item aggiornato.

    Fa:
        Delega a ``session_items_service.update_session_item``. Un payload
        vuoto produce un ``422 VALIDATION_ERROR``, un id inesistente un
        ``404 NOT_FOUND``.
    """
    return await session_items_service.update_session_item(session_item_id, payload)


@router.delete(
    "/{session_item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina un session item",
    responses={404: {"model": ErrorResponse, "description": "SessionItem inesistente"}},
)
async def delete_session_item(session_item_id: str = _SessionItemId) -> None:
    """Elimina un session item.

    Riceve:
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``session_items_service.delete_session_item``; se il session
        item non esisteva risponde ``404 NOT_FOUND`` invece di fingere un successo.
    """
    await session_items_service.delete_session_item(session_item_id)
