"""Router HTTP dell'entità Target: CRUD su ``/api/targets``.

Il router non tocca MongoDB: valida l'input, delega a ``targets_service`` e
lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.
"""

from fastapi import APIRouter, Path, status

from app.models.common import ErrorResponse
from app.models.target import Target, TargetCreate, TargetUpdate
from app.services import targets_service

router = APIRouter(
    prefix="/targets",
    tags=["targets"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_TargetId = Path(description="Identificativo del target (24 caratteri esadecimali)")


@router.get("", response_model=list[Target], summary="Elenca i target")
async def list_targets() -> list[Target]:
    """Restituisce tutti i target registrati.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista dei target ordinata per nome.

    Fa:
        Delega a ``targets_service.list_targets``.
    """
    return await targets_service.list_targets()


@router.get(
    "/{target_id}",
    response_model=Target,
    summary="Recupera un target",
    responses={404: {"model": ErrorResponse, "description": "Target inesistente"}},
)
async def get_target(target_id: str = _TargetId) -> Target:
    """Restituisce un singolo target.

    Riceve:
        target_id: identificativo del target, dal path.

    Restituisce:
        ``200`` con il target richiesto.

    Fa:
        Delega a ``targets_service.get_target``; un id inesistente produce un
        ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await targets_service.get_target(target_id)


@router.post(
    "",
    response_model=Target,
    status_code=status.HTTP_201_CREATED,
    summary="Crea un target",
)
async def create_target(payload: TargetCreate) -> Target:
    """Crea un nuovo target.

    Riceve:
        payload: il corpo della richiesta, validato come ``TargetCreate``.

    Restituisce:
        ``201`` con il target creato, comprensivo dell'``id`` generato da MongoDB.

    Fa:
        Delega a ``targets_service.create_target``.
    """
    return await targets_service.create_target(payload)


@router.put(
    "/{target_id}",
    response_model=Target,
    summary="Aggiorna un target",
    responses={404: {"model": ErrorResponse, "description": "Target inesistente"}},
)
async def update_target(payload: TargetUpdate, target_id: str = _TargetId) -> Target:
    """Aggiorna i campi valorizzati di un target esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        target_id: identificativo del target, dal path.

    Restituisce:
        ``200`` con il target aggiornato.

    Fa:
        Delega a ``targets_service.update_target``. Un payload vuoto produce un
        ``422 VALIDATION_ERROR``, un id inesistente un ``404 NOT_FOUND``.
    """
    return await targets_service.update_target(target_id, payload)


@router.delete(
    "/{target_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina un target",
    responses={404: {"model": ErrorResponse, "description": "Target inesistente"}},
)
async def delete_target(target_id: str = _TargetId) -> None:
    """Elimina un target.

    Riceve:
        target_id: identificativo del target, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``targets_service.delete_target``; se il target non esisteva
        risponde ``404 NOT_FOUND`` invece di fingere un successo.
    """
    await targets_service.delete_target(target_id)
