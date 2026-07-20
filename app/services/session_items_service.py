"""Logica di business dell'entità SessionItem: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError, NotFoundError, ValidationError
from app.db.collections import SESSION_ITEMS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.session_item import SessionItem, SessionItemCreate, SessionItemUpdate

logger = logging.getLogger(__name__)


async def list_session_items() -> list[SessionItem]:
    """Elenca tutti i session item registrati.

    Riceve:
        Nulla.

    Restituisce:
        La lista dei ``SessionItem``, in ordine di inserimento.

    Fa:
        Legge l'intera collezione ``session_items`` e converte ogni documento
        nel modello Pydantic. Solleva ``DatabaseError`` se Mongo non risponde.
    """
    collection = get_collection(SESSION_ITEMS)
    try:
        documents = await collection.find().to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere i session item dal database.") from exc
    return [SessionItem.model_validate(document) for document in documents]


async def get_session_item(session_item_id: str) -> SessionItem:
    """Recupera un singolo session item per identificativo.

    Riceve:
        session_item_id: identificativo del session item come stringa esadecimale
            a 24 caratteri.

    Restituisce:
        Il ``SessionItem`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato),
        interroga la collezione ``session_items`` e solleva ``NotFoundError`` se
        il documento non esiste.
    """
    collection = get_collection(SESSION_ITEMS)
    object_id = to_object_id(session_item_id, "Id del session item")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere il session item dal database.") from exc
    if document is None:
        raise NotFoundError(f"SessionItem '{session_item_id}' non trovato.")
    return SessionItem.model_validate(document)


async def create_session_item(payload: SessionItemCreate) -> SessionItem:
    """Crea un nuovo session item.

    Riceve:
        payload: i dati del session item validati da ``SessionItemCreate``.

    Restituisce:
        Il ``SessionItem`` creato, completo dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``session_items`` e ricostruisce
        il modello con l'``_id`` assegnato, senza rileggerlo dal database.
    """
    collection = get_collection(SESSION_ITEMS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare il session item.") from exc
    logger.info("SessionItem creato: %s", insert_result.inserted_id)
    return SessionItem.model_validate({**document, "_id": insert_result.inserted_id})


async def create_session_items_batch(payloads: list[SessionItemCreate]) -> list[str]:
    """Crea più session item in un'unica operazione.

    Riceve:
        payloads: la lista dei session item da creare, tipicamente il prodotto
            cartesiano Target × Scenario generato dal wizard di creazione
            sessione nel frontend.

    Restituisce:
        La lista degli ``id`` (stringa) assegnati da MongoDB, nello stesso
        ordine dei payload in ingresso.

    Fa:
        Esegue un ``insert_many`` sulla collezione ``session_items``. Solleva
        ``ValidationError`` se la lista è vuota, senza interrogare il database.
    """
    if not payloads:
        raise ValidationError("La lista di session item da creare non può essere vuota.")

    collection = get_collection(SESSION_ITEMS)
    documents = [payload.model_dump(by_alias=True, mode="json") for payload in payloads]
    try:
        insert_result = await collection.insert_many(documents)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare i session item in batch.") from exc
    logger.info("SessionItem creati in batch: %d", len(insert_result.inserted_ids))
    return [str(inserted_id) for inserted_id in insert_result.inserted_ids]


async def update_session_item(session_item_id: str, payload: SessionItemUpdate) -> SessionItem:
    """Aggiorna i campi valorizzati di un session item esistente.

    Riceve:
        session_item_id: identificativo del session item da aggiornare.
        payload: i campi da modificare; quelli omessi restano invariati.

    Restituisce:
        Il ``SessionItem`` nello stato successivo all'aggiornamento.

    Fa:
        Esegue un ``find_one_and_update`` con ``$set`` sui soli campi presenti
        nella richiesta. Solleva ``NotFoundError`` se il session item non esiste
        e ``ValidationError`` se il payload non contiene alcun campo.
    """
    collection = get_collection(SESSION_ITEMS)
    object_id = to_object_id(session_item_id, "Id del session item")
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
        raise DatabaseError("Impossibile aggiornare il session item.") from exc
    if document is None:
        raise NotFoundError(f"SessionItem '{session_item_id}' non trovato.")
    logger.info("SessionItem aggiornato: %s (campi: %s)", session_item_id, ", ".join(changes))
    return SessionItem.model_validate(document)


async def delete_session_item(session_item_id: str) -> None:
    """Elimina un session item.

    Riceve:
        session_item_id: identificativo del session item da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Cancella il documento dalla collezione ``session_items`` e solleva
        ``NotFoundError`` se non esisteva, così che il client distingua una
        cancellazione effettiva da un id sbagliato.
    """
    collection = get_collection(SESSION_ITEMS)
    object_id = to_object_id(session_item_id, "Id del session item")
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare il session item.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"SessionItem '{session_item_id}' non trovato.")
    logger.info("SessionItem eliminato: %s", session_item_id)
