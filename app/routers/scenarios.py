"""Router HTTP dell'entità Scenario: CRUD su ``/api/scenarios``.

Il router non tocca MongoDB: valida l'input, delega a ``scenarios_service`` e
lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.
"""

from fastapi import APIRouter, Path, status

from app.models.common import ErrorResponse
from app.models.scenario import Scenario, ScenarioCreate, ScenarioUpdate
from app.services import scenarios_service

router = APIRouter(
    prefix="/scenarios",
    tags=["scenarios"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_ScenarioId = Path(description="Identificativo dello scenario (24 caratteri esadecimali)")


@router.get("", response_model=list[Scenario], summary="Elenca gli scenari")
async def list_scenarios() -> list[Scenario]:
    """Restituisce tutti gli scenari registrati.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista degli scenari ordinata per nome.

    Fa:
        Delega a ``scenarios_service.list_scenarios``.
    """
    return await scenarios_service.list_scenarios()


@router.get(
    "/{scenario_id}",
    response_model=Scenario,
    summary="Recupera uno scenario",
    responses={404: {"model": ErrorResponse, "description": "Scenario inesistente"}},
)
async def get_scenario(scenario_id: str = _ScenarioId) -> Scenario:
    """Restituisce un singolo scenario.

    Riceve:
        scenario_id: identificativo dello scenario, dal path.

    Restituisce:
        ``200`` con lo scenario richiesto.

    Fa:
        Delega a ``scenarios_service.get_scenario``; un id inesistente produce
        un ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await scenarios_service.get_scenario(scenario_id)


@router.post(
    "",
    response_model=Scenario,
    status_code=status.HTTP_201_CREATED,
    summary="Crea uno scenario",
)
async def create_scenario(payload: ScenarioCreate) -> Scenario:
    """Crea un nuovo scenario.

    Riceve:
        payload: il corpo della richiesta, validato come ``ScenarioCreate``.

    Restituisce:
        ``201`` con lo scenario creato, comprensivo dell'``id`` generato da MongoDB.

    Fa:
        Delega a ``scenarios_service.create_scenario``.
    """
    return await scenarios_service.create_scenario(payload)


@router.put(
    "/{scenario_id}",
    response_model=Scenario,
    summary="Aggiorna uno scenario",
    responses={404: {"model": ErrorResponse, "description": "Scenario inesistente"}},
)
async def update_scenario(payload: ScenarioUpdate, scenario_id: str = _ScenarioId) -> Scenario:
    """Aggiorna i campi valorizzati di uno scenario esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        scenario_id: identificativo dello scenario, dal path.

    Restituisce:
        ``200`` con lo scenario aggiornato.

    Fa:
        Delega a ``scenarios_service.update_scenario``. Un payload vuoto produce
        un ``422 VALIDATION_ERROR``, un id inesistente un ``404 NOT_FOUND``.
    """
    return await scenarios_service.update_scenario(scenario_id, payload)


@router.delete(
    "/{scenario_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina uno scenario",
    responses={404: {"model": ErrorResponse, "description": "Scenario inesistente"}},
)
async def delete_scenario(scenario_id: str = _ScenarioId) -> None:
    """Elimina uno scenario.

    Riceve:
        scenario_id: identificativo dello scenario, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``scenarios_service.delete_scenario``; se lo scenario non
        esisteva risponde ``404 NOT_FOUND`` invece di fingere un successo.
    """
    await scenarios_service.delete_scenario(scenario_id)
