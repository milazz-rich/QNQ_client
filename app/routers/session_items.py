"""Router HTTP dell'entità SessionItem: CRUD su ``/api/session-items``.

Il router non tocca MongoDB: valida l'input, delega a ``session_items_service``
e lascia che le eccezioni applicative vengano tradotte dagli handler centralizzati.

**Ordine delle rotte.** ``GET``/``DELETE /session-items/orphaned`` sono
dichiarate *prima* di ``GET``/``DELETE /session-items/{session_item_id}``:
entrambe le coppie condividono verbo e livello di path, e FastAPI risolve
nell'ordine di dichiarazione. Invertendole, ``orphaned`` verrebbe interpretato
come un ``session_item_id`` e produrrebbe un ``422`` (non rispetta il pattern a
24 caratteri esadecimali) invece di raggiungere l'endpoint di manutenzione —
stesso principio già applicato a ``/results/aggregate`` (§5.9).

``POST /session-items/batch`` non ha invece bisogno di quell'accortezza: la sua
rotta è statica e quella parametrica omonima è su ``GET``, quindi non c'è
ambiguità di instradamento fra le due.
"""

from fastapi import APIRouter, Path, status

from app.models.common import ErrorResponse
from app.models.session_item import (
    OrphanedSessionItem,
    OrphanedSessionItemsDeleteResult,
    SessionItem,
    SessionItemBatchCreate,
    SessionItemBatchResult,
    SessionItemCreate,
    SessionItemUpdate,
)
from app.services import session_items_service

router = APIRouter(
    prefix="/session-items",
    tags=["session-items"],
    responses={
        422: {"model": ErrorResponse, "description": "Payload o identificativo non valido"},
        503: {"model": ErrorResponse, "description": "Database non raggiungibile"},
    },
)

_SessionItemId = Path(description="Identificativo del session item (24 caratteri esadecimali)")


@router.get("", response_model=list[SessionItem], summary="Elenca i session item")
async def list_session_items() -> list[SessionItem]:
    """Restituisce tutti i session item registrati.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista dei session item.

    Fa:
        Delega a ``session_items_service.list_session_items``.
    """
    return await session_items_service.list_session_items()


@router.get(
    "/orphaned",
    response_model=list[OrphanedSessionItem],
    summary="Elenca i session item non referenziati da nessuna sessione",
)
async def list_orphaned_session_items() -> list[OrphanedSessionItem]:
    """Restituisce i session item orfani, candidati alla pulizia.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con la lista dei ``SessionItem`` il cui ``id`` non compare
        nell'array ``items`` di nessuna ``Session`` esistente — ciascuno con
        ``scenarioId``, ``protocol``, ``environment`` e ``createdAt``,
        sufficienti per decidere se conservarlo o cancellarlo senza doverlo
        aprire singolarmente.

    Fa:
        Delega a ``session_items_service.list_orphaned_session_items``. È di
        sola lettura: non cancella nulla, a differenza di
        ``DELETE /session-items/orphaned`` sotto. Uno stesso item può ricomparire
        qui anche dopo essere stato "salvato" da un rilancio (§3.3): l'elenco
        riflette sempre lo stato attuale del database, mai una decisione
        precedente.
    """
    return await session_items_service.list_orphaned_session_items()


@router.delete(
    "/orphaned",
    response_model=OrphanedSessionItemsDeleteResult,
    summary="Cancella tutti i session item non referenziati da nessuna sessione",
)
async def delete_orphaned_session_items() -> OrphanedSessionItemsDeleteResult:
    """Cancella tutti i session item correntemente orfani.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con il conteggio e gli ``id`` effettivamente cancellati.
        ``200`` e non ``204`` perché, a differenza di
        ``DELETE /session-items/{id}``, il corpo della risposta è
        l'informazione principale: senza, il chiamante non saprebbe cosa è
        stato rimosso senza un'altra chiamata a ``GET /orphaned`` prima.

    Fa:
        Delega a ``session_items_service.delete_orphaned_session_items``, che
        **ricalcola** l'insieme degli orfani al momento della chiamata — non
        riusa un elenco ottenuto da una precedente ``GET``, che nel frattempo
        potrebbe essere diventato stale (un rilancio può aver riassegnato uno
        di quegli item, §3.3). Il vincolo di integrità referenziale di
        ``DELETE /session-items/{id}`` resta comunque attivo per ciascun item:
        questo endpoint lo riusa internamente invece di duplicarlo, quindi un
        item riassegnato nella finestra fra il calcolo e la cancellazione
        viene saltato, non cancellato a forza. Restituisce sempre ``200``,
        anche con lista vuota: nessun orfano da cancellare non è un errore.
    """
    ids = await session_items_service.delete_orphaned_session_items()
    return OrphanedSessionItemsDeleteResult(count=len(ids), ids=ids)


@router.get(
    "/{session_item_id}",
    response_model=SessionItem,
    summary="Recupera un session item",
    responses={404: {"model": ErrorResponse, "description": "SessionItem inesistente"}},
)
async def get_session_item(session_item_id: str = _SessionItemId) -> SessionItem:
    """Restituisce un singolo session item.

    Riceve:
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``200`` con il session item richiesto.

    Fa:
        Delega a ``session_items_service.get_session_item``; un id inesistente
        produce un ``404 NOT_FOUND`` tramite l'exception handler centralizzato.
    """
    return await session_items_service.get_session_item(session_item_id)


@router.post(
    "",
    response_model=SessionItem,
    status_code=status.HTTP_201_CREATED,
    summary="Crea un session item",
)
async def create_session_item(payload: SessionItemCreate) -> SessionItem:
    """Crea un nuovo session item.

    Riceve:
        payload: il corpo della richiesta, validato come ``SessionItemCreate``.

    Restituisce:
        ``201`` con il session item creato, comprensivo dell'``id`` generato da
        MongoDB.

    Fa:
        Delega a ``session_items_service.create_session_item``.
    """
    return await session_items_service.create_session_item(payload)


@router.post(
    "/batch",
    response_model=SessionItemBatchResult,
    status_code=status.HTTP_201_CREATED,
    summary="Genera il prodotto Scenario × Protocollo × Ambiente",
)
async def create_session_items_batch(
    payload: SessionItemBatchCreate,
) -> SessionItemBatchResult:
    """Genera e crea il prodotto cartesiano richiesto dal wizard.

    Riceve:
        payload: la **specifica** — ``scenarioIds``, ``protocols``,
            ``environments``. Non una lista di item già espansa: è il backend a
            costruire il prodotto. Target, client, ``reps`` e ``timeout`` non
            compaiono: sono della ``Session`` che raccoglierà questi item.

    Restituisce:
        ``201`` con gli ``id`` creati, in ordine deterministico (scenario
        esterno, poi protocollo, poi ambiente).

    Fa:
        Delega a ``session_items_service.create_session_items_batch``. Una
        lista vuota fra ``scenarioIds``/``protocols``/``environments`` produce
        un ``422 VALIDATION_ERROR`` da FastAPI (``min_length=1``) senza toccare
        il database. I valori ripetuti sono deduplicati, per non generare item
        identici non richiesti.
    """
    ids = await session_items_service.create_session_items_batch(payload)
    return SessionItemBatchResult(ids=ids)


@router.put(
    "/{session_item_id}",
    response_model=SessionItem,
    summary="Aggiorna un session item",
    responses={404: {"model": ErrorResponse, "description": "SessionItem inesistente"}},
)
async def update_session_item(
    payload: SessionItemUpdate, session_item_id: str = _SessionItemId
) -> SessionItem:
    """Aggiorna i campi valorizzati di un session item esistente.

    Riceve:
        payload: i campi da modificare; quelli omessi restano invariati.
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``200`` con il session item aggiornato.

    Fa:
        Delega a ``session_items_service.update_session_item``. Un payload
        vuoto produce un ``422 VALIDATION_ERROR``, un id inesistente un
        ``404 NOT_FOUND``.
    """
    return await session_items_service.update_session_item(session_item_id, payload)


@router.delete(
    "/{session_item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Elimina un session item",
    responses={
        404: {"model": ErrorResponse, "description": "SessionItem inesistente"},
        409: {
            "model": ErrorResponse,
            "description": "SessionItem ancora referenziato da una o più sessioni",
        },
    },
)
async def delete_session_item(session_item_id: str = _SessionItemId) -> None:
    """Elimina un session item.

    Riceve:
        session_item_id: identificativo del session item, dal path.

    Restituisce:
        ``204`` senza corpo.

    Fa:
        Delega a ``session_items_service.delete_session_item``; se il session
        item non esisteva risponde ``404 NOT_FOUND`` invece di fingere un
        successo, e se è ancora referenziato da una o più sessioni risponde
        ``409 CONFLICT`` senza procedere alla cancellazione.
    """
    await session_items_service.delete_session_item(session_item_id)
