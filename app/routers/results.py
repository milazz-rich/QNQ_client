"""Router HTTP dell'entità Result: sola lettura su ``/api/results``.

I risultati non sono creati dal client ma prodotti dal session runner, quindi
non esistono ``POST``/``PUT``/``DELETE``.
"""

from fastapi import APIRouter, Path, Query

from app.models.common import ErrorResponse
from app.models.result import Result
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


@router.get("", response_model=list[Result], summary="Elenca i risultati")
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
            "Filtra per singola esecuzione di sessione. Preferito a "
            "sessionItemIds per ottenere i risultati di UNA sessione: diretto e "
            "senza ambiguità quando un SessionItem è condiviso fra più sessioni"
        ),
    ),
) -> list[Result]:
    """Restituisce i risultati, opzionalmente filtrati.

    Riceve:
        scenario_path: valore di ``?scenarioPath=``, filtra per singolo scenario.
        session_item_ids: valore di ``?sessionItemIds=``, lista di id separati
            da virgola.
        session_id: valore di ``?sessionId=``, filtra per la singola esecuzione
            di sessione. È il filtro preferito per "i risultati di questa
            sessione": ``sessionItemIds`` resta disponibile ma è ambiguo quando
            uno stesso ``SessionItem`` è condiviso fra più sessioni.

    Restituisce:
        ``200`` con la lista dei risultati che soddisfano i filtri, ordinata per
        istante di completamento crescente. I filtri si combinano in AND.

    Fa:
        Spacchetta la lista comma-joined scartando i segmenti vuoti — così
        ``?sessionItemIds=a,,b`` e un parametro vuoto non generano filtri
        spuri — e delega a ``results_service.list_results``.
    """
    ids = _split_ids(session_item_ids)
    return await results_service.list_results(
        scenario_path=scenario_path, session_item_ids=ids, session_id=session_id
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
