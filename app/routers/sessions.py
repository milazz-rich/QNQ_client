"""Router HTTP dell'entità Session: CRUD su ``/api/sessions`` più l'avvio.

Il router non tocca MongoDB: valida l'input, delega a ``sessions_service`` e
lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.
"""

from fastapi import APIRouter, BackgroundTasks, Path, status
from pydantic import BaseModel, Field

from app.core.errors import ConflictError
from app.models.common import ErrorResponse
from app.models.session import RunStatus, Session, SessionCreate, SessionUpdate
from app.services import session_runner, sessions_service

router = APIRouter(
    prefix="/sessions",
    tags=["sessions"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_SessionId = Path(description="Identificativo della sessione (24 caratteri esadecimali)")


class SessionStartResponse(BaseModel):
    """Conferma di avvio di una sessione."""

    id: str = Field(description="Identificativo della sessione avviata")
    status: RunStatus = Field(description="Stato della sessione subito dopo l'avvio")
    items: int = Field(description="Numero di item che verranno eseguiti")


@router.get("", response_model=list[Session], summary="Elenca le sessioni")
async def list_sessions() -> list[Session]:
    """Restituisce tutte le sessioni registrate.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista delle sessioni, dalla più recente alla più vecchia.

    Fa:
        Delega a ``sessions_service.list_sessions``.
    """
    return await sessions_service.list_sessions()


@router.get(
    "/{session_id}",
    response_model=Session,
    summary="Recupera una sessione",
    responses={404: {"model": ErrorResponse, "description": "Sessione inesistente"}},
)
async def get_session(session_id: str = _SessionId) -> Session:
    """Restituisce una singola sessione.

    Riceve:
        session_id: identificativo della sessione, dal path.

    Restituisce:
        ``200`` con la sessione richiesta, incluso l'avanzamento di ogni item.

    Fa:
        Delega a ``sessions_service.get_session``. È l'endpoint su cui il
        frontend fa polling durante l'esecuzione per mostrare il progresso.
    """
    return await sessions_service.get_session(session_id)


@router.post(
    "",
    response_model=Session,
    status_code=status.HTTP_201_CREATED,
    summary="Crea una sessione",
)
async def create_session(payload: SessionCreate) -> Session:
    """Crea una nuova sessione.

    Riceve:
        payload: il corpo della richiesta, validato come ``SessionCreate``.

    Restituisce:
        ``201`` con la sessione creata, comprensiva dell'``id`` generato da MongoDB.

    Fa:
        Delega a ``sessions_service.create_session``.
    """
    return await sessions_service.create_session(payload)


@router.post(
    "/{session_id}/start",
    response_model=SessionStartResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Avvia l'esecuzione di una sessione",
    responses={
        404: {"model": ErrorResponse, "description": "Sessione inesistente"},
        409: {"model": ErrorResponse, "description": "Sessione già in esecuzione"},
    },
)
async def start_session(
    background_tasks: BackgroundTasks, session_id: str = _SessionId
) -> SessionStartResponse:
    """Avvia l'esecuzione di una sessione in background.

    Riceve:
        background_tasks: il raccoglitore di task di FastAPI, iniettato dal framework.
        session_id: identificativo della sessione da avviare, dal path.

    Restituisce:
        ``202`` con id, stato e numero di item: la risposta parte subito, prima
        che l'esecuzione sia terminata. Il frontend segue l'avanzamento facendo
        polling su ``GET /api/sessions/{id}``.

    Fa:
        Verifica che la sessione esista, che non sia già in esecuzione
        (``409 CONFLICT``) e che abbia almeno un item (``422``), poi accoda
        ``session_runner.start_session``. Lo stato viene portato a ``running``
        prima di rispondere, non dentro il task, così che un polling immediato
        non veda ancora ``pending`` e creda che l'avvio sia fallito.
    """
    from app.core.errors import ValidationError

    session = await sessions_service.get_session(session_id)
    if session.status is RunStatus.RUNNING:
        raise ConflictError(f"La sessione '{session_id}' è già in esecuzione.")
    if not session.items:
        raise ValidationError(f"La sessione '{session_id}' non contiene item da eseguire.")

    await sessions_service.set_status(session_id, RunStatus.RUNNING)
    background_tasks.add_task(session_runner.start_session, session_id)

    return SessionStartResponse(
        id=session_id, status=RunStatus.RUNNING, items=len(session.items)
    )


@router.put(
    "/{session_id}",
    response_model=Session,
    summary="Aggiorna una sessione",
    responses={404: {"model": ErrorResponse, "description": "Sessione inesistente"}},
)
async def update_session(payload: SessionUpdate, session_id: str = _SessionId) -> Session:
    """Aggiorna i campi valorizzati di una sessione esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        session_id: identificativo della sessione, dal path.

    Restituisce:
        ``200`` con la sessione aggiornata.

    Fa:
        Delega a ``sessions_service.update_session``. Un payload vuoto produce
        un ``422 VALIDATION_ERROR``, un id inesistente un ``404 NOT_FOUND``.
    """
    return await sessions_service.update_session(session_id, payload)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina una sessione",
    responses={404: {"model": ErrorResponse, "description": "Sessione inesistente"}},
)
async def delete_session(session_id: str = _SessionId) -> None:
    """Elimina una sessione.

    Riceve:
        session_id: identificativo della sessione, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``sessions_service.delete_session``; se la sessione non
        esisteva risponde ``404 NOT_FOUND`` invece di fingere un successo.
    """
    await sessions_service.delete_session(session_id)
