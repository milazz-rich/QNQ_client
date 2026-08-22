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
from app.models.result import (
    AggregateDimension,
    AggregateMetric,
    Result,
    ResultAggregateGroup,
    ResultCreate,
    ResultStatus,
)

logger = logging.getLogger(__name__)

# Paginazione di ``GET /api/results``. Il tetto massimo esiste perché una
# sessione lunga produce facilmente migliaia di risultati: senza limite, una
# singola richiesta potrebbe caricarli tutti in memoria e saturare la risposta.
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


def build_filter_query(
    scenario_path: str | None = None,
    session_item_ids: list[str] | None = None,
    session_id: str | None = None,
    client_id: str | None = None,
    scenario_id: str | None = None,
    target_id: str | None = None,
) -> dict[str, object]:
    """Compone la query Mongo a partire dai filtri dell'API.

    Riceve:
        scenario_path: filtro sul path richiesto (confronto esatto).
        session_item_ids: filtro sui session item che hanno prodotto la misura.
        session_id: filtro sulla singola esecuzione di sessione.
        client_id: filtro sul motore di misura.
        scenario_id: filtro sullo scenario.
        target_id: filtro sul server sotto test.

    Restituisce:
        Il dizionario di query, vuoto se nessun filtro è valorizzato.

    Fa:
        Condivisa da ``list_results`` e ``aggregate_results`` perché i due
        endpoint devono accettare **esattamente** gli stessi filtri: tenerne
        due copie li farebbe divergere alla prima aggiunta. I filtri si
        combinano in AND. Gli id sono confrontati come stringhe, coerentemente
        con come il runner li salva. Una lista di id vuota vale "nessun
        filtro", non "nessun risultato", perché deriva da una query string
        vuota.
    """
    query: dict[str, object] = {}
    if scenario_path:
        query["scenarioPath"] = scenario_path
    if session_item_ids:
        query["sessionItemId"] = {"$in": session_item_ids}
    if session_id:
        query["sessionId"] = session_id
    if client_id:
        query["clientId"] = client_id
    if scenario_id:
        query["scenarioId"] = scenario_id
    if target_id:
        query["targetId"] = target_id
    return query


async def list_results(
    scenario_path: str | None = None,
    session_item_ids: list[str] | None = None,
    session_id: str | None = None,
    client_id: str | None = None,
    scenario_id: str | None = None,
    target_id: str | None = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> tuple[list[Result], int]:
    """Elenca una pagina di risultati, opzionalmente filtrati.

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
        client_id: se valorizzato, restituisce solo i risultati prodotti da quel
            motore di misura (confronto curl vs Chrome).
        scenario_id: se valorizzato, filtra per scenario (riferimento diretto,
            preferibile a ``scenario_path`` che è uno snapshot testuale).
        target_id: se valorizzato, filtra per server sotto test.
        page: pagina richiesta, 1-based.
        page_size: numero massimo di risultati per pagina.

    Restituisce:
        La coppia ``(risultati della pagina, totale che soddisfa i filtri)``.
        Il totale è **indipendente dalla paginazione**: serve al chiamante per
        sapere quante pagine esistono senza doverle scorrere tutte.

    Fa:
        Compone la query con ``build_filter_query``, poi esegue due operazioni
        sulla stessa query: un ``count_documents`` per il totale e una ``find``
        con ``skip``/``limit`` per la pagina. L'ordinamento per ``time``
        crescente (con ``_id`` come discriminante) è ciò che rende la
        paginazione stabile: senza un ordine totale, pagine successive
        potrebbero ripetere o saltare documenti.
    """
    query = build_filter_query(
        scenario_path=scenario_path,
        session_item_ids=session_item_ids,
        session_id=session_id,
        client_id=client_id,
        scenario_id=scenario_id,
        target_id=target_id,
    )

    collection = get_collection(RESULTS)
    skip = (page - 1) * page_size
    try:
        total = await collection.count_documents(query)
        documents = (
            await collection.find(query)
            .sort([("time", 1), ("_id", 1)])
            .skip(skip)
            .limit(page_size)
            .to_list(length=page_size)
        )
    except PyMongoError as exc:
        raise DatabaseError("Impossibile leggere i risultati dal database.") from exc
    return [Result.model_validate(document) for document in documents], total


async def aggregate_results(
    group_by: AggregateDimension,
    metric: AggregateMetric,
    scenario_path: str | None = None,
    session_item_ids: list[str] | None = None,
    session_id: str | None = None,
    client_id: str | None = None,
    scenario_id: str | None = None,
    target_id: str | None = None,
) -> tuple[list[ResultAggregateGroup], int]:
    """Calcola le medie di una metrica, raggruppate per dimensione e protocollo.

    Riceve:
        group_by: dimensione di raggruppamento (target, environment, client,
            scenario).
        metric: metrica di cui calcolare la media (total, ttfb, kb).
        scenario_path, session_item_ids, session_id, client_id, scenario_id,
            target_id: gli stessi filtri accettati da ``list_results``.

    Restituisce:
        La coppia ``(gruppi, misure considerate)``. I gruppi sono ordinati per
        etichetta e poi per protocollo, così l'output è stabile fra chiamate
        successive e direttamente utilizzabile per un grafico.

    Fa:
        Esegue **tutto il lavoro sul database** con una pipeline di
        aggregazione: al chiamante tornano solo i valori finali, mai i
        documenti grezzi — è la ragione d'essere di questo endpoint, che deve
        poter riassumere decine di migliaia di misure senza trasferirle.

        Considera **solo** i risultati con ``status="completed"``: una misura
        fallita ha ``total``/``ttfb``/``kb`` azzerati per costruzione (§5.3 di
        AGENTS.md), quindi includerla abbasserebbe le medie in proporzione al
        tasso di fallimento, facendo sembrare più veloce un target che invece
        sta solo fallendo di più.

        Il protocollo è **sempre** parte della chiave di raggruppamento: il
        confronto HTTP/2 vs HTTP/3 è l'oggetto stesso dell'applicazione, e una
        media che li mescolasse non avrebbe significato.

        Per ``target``, ``scenario`` ed ``environment`` tutto ciò che serve è
        già nel ``Result`` — rispettivamente ``target``/``targetId``,
        ``scenarioPath``/``scenarioId`` ed ``environment``, quest'ultimo un
        campo di primo livello dopo il refactoring (§3.3): nessun ``$lookup``.
        Solo ``client`` richiede una join, perché il nome del motore non è
        denormalizzato nel ``Result``; gli id sono salvati come stringhe,
        quindi la join passa da ``$toObjectId``.
    """
    match_stage = build_filter_query(
        scenario_path=scenario_path,
        session_item_ids=session_item_ids,
        session_id=session_id,
        client_id=client_id,
        scenario_id=scenario_id,
        target_id=target_id,
    )
    match_stage["status"] = ResultStatus.COMPLETED.value

    pipeline: list[dict[str, object]] = [{"$match": match_stage}]

    # Chiave di raggruppamento ed etichetta, per dimensione.
    if group_by is AggregateDimension.TARGET:
        key_expr, label_expr = "$targetId", {"$first": "$target"}
    elif group_by is AggregateDimension.SCENARIO:
        key_expr, label_expr = "$scenarioId", {"$first": "$scenarioPath"}
    elif group_by is AggregateDimension.CLIENT:
        pipeline += _lookup_stages("clients", "clientId", "name")
        key_expr, label_expr = "$clientId", {"$first": "$_joined"}
    else:  # AggregateDimension.ENVIRONMENT — campo diretto del Result
        key_expr, label_expr = "$environment", {"$first": "$environment"}

    pipeline += [
        {
            "$group": {
                "_id": {"key": key_expr, "proto": "$proto"},
                "label": label_expr,
                "avg": {"$avg": f"${metric.value}"},
                "count": {"$sum": 1},
            }
        },
        {"$sort": {"label": 1, "_id.proto": 1}},
    ]

    collection = get_collection(RESULTS)
    try:
        rows = await collection.aggregate(pipeline).to_list(length=None)
    except PyMongoError as exc:
        raise DatabaseError("Impossibile aggregare i risultati.") from exc

    groups = [
        ResultAggregateGroup(
            key=str(row["_id"]["key"]),
            # Un riferimento non più risolvibile (client cancellato) lascia
            # l'etichetta a null: meglio un segnaposto esplicito che un vuoto.
            label=str(row.get("label") or "(senza etichetta)"),
            proto=row["_id"]["proto"],
            avg=float(row["avg"] or 0.0),
            count=int(row["count"]),
        )
        for row in rows
    ]
    considered = sum(group.count for group in groups)
    logger.info(
        "Aggregazione per %s su %s: %d gruppi, %d misure considerate",
        group_by.value,
        metric.value,
        len(groups),
        considered,
    )
    return groups, considered


def _lookup_stages(collection_name: str, local_field: str, wanted: str) -> list[dict[str, object]]:
    """Costruisce gli stage di join verso una collezione di configurazione.

    Riceve:
        collection_name: collezione da cui prendere il dato (``clients``,
            ``targets``).
        local_field: campo del ``Result`` che contiene l'id come stringa.
        wanted: campo da estrarre dal documento joinato (es. ``name``).

    Restituisce:
        Gli stage ``$lookup`` e ``$set`` che valorizzano il campo temporaneo
        ``_joined`` su ogni documento.

    Fa:
        Il ``$lookup`` per ``localField``/``foreignField`` non funziona qui: nel
        ``Result`` gli id sono **stringhe**, in ``_id`` sono ``ObjectId``, e
        Mongo non converte implicitamente. Serve quindi la forma con ``let`` +
        ``pipeline`` e una conversione esplicita con ``$toObjectId``. Un
        riferimento non più risolvibile (entità cancellata) produce un array
        vuoto: ``$first`` restituisce ``null`` e il chiamante mostra un
        segnaposto, invece di far sparire il gruppo dall'aggregazione.
    """
    return [
        {
            "$lookup": {
                "from": collection_name,
                "let": {"fk": {"$toObjectId": f"${local_field}"}},
                "pipeline": [
                    {"$match": {"$expr": {"$eq": ["$_id", "$$fk"]}}},
                    {"$project": {wanted: 1}},
                ],
                "as": "_lookup",
            }
        },
        {"$set": {"_joined": {"$first": f"$_lookup.{wanted}"}}},
    ]


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
