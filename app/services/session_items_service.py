"""Logica di business dell'entità SessionItem: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from bson import ObjectId

from app.core.errors import ConflictError, DatabaseError, NotFoundError, ValidationError
from app.db.collections import SESSION_ITEMS, SESSIONS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.session_item import (
    OrphanedSessionItem,
    SessionItem,
    SessionItemBatchCreate,
    SessionItemCreate,
    SessionItemUpdate,
)

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


async def list_orphaned_session_items() -> list[OrphanedSessionItem]:
    """Elenca i session item non referenziati da nessuna Session esistente.

    Riceve:
        Nulla.

    Restituisce:
        La lista degli ``OrphanedSessionItem`` (§5.5), ciascuno con
        ``createdAt`` derivato dal proprio ``ObjectId`` — nessun campo di
        creazione è persistito su ``SessionItem`` (vedi il modello).

    Fa:
        Calcola l'insieme dei ``sessionItemId`` ancora referenziati con
        ``sessions.distinct("items.sessionItemId")`` — una singola query che
        raccoglie i riferimenti di **tutte** le sessioni in un colpo solo,
        invece di una per sessione — poi filtra ``session_items`` per
        esclusione (``_id $nin`` quell'insieme). Un ``SessionItem`` mai
        referenziato da nessuna sessione (creato ma mai raggruppato in una
        `Session`, non solo uno diventato orfano dopo una cancellazione) è
        comunque orfano per questa definizione: non è mai stato "in uso", il
        che è coerente con quanto un rilancio (§3.3) intende per riutilizzo —
        serve un item **raggiungibile da una sessione**, a prescindere dal
        perché non lo sia (mai assegnato, o assegnato e poi liberato).
    """
    items_collection = get_collection(SESSION_ITEMS)
    sessions_collection = get_collection(SESSIONS)
    try:
        referenced_ids = await sessions_collection.distinct("items.sessionItemId")
        referenced_object_ids = [
            ObjectId(raw_id) for raw_id in referenced_ids if ObjectId.is_valid(raw_id)
        ]
        documents = await items_collection.find(
            {"_id": {"$nin": referenced_object_ids}}
        ).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile calcolare i session item orfani.") from exc

    return [
        OrphanedSessionItem.model_validate(
            {**document, "createdAt": document["_id"].generation_time}
        )
        for document in documents
    ]


async def delete_orphaned_session_items() -> list[str]:
    """Cancella tutti i session item correntemente orfani.

    Riceve:
        Nulla.

    Restituisce:
        Gli ``id`` effettivamente cancellati (stringa esadecimale).

    Fa:
        Ricalcola l'insieme degli orfani **al momento della chiamata**
        (chiama ``list_orphaned_session_items`` internamente, non accetta una
        lista dall'esterno): un risultato di una precedente ``GET`` potrebbe
        essere stale se nel frattempo una nuova sessione ha riassegnato uno
        di quegli item (§3.3, rilancio/riproposizione).

        Cancella **un item alla volta tramite ``delete_session_item``**,
        non con un ``delete_many`` diretto: riusa così lo stesso controllo di
        integrità referenziale invece di duplicarlo, e resta corretta anche
        nella finestra — strettissima ma non nulla, Mongo standalone non ha
        transazioni multi-documento (§5.5) — fra il calcolo della lista e la
        cancellazione: se un item viene riassegnato a una sessione proprio in
        quell'istante, ``delete_session_item`` solleva ``ConflictError`` per
        quel singolo id, che viene registrato e saltato invece di far fallire
        l'intera operazione di manutenzione per un caso che, per costruzione,
        non dovrebbe mai verificarsi.
    """
    orphaned = await list_orphaned_session_items()
    deleted_ids: list[str] = []
    for item in orphaned:
        try:
            await delete_session_item(item.id)
        except ConflictError:
            logger.warning(
                "SessionItem '%s' riassegnato a una sessione fra il calcolo degli "
                "orfani e la cancellazione: saltato.",
                item.id,
            )
            continue
        except NotFoundError:
            # Già cancellato (es. da una chiamata concorrente a questo stesso
            # endpoint): non è un errore da segnalare, il risultato voluto —
            # l'item non è più nel database — è comunque raggiunto.
            logger.warning(
                "SessionItem '%s' già cancellato prima di questa chiamata: saltato.",
                item.id,
            )
            continue
        deleted_ids.append(item.id)

    logger.info("SessionItem orfani cancellati: %d", len(deleted_ids))
    return deleted_ids


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


async def create_session_items_batch(spec: SessionItemBatchCreate) -> list[str]:
    """Genera e crea il prodotto cartesiano Scenario × Protocollo × Ambiente.

    Riceve:
        spec: la specifica del wizard — quali scenari, quali protocolli, quali
            ambienti.

    Restituisce:
        La lista degli ``id`` (stringa) assegnati da MongoDB, nell'ordine di
        generazione: scenario esterno, poi protocollo, poi ambiente.

    Fa:
        Costruisce il prodotto cartesiano **nel backend**: il wizard invia una
        specifica compatta invece di N×M×P oggetti già espansi.

        Target, client, ``reps`` e ``timeout`` **non** compaiono nel prodotto:
        sono scelte della ``Session`` che raccoglierà questi item, uguali per
        tutti (§3.3 di AGENTS.md). Ciò che varia da item a item è solo la terna
        *(scenario, protocollo, ambiente)* — le tre dimensioni del confronto.

        I tre insiemi sono **deduplicati** preservando l'ordine di
        inserimento: lo stesso scenario o protocollo indicato due volte
        genererebbe altrimenti item identici, senza che l'utente lo abbia
        chiesto. L'ordine di generazione è deterministico, così il chiamante
        può correlare gli ``id`` restituiti alle combinazioni richieste senza
        rileggerli.

        Non verifica che gli scenari esistano: un id inesistente produce un
        item che fallirà con ``NOT_FOUND`` alla prima esecuzione, tracciato
        come tale (§5.4). Validarli qui richiederebbe N query aggiuntive per un
        errore che il runner intercetta comunque.
    """
    scenario_ids = list(dict.fromkeys(spec.scenario_ids))
    protocols = list(dict.fromkeys(spec.protocols))
    environments = list(dict.fromkeys(spec.environments))

    payloads = [
        SessionItemCreate(
            scenarioId=scenario_id,
            protocol=protocol,
            environment=environment,
        )
        for scenario_id in scenario_ids
        for protocol in protocols
        for environment in environments
    ]
    logger.info(
        "Batch: %d scenari × %d protocolli × %d ambienti = %d session item",
        len(scenario_ids),
        len(protocols),
        len(environments),
        len(payloads),
    )

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
    """Elimina un session item, se non più referenziato da alcuna sessione.

    Riceve:
        session_item_id: identificativo del session item da eliminare.

    Restituisce:
        ``None``.

    Fa:
        Verifica prima che nessuna ``Session`` referenzi ancora questo
        ``SessionItem`` nel proprio array ``items`` (campo
        ``items.sessionItemId``, confrontato come stringa): se ne trova almeno
        una solleva ``ConflictError`` e non procede alla cancellazione, per non
        lasciare una sessione con un riferimento pendente. Solo se nessuna
        sessione lo referenzia cancella il documento dalla collezione
        ``session_items``, sollevando ``NotFoundError`` se non esisteva, così
        che il client distingua una cancellazione effettiva da un id sbagliato.
    """
    collection = get_collection(SESSION_ITEMS)
    sessions_collection = get_collection(SESSIONS)
    object_id = to_object_id(session_item_id, "Id del session item")
    try:
        referencing_session = await sessions_collection.find_one(
            {"items": {"$elemMatch": {"sessionItemId": session_item_id}}}
        )
    except PyMongoError as exc:
        raise DatabaseError(
            "Impossibile verificare i riferimenti al session item."
        ) from exc
    if referencing_session is not None:
        raise ConflictError(
            f"SessionItem '{session_item_id}' è ancora in uso da una o più sessioni."
        )
    try:
        delete_result = await collection.delete_one({"_id": object_id})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile eliminare il session item.") from exc
    if delete_result.deleted_count == 0:
        raise NotFoundError(f"SessionItem '{session_item_id}' non trovato.")
    logger.info("SessionItem eliminato: %s", session_item_id)
