"""Router HTTP dell'entità Client: CRUD su ``/api/clients``.

Il router non tocca MongoDB: valida l'input, delega a ``clients_service`` e
lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.
"""

from fastapi import APIRouter, Path, status

from app.models.client import Client, ClientCreate, ClientUpdate
from app.models.common import ErrorResponse
from app.services import clients_service

router = APIRouter(
    prefix="/clients",
    tags=["clients"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_ClientId = Path(description="Identificativo del client (24 caratteri esadecimali)")


@router.get("", response_model=list[Client], summary="Elenca i client")
async def list_clients() -> list[Client]:
    """Restituisce tutti i client registrati.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista dei client ordinata per nome.

    Fa:
        Delega a ``clients_service.list_clients``.
    """
    return await clients_service.list_clients()


@router.get(
    "/{client_id}",
    response_model=Client,
    summary="Recupera un client",
    responses={404: {"model": ErrorResponse, "description": "Client inesistente"}},
)
async def get_client(client_id: str = _ClientId) -> Client:
    """Restituisce un singolo client.

    Riceve:
        client_id: identificativo del client, dal path.

    Restituisce:
        ``200`` con il client richiesto.

    Fa:
        Delega a ``clients_service.get_client``; un id inesistente produce un
        ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await clients_service.get_client(client_id)


@router.post(
    "",
    response_model=Client,
    status_code=status.HTTP_201_CREATED,
    summary="Crea un client",
)
async def create_client(payload: ClientCreate) -> Client:
    """Crea un nuovo client.

    Riceve:
        payload: il corpo della richiesta, validato come ``ClientCreate``.

    Restituisce:
        ``201`` con il client creato, comprensivo dell'``id`` generato da MongoDB.

    Fa:
        Delega a ``clients_service.create_client``.
    """
    return await clients_service.create_client(payload)


@router.put(
    "/{client_id}",
    response_model=Client,
    summary="Aggiorna un client",
    responses={404: {"model": ErrorResponse, "description": "Client inesistente"}},
)
async def update_client(payload: ClientUpdate, client_id: str = _ClientId) -> Client:
    """Aggiorna i campi valorizzati di un client esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        client_id: identificativo del client, dal path.

    Restituisce:
        ``200`` con il client aggiornato.

    Fa:
        Delega a ``clients_service.update_client``. Un payload vuoto produce un
        ``422 VALIDATION_ERROR``, un id inesistente un ``404 NOT_FOUND``.
    """
    return await clients_service.update_client(client_id, payload)


@router.delete(
    "/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina un client",
    responses={404: {"model": ErrorResponse, "description": "Client inesistente"}},
)
async def delete_client(client_id: str = _ClientId) -> None:
    """Elimina un client.

    Riceve:
        client_id: identificativo del client, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``clients_service.delete_client``; se il client non esisteva
        risponde ``404 NOT_FOUND`` invece di fingere un successo.
    """
    await clients_service.delete_client(client_id)
