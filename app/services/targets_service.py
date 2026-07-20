"""Logica di business dell'entità Target: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError, NotFoundError
from app.db.collections import TARGETS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.target import Target, TargetCreate, TargetUpdate

logger = logging.getLogger(__name__)


async def list_targets() -> list[Target]:
    """Elenca tutti i target registrati.

    Riceve:
        Nulla.

    Restituisce:
        La lista dei ``Target``, ordinata per nome crescente.

    Fa:
        Legge l'intera collezione ``targets`` e converte ogni documento nel
        modello Pydantic. Solleva ``DatabaseError`` se Mongo non risponde.
    """
    collection = get_collection(TARGETS)
    try:
        documents = await collection.find().sort("name", 1).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere i target dal database.") from exc
    return [Target.model_validate(document) for document in documents]


async def get_target(target_id: str) -> Target:
    """Recupera un singolo target per identificativo.

    Riceve:
        target_id: identificativo del target come stringa esadecimale a 24 caratteri.

    Restituisce:
        Il ``Target`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato),
        interroga la collezione ``targets`` e solleva ``NotFoundError`` se il
        documento non esiste.
    """
    collection = get_collection(TARGETS)
    object_id = to_object_id(target_id, "Id del target")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere il target dal database.") from exc
    if document is None:
        raise NotFoundError(f"Target '{target_id}' non trovato.")
    return Target.model_validate(document)


async def create_target(payload: TargetCreate) -> Target:
    """Crea un nuovo target.

    Riceve:
        payload: i dati del target validati da ``TargetCreate``.

    Restituisce:
        Il ``Target`` creato, completo dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``targets`` e ricostruisce il
        modello con l'``_id`` assegnato, senza rileggerlo dal database.
    """
    collection = get_collection(TARGETS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare il target.") from exc
    logger.info("Target creato: %s (%s)", payload.name, insert_result.inserted_id)
    return Target.model_validate({**document, "_id": insert_result.inserted_id})


async def update_target(target_id: str, payload: TargetUpdate) -> Target:
    """Aggiorna i campi valorizzati di un target esistente.

    Riceve:
        target_id: identificativo del target da aggiornare.
        payload: i campi da modificare; quelli omessi restano invariati.

    Restituisce:
        Il ``Target`` nello stato successivo all'aggiornamento.

    Fa:
        Esegue un ``find_one_and_update`` con ``$set`` sui soli campi presenti
        nella richiesta. Solleva ``NotFoundError`` se il target non esiste e
        ``ValidationError`` se il payload non contiene alcun campo.
    """
    from app.core.errors import ValidationError

    collection = get_collection(TARGETS)
    object_id = to_object_id(target_id, "Id del target")
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
        raise DatabaseError("Impossibile aggiornare il target.") from exc
    if document is None:
        raise NotFoundError(f"Target '{target_id}' non trovato.")
    logger.info("Target aggiornato: %s (campi: %s)", target_id, ", ".join(changes))
    return Target.model_validate(document)


async def delete_target(target_id: str) -> None:
    """Elimina un target.

    Riceve:
        target_id: identificativo del target da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Cancella il documento dalla collezione ``targets`` e solleva
        ``NotFoundError`` se non esisteva, così che il client distingua una
        cancellazione effettiva da un id sbagliato.
    """
    collection = get_collection(TARGETS)
    object_id = to_object_id(target_id, "Id del target")
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare il target.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"Target '{target_id}' non trovato.")
    logger.info("Target eliminato: %s", target_id)
