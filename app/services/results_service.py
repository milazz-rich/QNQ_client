"""Logica di business dell'entità Result: sola lettura più scrittura interna.

I ``Result`` non sono creati dal client HTTP ma prodotti dal session runner,
quindi l'API espone solo la lettura; ``create_result`` è usata internamente.
"""

import logging

from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError
from app.db.collections import RESULTS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.result import Result, ResultCreate

logger = logging.getLogger(__name__)


async def list_results(
    scenario_path: str | None = None,
    session_item_ids: list[str] | None = None,
    session_id: str | None = None,
) -> list[Result]:
    """Elenca i risultati, opzionalmente filtrati.

    Riceve:
        scenario_path: se valorizzato, restituisce solo i risultati di quello
            scenario (confronto esatto sul path richiesto).
        session_item_ids: se valorizzata, restituisce solo i risultati prodotti
            da quei session item.
        session_id: se valorizzato, restituisce solo i risultati prodotti da
            quella singola esecuzione di sessione. È il filtro **preferito** per
            "i risultati di questa sessione": diretto e senza l'ambiguità di
            ``session_item_ids``, dato che uno stesso ``SessionItem`` può essere
            condiviso fra più sessioni.

    Restituisce:
        La lista dei ``Result`` che soddisfano i filtri, ordinata per istante di
        completamento crescente.

    Fa:
        Compone la query Mongo combinando i filtri in AND. Gli id sono
        confrontati come stringhe, coerentemente con come il runner li salva.
        Una lista di id vuota è trattata come "nessun filtro", non come "nessun
        risultato", perché deriva da una query string vuota.
    """
    query: dict[str, object] = {}
    if scenario_path:
        query["scenarioPath"] = scenario_path
    if session_item_ids:
        query["sessionItemId"] = {"$in": session_item_ids}
    if session_id:
        query["sessionId"] = session_id

    collection = get_collection(RESULTS)
    try:
        documents = await collection.find(query).sort("time", 1).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere i risultati dal database.") from exc
    return [Result.model_validate(document) for document in documents]


async def get_result(result_id: str) -> Result:
    """Recupera un singolo risultato per identificativo.

    Riceve:
        result_id: identificativo del risultato come stringa esadecimale a 24 caratteri.

    Restituisce:
        Il ``Result`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato) e
        solleva ``NotFoundError`` se il documento non esiste.
    """
    from app.core.errors import NotFoundError

    collection = get_collection(RESULTS)
    object_id = to_object_id(result_id, "Id del risultato")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere il risultato dal database.") from exc
    if document is None:
        raise NotFoundError(f"Result '{result_id}' non trovato.")
    return Result.model_validate(document)


async def create_result(payload: ResultCreate) -> Result:
    """Salva un risultato prodotto da una misurazione.

    Riceve:
        payload: i dati del risultato, costruiti dal measurement service.

    Restituisce:
        Il ``Result`` salvato, completo dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``results``. Non è esposta come
        endpoint: i risultati nascono solo dall'esecuzione di una sessione.
    """
    collection = get_collection(RESULTS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile salvare il risultato.") from exc
    return Result.model_validate({**document, "_id": insert_result.inserted_id})


async def delete_results_by_session(session_id: str) -> int:
    """Elimina tutti i risultati prodotti da una singola esecuzione di sessione.

    Riceve:
        session_id: identificativo della sessione i cui risultati vanno eliminati.

    Restituisce:
        Il numero di risultati eliminati.

    Fa:
        Filtra per ``sessionId``, non per ``sessionItemId``: così la
        cancellazione a cascata di una sessione (vedi
        ``sessions_service.delete_session``) non tocca i risultati di altre
        sessioni che condividono lo stesso ``SessionItem``. Il confronto è per
        stringa, coerente con come il runner salva l'id.
    """
    collection = get_collection(RESULTS)
    try:
        delete_result = await collection.delete_many({"sessionId": session_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare i risultati della sessione.") from exc
    logger.info("Risultati eliminati per la sessione %s: %d", session_id, delete_result.deleted_count)
    return delete_result.deleted_count


async def delete_results_by_session_and_item(session_id: str, session_item_id: str) -> int:
    """Elimina i risultati di un item nell'ambito di una singola sessione.

    Riceve:
        session_id: identificativo della sessione in esecuzione.
        session_item_id: identificativo del session item.

    Restituisce:
        Il numero di risultati eliminati.

    Fa:
        Usata per ripulire i risultati di una **precedente esecuzione di questa
        stessa sessione** prima di rieseguire un item, così che un rilancio non
        accumuli duplicati. Il filtro combina ``sessionId`` e ``sessionItemId``
        in AND: senza ``sessionId`` cancellerebbe anche i risultati di altre
        sessioni che riusano lo stesso ``SessionItem``.
    """
    collection = get_collection(RESULTS)
    try:
        delete_result = await collection.delete_many(
            {"sessionId": session_id, "sessionItemId": session_item_id}
        )
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare i risultati precedenti.") from exc
    logger.info(
        "Risultati eliminati per la sessione %s / item %s: %d",
        session_id,
        session_item_id,
        delete_result.deleted_count,
    )
    return delete_result.deleted_count
