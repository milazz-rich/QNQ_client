"""Logica di business dell'entità Session: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError, NotFoundError, ValidationError
from app.db.collections import SESSIONS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.session import RunStatus, Session, SessionCreate, SessionUpdate
from app.services import results_service

logger = logging.getLogger(__name__)


async def list_sessions() -> list[Session]:
    """Elenca tutte le sessioni registrate.

    Riceve:
        Nulla.

    Restituisce:
        La lista delle ``Session``, dalla più recente alla più vecchia.

    Fa:
        Legge l'intera collezione ``sessions`` ordinando per ``when``
        decrescente, che è l'ordine con cui il frontend le presenta.
    """
    collection = get_collection(SESSIONS)
    try:
        documents = await collection.find().sort("when", -1).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere le sessioni dal database.") from exc
    return [Session.model_validate(document) for document in documents]


async def get_session(session_id: str) -> Session:
    """Recupera una singola sessione per identificativo.

    Riceve:
        session_id: identificativo della sessione come stringa esadecimale a 24 caratteri.

    Restituisce:
        La ``Session`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato),
        interroga la collezione ``sessions`` e solleva ``NotFoundError`` se il
        documento non esiste.
    """
    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere la sessione dal database.") from exc
    if document is None:
        raise NotFoundError(f"Session '{session_id}' non trovata.")
    return Session.model_validate(document)


async def create_session(payload: SessionCreate) -> Session:
    """Crea una nuova sessione.

    Riceve:
        payload: i dati della sessione validati da ``SessionCreate``.

    Restituisce:
        La ``Session`` creata, completa dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``sessions`` e ricostruisce il
        modello con l'``_id`` assegnato, senza rileggerlo dal database.
    """
    collection = get_collection(SESSIONS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare la sessione.") from exc
    logger.info("Session creata: %s (%s)", payload.name, insert_result.inserted_id)
    return Session.model_validate({**document, "_id": insert_result.inserted_id})


async def update_session(session_id: str, payload: SessionUpdate) -> Session:
    """Aggiorna i campi valorizzati di una sessione esistente.

    Riceve:
        session_id: identificativo della sessione da aggiornare.
        payload: i campi da modificare; quelli omessi restano invariati.

    Restituisce:
        La ``Session`` nello stato successivo all'aggiornamento.

    Fa:
        Esegue un ``find_one_and_update`` con ``$set`` sui soli campi presenti
        nella richiesta. Solleva ``NotFoundError`` se la sessione non esiste e
        ``ValidationError`` se il payload non contiene alcun campo.
    """
    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
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
        raise DatabaseError("Impossibile aggiornare la sessione.") from exc
    if document is None:
        raise NotFoundError(f"Session '{session_id}' non trovata.")
    logger.info("Session aggiornata: %s (campi: %s)", session_id, ", ".join(changes))
    return Session.model_validate(document)


async def delete_session(session_id: str) -> None:
    """Elimina una sessione e, a cascata, tutti i suoi ``Result``.

    Riceve:
        session_id: identificativo della sessione da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Cancella il documento dalla collezione ``sessions`` **e** tutti i
        ``Result`` con quel ``sessionId``. Il filtro sui risultati usa
        ``sessionId`` (non ``sessionItemId``): lo stesso ``SessionItem`` può
        essere condiviso da più sessioni, e cancellare per ``sessionItemId``
        eliminerebbe anche i risultati di sessioni ancora esistenti.

        MongoDB gira qui in configurazione standalone, che **non** supporta le
        transazioni multi-documento; l'operazione è quindi resa coerente
        dall'ordine, non da una transazione. I risultati sono cancellati
        **prima** della sessione: se la cancellazione della sessione fallisce,
        la sessione resta e l'operazione è ripetibile; l'ordine inverso
        lascerebbe invece risultati orfani non più raggiungibili. Nel caso
        normale di sessione inesistente non ci sono risultati con quel
        ``sessionId`` da cancellare, quindi la ``delete_many`` è un no-op e la
        ``NotFoundError`` viene sollevata correttamente sulla sessione.
    """
    object_id = to_object_id(session_id, "Id della sessione")

    deleted_results = await results_service.delete_results_by_session(session_id)

    collection = get_collection(SESSIONS)
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare la sessione.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"Session '{session_id}' non trovata.")
    logger.info("Session eliminata: %s (con %d risultati a cascata)", session_id, deleted_results)


async def set_status(session_id: str, status: RunStatus) -> None:
    """Imposta lo stato complessivo di una sessione.

    Riceve:
        session_id: identificativo della sessione.
        status: il nuovo stato.

    Restituisce:
        ``None``.

    Fa:
        Aggiorna il solo campo ``status``. Usata dal session runner durante
        l'esecuzione in background.
    """
    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
    try:
        await collection.update_one({"_id": object_id}, {"$set": {"status": status.value}})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile aggiornare lo stato della sessione.") from exc


async def set_current_index(session_id: str, index: int) -> None:
    """Imposta l'indice dell'item attualmente in esecuzione.

    Riceve:
        session_id: identificativo della sessione.
        index: indice dell'item nella lista ``items``.

    Restituisce:
        ``None``.

    Fa:
        Aggiorna il solo campo ``currentIndex``, letto dal frontend in polling
        per evidenziare l'item in corso.
    """
    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
    try:
        await collection.update_one({"_id": object_id}, {"$set": {"currentIndex": index}})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile aggiornare l'indice della sessione.") from exc


async def update_item_progress(
    session_id: str,
    index: int,
    *,
    done: int | None = None,
    total: int | None = None,
    status: RunStatus | None = None,
) -> None:
    """Aggiorna l'avanzamento di un singolo item della sessione.

    Riceve:
        session_id: identificativo della sessione.
        index: posizione dell'item nella lista ``items``.
        done: ripetizioni completate, se da aggiornare.
        total: ripetizioni previste, se da aggiornare.
        status: stato dell'item, se da aggiornare.

    Restituisce:
        ``None``.

    Fa:
        Aggiorna in modo mirato i soli campi indicati usando la notazione
        posizionale ``items.<index>.<campo>``, senza riscrivere l'intero array:
        così l'aggiornamento resta atomico e non sovrascrive il lavoro fatto
        sugli altri item. Se non viene indicato alcun campo non tocca il database.
    """
    changes: dict[str, object] = {}
    if done is not None:
        changes[f"items.{index}.done"] = done
    if total is not None:
        changes[f"items.{index}.total"] = total
    if status is not None:
        changes[f"items.{index}.status"] = status.value
    if not changes:
        return

    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
    try:
        await collection.update_one({"_id": object_id}, {"$set": changes})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile aggiornare l'avanzamento della sessione.") from exc
