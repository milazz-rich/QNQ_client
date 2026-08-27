"""Logica di business dell'entità Client: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError, NotFoundError, ValidationError
from app.db.collections import CLIENTS
from app.db.mongo import get_collection
from app.models.client import Client, ClientCreate, ClientUpdate
from app.models.common import to_object_id

logger = logging.getLogger(__name__)


async def list_clients() -> list[Client]:
    """Elenca tutti i client registrati.

    Riceve:
        Nulla.

    Restituisce:
        La lista dei ``Client``, ordinata per nome crescente.

    Fa:
        Legge l'intera collezione ``clients`` e converte ogni documento nel
        modello Pydantic. Solleva ``DatabaseError`` se Mongo non risponde.
    """
    collection = get_collection(CLIENTS)
    try:
        documents = await collection.find().sort("name", 1).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere i client dal database.") from exc
    return [Client.model_validate(document) for document in documents]


async def get_client(client_id: str) -> Client:
    """Recupera un singolo client per identificativo.

    Riceve:
        client_id: identificativo del client come stringa esadecimale a 24 caratteri.

    Restituisce:
        Il ``Client`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato),
        interroga la collezione ``clients`` e solleva ``NotFoundError`` se il
        documento non esiste.
    """
    collection = get_collection(CLIENTS)
    object_id = to_object_id(client_id, "Id del client")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere il client dal database.") from exc
    if document is None:
        raise NotFoundError(f"Client '{client_id}' non trovato.")
    return Client.model_validate(document)


async def create_client(payload: ClientCreate) -> Client:
    """Crea un nuovo client.

    Riceve:
        payload: i dati del client validati da ``ClientCreate``.

    Restituisce:
        Il ``Client`` creato, completo dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``clients`` e ricostruisce il
        modello con l'``_id`` assegnato, senza rileggerlo dal database.
    """
    collection = get_collection(CLIENTS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare il client.") from exc
    logger.info("Client creato: %s (%s)", payload.name, insert_result.inserted_id)
    return Client.model_validate({**document, "_id": insert_result.inserted_id})


async def update_client(client_id: str, payload: ClientUpdate) -> Client:
    """Aggiorna i campi valorizzati di un client esistente.

    Riceve:
        client_id: identificativo del client da aggiornare.
        payload: i campi da modificare; quelli omessi restano invariati.

    Restituisce:
        Il ``Client`` nello stato successivo all'aggiornamento.

    Fa:
        Esegue un ``find_one_and_update`` con ``$set`` sui soli campi presenti
        nella richiesta. Solleva ``NotFoundError`` se il client non esiste e
        ``ValidationError`` se il payload non contiene alcun campo.
    """
    collection = get_collection(CLIENTS)
    object_id = to_object_id(client_id, "Id del client")
    changes = payload.model_dump(by_alias=True, mode="json", exclude_unset=True)
    if not changes:
        raise ValidationError("Nessun campo da aggiornare nella richiesta.")

    try:
        document = await collection.find_one_and_update(
            {"_id": object_id},
            {"$set": changes},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError as exc:
        raise DatabaseError("Impossibile aggiornare il client.") from exc
    if document is None:
        raise NotFoundError(f"Client '{client_id}' non trovato.")
    logger.info("Client aggiornato: %s (campi: %s)", client_id, ", ".join(changes))
    return Client.model_validate(document)


async def delete_client(client_id: str) -> None:
    """Elimina un client.

    Riceve:
        client_id: identificativo del client da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Cancella il documento dalla collezione ``clients`` e solleva
        ``NotFoundError`` se non esisteva, così che il client distingua una
        cancellazione effettiva da un id sbagliato.
    """
    collection = get_collection(CLIENTS)
    object_id = to_object_id(client_id, "Id del client")
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare il client.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"Client '{client_id}' non trovato.")
    logger.info("Client eliminato: %s", client_id)
