"""Logica di business dell'entità Scenario: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from app.core.errors import DatabaseError, NotFoundError
from app.db.collections import SCENARIOS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.scenario import Scenario, ScenarioCreate, ScenarioUpdate

logger = logging.getLogger(__name__)


async def list_scenarios() -> list[Scenario]:
    """Elenca tutti gli scenari registrati.

    Riceve:
        Nulla.

    Restituisce:
        La lista degli ``Scenario``, ordinata per nome crescente.

    Fa:
        Legge l'intera collezione ``scenarios`` e converte ogni documento nel
        modello Pydantic. Solleva ``DatabaseError`` se Mongo non risponde.
    """
    collection = get_collection(SCENARIOS)
    try:
        documents = await collection.find().sort("name", 1).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere gli scenari dal database.") from exc
    return [Scenario.model_validate(document) for document in documents]


async def get_scenario(scenario_id: str) -> Scenario:
    """Recupera un singolo scenario per identificativo.

    Riceve:
        scenario_id: identificativo dello scenario come stringa esadecimale a 24 caratteri.

    Restituisce:
        Lo ``Scenario`` corrispondente.

    Fa:
        Converte l'id in ``ObjectId`` (``ValidationError`` se malformato),
        interroga la collezione ``scenarios`` e solleva ``NotFoundError`` se il
        documento non esiste.
    """
    collection = get_collection(SCENARIOS)
    object_id = to_object_id(scenario_id, "Id dello scenario")
    try:
        document = await collection.find_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere lo scenario dal database.") from exc
    if document is None:
        raise NotFoundError(f"Scenario '{scenario_id}' non trovato.")
    return Scenario.model_validate(document)


async def create_scenario(payload: ScenarioCreate) -> Scenario:
    """Crea un nuovo scenario.

    Riceve:
        payload: i dati dello scenario validati da ``ScenarioCreate``.

    Restituisce:
        Lo ``Scenario`` creato, completo dell'``id`` generato da MongoDB.

    Fa:
        Inserisce il documento nella collezione ``scenarios`` e ricostruisce il
        modello con l'``_id`` assegnato, senza rileggerlo dal database.
    """
    collection = get_collection(SCENARIOS)
    document = payload.model_dump(by_alias=True, mode="json")
    try:
        insert_result = await collection.insert_one(document)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile creare lo scenario.") from exc
    logger.info("Scenario creato: %s (%s)", payload.name, insert_result.inserted_id)
    return Scenario.model_validate({**document, "_id": insert_result.inserted_id})


async def update_scenario(scenario_id: str, payload: ScenarioUpdate) -> Scenario:
    """Aggiorna i campi valorizzati di uno scenario esistente.

    Riceve:
        scenario_id: identificativo dello scenario da aggiornare.
        payload: i campi da modificare; quelli omessi restano invariati.

    Restituisce:
        Lo ``Scenario`` nello stato successivo all'aggiornamento.

    Fa:
        Esegue un ``find_one_and_update`` con ``$set`` sui soli campi presenti
        nella richiesta. Solleva ``NotFoundError`` se lo scenario non esiste e
        ``ValidationError`` se il payload non contiene alcun campo.
    """
    from app.core.errors import ValidationError

    collection = get_collection(SCENARIOS)
    object_id = to_object_id(scenario_id, "Id dello scenario")
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
        raise DatabaseError("Impossibile aggiornare lo scenario.") from exc
    if document is None:
        raise NotFoundError(f"Scenario '{scenario_id}' non trovato.")
    logger.info("Scenario aggiornato: %s (campi: %s)", scenario_id, ", ".join(changes))
    return Scenario.model_validate(document)


async def delete_scenario(scenario_id: str) -> None:
    """Elimina uno scenario.

    Riceve:
        scenario_id: identificativo dello scenario da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Cancella il documento dalla collezione ``scenarios`` e solleva
        ``NotFoundError`` se non esisteva, così che il client distingua una
        cancellazione effettiva da un id sbagliato.
    """
    collection = get_collection(SCENARIOS)
    object_id = to_object_id(scenario_id, "Id dello scenario")
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare lo scenario.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"Scenario '{scenario_id}' non trovato.")
    logger.info("Scenario eliminato: %s", scenario_id)
