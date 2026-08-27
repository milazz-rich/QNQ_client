"""Logica di business dell'entità Session: nessuna dipendenza da FastAPI."""

import logging

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

from app.core import session_logging
from app.core.errors import ConflictError, DatabaseError, NotFoundError, ValidationError
from app.db.collections import SESSIONS
from app.db.mongo import get_collection
from app.models.common import to_object_id
from app.models.session import RunStatus, Session, SessionCreate, SessionUpdate
from app.services import results_service

logger = logging.getLogger(__name__)

# Messaggio del 409 prodotto quando la scrittura viola l'indice unico parziale
# ``ix_single_running_session``. Vive qui, e non duplicato nei due punti che lo
# sollevano (``set_status`` e ``update_session``), perché è la **stessa**
# condizione vista da due strade diverse: due testi che divergessero farebbero
# sembrare al client due errori distinti dove ce n'è uno solo. È deliberatamente
# più generico del messaggio del router (§5.1): a questo livello la race è già
# avvenuta e non si sa più quale sessione l'abbia vinta, quindi non si può
# nominare quella bloccante.
_ALREADY_RUNNING_MESSAGE = (
    "Un'altra sessione è già in esecuzione: le misure di questo "
    "progetto sono sequenziali per metodologia (AGENTS.md §5.1), non "
    "è possibile eseguirne due contemporaneamente."
)


async def get_running_session() -> Session | None:
    """Cerca una Session con ``status="running"``, se esiste.

    Riceve:
        Nulla.

    Restituisce:
        La ``Session`` in esecuzione, oppure ``None`` se nessuna lo è.

    Fa:
        Usata per un controllo applicativo **prima** di tentare di avviare
        un'altra sessione, per poter rispondere con un messaggio che nomina
        la sessione bloccante invece di un generico conflitto. Da sola non
        basta a escludere una race fra due avvii concorrenti: la garanzia
        atomica finale è l'indice unico parziale su ``sessions.status``
        (``ix_single_running_session``, vedi ``db.mongo.ensure_indexes``),
        che ``set_status``/``update_session`` traducono in ``ConflictError``
        se la scrittura arriva comunque a collidere.
    """
    collection = get_collection(SESSIONS)
    try:
        document = await collection.find_one({"status": RunStatus.RUNNING.value})
    except PyMongoError as exc:
        raise DatabaseError("Impossibile verificare le sessioni in esecuzione.") from exc
    return Session.model_validate(document) if document is not None else None


async def recover_interrupted_sessions() -> int:
    """Riporta a ``failed`` le Session rimaste ``running`` da un crash precedente.

    Riceve:
        Nulla.

    Restituisce:
        Il numero di sessioni recuperate.

    Fa:
        Invocata **all'avvio** dal lifespan di FastAPI (`app/main.py`), nello
        stesso punto in cui `firefox_client.cleanup_stale_run_profiles` ripulisce
        le directory orfane (§5.7): un processo che sta appena avviandosi non
        sta eseguendo nessuna sessione, quindi qualunque documento con
        ``status="running"`` a questo punto è per definizione un residuo di
        un'interruzione anomala (crash, `SIGKILL`, riavvio) — nessun
        `session_runner.start_session` può essere realmente in corso nel
        processo che sta nascendo ora.

        Senza questo passo la sessione resterebbe bloccata in ``running`` per
        sempre: nessun codice esistente la riporta indietro spontaneamente, e
        l'indice unico parziale impedirebbe anche l'avvio di qualunque nuova
        sessione finché quella fantasma occupa lo status. Scrive anche
        ``note``, così chi la trova già ``failed`` sa che **nessun item ha
        realmente fallito una misura** — a differenza del significato abituale
        di ``failed`` (§5.4) — e non la scambia per un dato di misura valido.

        Usa ``update_many`` invece di passare da ``set_status`` (che
        aggiornerebbe una sessione alla volta): non c'è ambiguità da
        risolvere sessione per sessione, e più documenti ``running``
        contemporaneamente sono già di per sé la prova che l'indice unico
        (introdotto insieme a questo meccanismo) non esisteva ancora quando
        sono stati scritti.
    """
    collection = get_collection(SESSIONS)
    note = (
        "Sessione riportata a 'failed' dal recupero da crash all'avvio: "
        "era ancora 'running' quando il processo è ripartito, quindi "
        "l'esecuzione precedente si è interrotta in modo anomalo (crash o "
        "riavvio) prima di poter completare o fallire regolarmente. "
        "Nessun item ha necessariamente fallito una misura."
    )
    try:
        result = await collection.update_many(
            {"status": RunStatus.RUNNING.value},
            {"$set": {"status": RunStatus.FAILED.value, "note": note}},
        )
    except PyMongoError as exc:
        raise DatabaseError("Impossibile recuperare le sessioni interrotte.") from exc
    if result.modified_count:
        logger.warning(
            "Recuperate %d sessioni rimaste 'running' da un'interruzione anomala.",
            result.modified_count,
        )
    return result.modified_count


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

        ``SessionUpdate`` include ``status``, quindi è un secondo modo (oltre
        a ``POST /sessions/{id}/start``) di portare una sessione a
        ``RUNNING``: soggetto alla stessa garanzia atomica, non solo al
        controllo del router di `/start`. Una violazione dell'indice unico
        parziale ``ix_single_running_session`` produce ``ConflictError``
        (409) invece di ``DatabaseError``, con lo stesso significato di
        ``set_status``.
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
    except DuplicateKeyError as exc:
        raise ConflictError(_ALREADY_RUNNING_MESSAGE) from exc
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

    # Il log su file segue la stessa cascata dei Result, e per la stessa
    # ragione: `GET /api/sessions/{id}/log` richiede che la sessione esista,
    # quindi un log che le sopravvivesse non sarebbe più leggibile da nessuno —
    # solo spazio occupato. Viene cancellato **dopo** la sessione, perché è
    # l'unico passo che non ha bisogno di essere ripetibile.
    deleted_log = session_logging.delete_session_log(session_id)
    logger.info(
        "Session eliminata: %s (con %d risultati a cascata, log su file: %s)",
        session_id,
        deleted_results,
        "rimosso" if deleted_log else "assente",
    )


async def get_session_log(session_id: str) -> str:
    """Restituisce il log su file prodotto dall'esecuzione di una sessione.

    Riceve:
        session_id: identificativo della sessione.

    Restituisce:
        Il contenuto testuale di ``logs/sessions/{sessionId}.log``.

    Fa:
        Verifica **prima** che la sessione esista, così un id inesistente
        produce il ``404`` della sessione e non quello, fuorviante, del log
        mancante. Solleva poi ``NotFoundError`` se il file non c'è: una
        sessione creata ma mai avviata non ha ancora un log, e questo è un
        esito normale da comunicare in modo pulito, non un errore interno.

        Il file resta leggibile anche a sessione conclusa, ``completed`` o
        ``failed`` che sia: ``session_log_context`` chiude lo stream di
        scrittura ma non rimuove nulla (§5.10). Viene invece riscritto da capo
        se la stessa sessione viene rilanciata, coerentemente con la
        cancellazione dei ``Result`` della run precedente.
    """
    await get_session(session_id)

    try:
        content = session_logging.read_session_log(session_id)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    except OSError as exc:
        raise DatabaseError(f"Impossibile leggere il log della sessione: {exc}") from exc

    if content is None:
        raise NotFoundError(
            f"Nessun log disponibile per la sessione '{session_id}': "
            "non è mai stata avviata, oppure l'esecuzione non ha ancora scritto nulla."
        )
    return content


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

        Solleva ``ConflictError`` (409) invece di ``DatabaseError`` se la
        scrittura viola l'indice unico parziale ``ix_single_running_session``
        (`db.mongo.ensure_indexes`): può succedere solo quando ``status`` è
        ``RUNNING`` e un'altra sessione lo è già — riscrivere ``RUNNING`` sulla
        **stessa** sessione che ce l'ha già non viola l'indice (stesso
        documento, stesso valore), quindi la doppia chiamata già presente in
        `session_runner.start_session` (router + runner) resta innocua. È il
        backstop atomico al controllo applicativo di
        ``get_running_session``: chiude la finestra di race fra due avvii
        concorrenti su sessioni diverse che quel controllo, da solo, non può
        escludere.
    """
    collection = get_collection(SESSIONS)
    object_id = to_object_id(session_id, "Id della sessione")
    try:
        await collection.update_one({"_id": object_id}, {"$set": {"status": status.value}})
    except DuplicateKeyError as exc:
        raise ConflictError(_ALREADY_RUNNING_MESSAGE) from exc
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
