"""Router HTTP dell'entità Result: sola lettura su ``/api/results``.

I risultati non sono creati dal client ma prodotti dal session runner, quindi
non esistono ``POST``/``PUT``/``DELETE``.

**Ordine delle rotte.** ``GET /results/aggregate`` è dichiarato *prima* di
``GET /results/{result_id}``: entrambe sono ``GET`` sullo stesso livello di
path, e FastAPI risolve nell'ordine di dichiarazione. Invertendole,
``/aggregate`` verrebbe interpretato come un ``result_id`` e produrrebbe un
``422`` (non rispetta il pattern a 24 caratteri esadecimali) invece di
raggiungere l'aggregazione.
"""

from fastapi import APIRouter, Path, Query

from app.models.common import ErrorResponse
from app.models.result import (
    AggregateDimension,
    AggregateMetric,
    Result,
    ResultAggregate,
    ResultPage,
)
from app.services import results_service

router = APIRouter(
    prefix="/results",
    tags=["results"],
    responses={
        422: {"model": ErrorResponse, "description": "Parametro non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_ResultId = Path(description="Identificativo del risultato (24 caratteri esadecimali)")


@router.get("", response_model=ResultPage, summary="Elenca i risultati (paginato)")
async def list_results(
    scenario_path: str | None = Query(
        default=None,
        alias="scenarioPath",
        description="Filtra per path dello scenario (confronto esatto)",
    ),
    session_item_ids: str | None = Query(
        default=None,
        alias="sessionItemIds",
        description="Filtra per session item: lista di id separati da virgola",
    ),
    session_id: str | None = Query(
        default=None,
        alias="sessionId",
        description=(
            "Filtra per una o più esecuzioni di sessione: lista di id separati "
            "da virgola, come sessionItemIds (un solo id è il caso degenere "
            "di una lista con un elemento). Preferito a sessionItemIds per "
            "ottenere i risultati di una o più sessioni specifiche: diretto e "
            "senza ambiguità quando un SessionItem è condiviso fra più sessioni. "
            "Più id si combinano in OR (unione), non in AND"
        ),
    ),
    client_id: str | None = Query(
        default=None,
        alias="clientId",
        description="Filtra per motore di misura che ha prodotto il risultato",
    ),
    scenario_id: str | None = Query(
        default=None,
        alias="scenarioId",
        description=(
            "Filtra per scenario. Preferito a scenarioPath, che è uno snapshot "
            "testuale e può essere condiviso da scenari diversi"
        ),
    ),
    target_id: str | None = Query(
        default=None,
        alias="targetId",
        description="Filtra per server sotto test",
    ),
    page: int = Query(default=1, ge=1, description="Pagina richiesta, 1-based"),
    page_size: int = Query(
        default=results_service.DEFAULT_PAGE_SIZE,
        ge=1,
        le=results_service.MAX_PAGE_SIZE,
        alias="pageSize",
        description=(
            f"Risultati per pagina (max {results_service.MAX_PAGE_SIZE})"
        ),
    ),
) -> ResultPage:
    """Restituisce una pagina di risultati, opzionalmente filtrati.

    Riceve:
        scenario_path: valore di ``?scenarioPath=``, filtra per singolo scenario.
        session_item_ids: valore di ``?sessionItemIds=``, lista di id separati
            da virgola.
        session_id: valore di ``?sessionId=``, una o più sessioni, lista di id
            separati da virgola (un solo id senza virgole è il caso degenere).
            Più id si combinano in **OR**: l'unione dei risultati di ciascuna
            sessione elencata, non l'intersezione.
        client_id: valore di ``?clientId=``, filtra per motore di misura.
        scenario_id: valore di ``?scenarioId=``, filtra per scenario.
        target_id: valore di ``?targetId=``, filtra per server sotto test.
        page: valore di ``?page=``, 1-based.
        page_size: valore di ``?pageSize=``, limitato a
            ``results_service.MAX_PAGE_SIZE``.

    Restituisce:
        ``200`` con un ``ResultPage``: ``items`` (la pagina, ordinata per
        istante di completamento crescente), ``total`` (quanti risultati
        soddisfano i filtri **in tutto**, non solo in questa pagina), più
        ``page`` e ``pageSize`` applicati. I filtri diversi si combinano in
        AND; gli id dentro ``sessionId``/``sessionItemIds`` si combinano in OR
        fra loro (§5.8 di AGENTS.md). ``page`` oltre l'ultima pagina
        disponibile produce ``items`` vuoto e ``total`` invariato, non un
        errore.

    Fa:
        Spacchetta le liste comma-joined scartando i segmenti vuoti — così
        ``?sessionItemIds=a,,b`` e un parametro vuoto non generano filtri
        spuri — e delega a ``results_service.list_results``, che restituisce
        pagina e totale in un'unica chiamata. ``page``/``pageSize`` fuori range
        sono respinti da FastAPI con ``422`` prima di arrivare al servizio.
    """
    items, total = await results_service.list_results(
        scenario_path=scenario_path,
        session_item_ids=_split_ids(session_item_ids),
        session_ids=_split_ids(session_id),
        client_id=client_id,
        scenario_id=scenario_id,
        target_id=target_id,
        page=page,
        page_size=page_size,
    )
    return ResultPage(items=items, total=total, page=page, pageSize=page_size)


@router.get(
    "/aggregate",
    response_model=ResultAggregate,
    summary="Medie aggregate per dimensione e protocollo",
)
async def aggregate_results(
    group_by: AggregateDimension = Query(
        alias="groupBy",
        description=(
            "Dimensione di raggruppamento. 'environment' confronta gli "
            "ambienti di deploy (docker vs kvm) a parità di tutto il resto"
        ),
    ),
    metric: AggregateMetric = Query(
        default=AggregateMetric.TOTAL,
        description="Metrica di cui calcolare la media",
    ),
    scenario_path: str | None = Query(default=None, alias="scenarioPath"),
    session_item_ids: str | None = Query(default=None, alias="sessionItemIds"),
    session_id: str | None = Query(
        default=None,
        alias="sessionId",
        description=(
            "Una o più sessioni, lista di id separati da virgola — stessa "
            "semantica OR di GET /api/results"
        ),
    ),
    client_id: str | None = Query(default=None, alias="clientId"),
    scenario_id: str | None = Query(default=None, alias="scenarioId"),
    target_id: str | None = Query(default=None, alias="targetId"),
) -> ResultAggregate:
    """Restituisce le medie di una metrica, raggruppate per dimensione e protocollo.

    Riceve:
        group_by: valore di ``?groupBy=`` — ``target``, ``environment``,
            ``client`` o ``scenario``.
        metric: valore di ``?metric=`` — ``total``, ``ttfb`` o ``kb``.
        scenario_path, session_item_ids, session_id, client_id, scenario_id,
            target_id: gli **stessi** filtri di ``GET /api/results``, con la
            stessa semantica (filtri diversi in AND, id multipli dentro
            ``sessionId``/``sessionItemIds`` in OR).

    Restituisce:
        ``200`` con un ``ResultAggregate``: la dimensione e la metrica
        applicate, i ``groups`` (uno per combinazione dimensione × protocollo,
        con media e numerosità) e ``considered``, il totale delle misure
        entrate nel calcolo. **Nessun risultato grezzo**: l'aggregazione è
        interamente eseguita dal database.

    Fa:
        Delega a ``results_service.aggregate_results``, che esegue una pipeline
        MongoDB. Sono considerate solo le misure con ``status="completed"``:
        una misura fallita ha metriche azzerate per costruzione e
        abbasserebbe le medie in proporzione al tasso di fallimento. Un
        ``groupBy`` o una ``metric`` fuori dai valori ammessi producono un
        ``422`` da FastAPI prima di raggiungere il servizio.
    """
    groups, considered = await results_service.aggregate_results(
        group_by=group_by,
        metric=metric,
        scenario_path=scenario_path,
        session_item_ids=_split_ids(session_item_ids),
        session_ids=_split_ids(session_id),
        client_id=client_id,
        scenario_id=scenario_id,
        target_id=target_id,
    )
    return ResultAggregate(
        groupBy=group_by, metric=metric, groups=groups, considered=considered
    )


@router.get(
    "/{result_id}",
    response_model=Result,
    summary="Recupera un risultato",
    responses={404: {"model": ErrorResponse, "description": "Risultato inesistente"}},
)
async def get_result(result_id: str = _ResultId) -> Result:
    """Restituisce un singolo risultato.

    Riceve:
        result_id: identificativo del risultato, dal path.

    Restituisce:
        ``200`` con il risultato richiesto.

    Fa:
        Delega a ``results_service.get_result``; un id inesistente produce un
        ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await results_service.get_result(result_id)


def _split_ids(raw: str | None) -> list[str]:
    """Spacchetta una lista di identificativi separati da virgola.

    Riceve:
        raw: il valore grezzo della query string, oppure ``None``.

    Restituisce:
        La lista degli id non vuoti; lista vuota se il parametro è assente.

    Fa:
        Rimuove spazi e segmenti vuoti. Non valida il formato degli id: un id
        inesistente semplicemente non produce risultati, il che è il
        comportamento atteso per un filtro.
    """
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]
