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
) -> list[Result]:
    """Elenca i risultati, opzionalmente filtrati.

    Riceve:
        scenario_path: se valorizzato, restituisce solo i risultati di quello
            scenario (confronto esatto sul path richiesto).
        session_item_ids: se valorizzata, restituisce solo i risultati prodotti
            da quei session item; usata dal frontend per il filtro per sessione.

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


async def delete_results_by_session_items(session_item_ids: list[str]) -> int:
    """Elimina tutti i risultati prodotti dai session item indicati.

    Riceve:
        session_item_ids: gli identificativi dei session item.

    Restituisce:
        Il numero di risultati eliminati.

    Fa:
        Usata per ripulire i risultati di una esecuzione precedente quando una
        sessione viene riavviata, così che i dati di due run non si mescolino.
        Con lista vuota non tocca il database e restituisce 0.
    """
    if not session_item_ids:
        return 0

    collection = get_collection(RESULTS)
    try:
        delete_result = await collection.delete_many({"sessionItemId": {"$in": session_item_ids}})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare i risultati precedenti.") from exc
    logger.info("Risultati eliminati: %d", delete_result.deleted_count)
    return delete_result.deleted_count
