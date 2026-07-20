"""Orchestrazione dell'esecuzione di una sessione di misurazioni.

Gira come background task di FastAPI: ``POST /api/sessions/{id}/start`` risponde
subito e l'esecuzione prosegue qui, così che il polling del frontend veda
l'avanzamento in tempo reale.

Le misurazioni sono eseguite **in sequenza**, mai in parallelo: due richieste
concorrenti si contenderebbero la banda e falserebbero il confronto fra HTTP/2 e
HTTP/3, che è l'unica cosa che questa applicazione deve misurare.
"""

import logging
from datetime import UTC, datetime

from app.core.errors import AppError
from app.models.result import ResultCreate, ResultStatus
from app.models.session import RunStatus, Session, SessionProgressItem
from app.services import results_service, session_items_service, sessions_service
from app.services.measurement import runner as measurement_runner

logger = logging.getLogger(__name__)


async def start_session(session_id: str) -> None:
    """Esegue tutti gli item di una sessione, dal primo all'ultimo.

    Riceve:
        session_id: identificativo della sessione da eseguire.

    Restituisce:
        ``None``.

    Fa:
        Porta la sessione in ``running``, esegue in sequenza ogni item
        aggiornandone l'avanzamento sul database man mano, e al termine la porta
        in ``completed``. Non solleva mai eccezioni verso il chiamante: gira
        come background task, dove un'eccezione non gestita finirebbe solo nei
        log lasciando la sessione bloccata in ``running`` per sempre. Ogni
        errore viene registrato e la sessione viene comunque chiusa.
    """
    logger.info("Avvio esecuzione sessione %s", session_id)
    try:
        session = await sessions_service.get_session(session_id)
        await sessions_service.set_status(session_id, RunStatus.RUNNING)
        await _run_items(session)
    except Exception:
        logger.exception("Esecuzione della sessione %s interrotta da un errore", session_id)
    finally:
        try:
            await sessions_service.set_status(session_id, RunStatus.COMPLETED)
            logger.info("Sessione %s completata", session_id)
        except AppError:
            logger.exception("Impossibile marcare come completata la sessione %s", session_id)


async def _run_items(session: Session) -> None:
    """Esegue in sequenza tutti gli item di avanzamento di una sessione.

    Riceve:
        session: la sessione già caricata, con la lista ``items`` da eseguire.

    Restituisce:
        ``None``.

    Fa:
        Per ogni item aggiorna ``currentIndex``, lo porta in ``running``, esegue
        le ripetizioni e lo chiude in ``completed``. Un item che fallisce in
        modo irrecuperabile (riferimento rotto, client non supportato) non è mai
        stato misurato: viene marcato ``failed`` (non ``completed``, per non
        farlo contare come dato valido), viene comunque salvato un ``Result``
        con ``status="failed"`` così il fallimento resta tracciato invece di
        sparire silenziosamente, e l'esecuzione prosegue con il successivo —
        una configurazione sbagliata su un item non deve invalidare l'intera
        sessione.
    """
    for index, item in enumerate(session.items):
        await sessions_service.set_current_index(session.id, index)
        await sessions_service.update_item_progress(
            session.id, index, status=RunStatus.RUNNING, done=0
        )
        final_status = RunStatus.COMPLETED
        try:
            await _run_single_item(session.id, index, item)
        except AppError as exc:
            logger.error(
                "Item %d della sessione %s saltato: [%s] %s",
                index,
                session.id,
                exc.code,
                exc.message,
            )
            final_status = RunStatus.FAILED
            await results_service.create_result(_skipped_item_result(item, exc.message))
        except Exception as exc:
            logger.exception("Item %d della sessione %s interrotto", index, session.id)
            final_status = RunStatus.FAILED
            await results_service.create_result(_skipped_item_result(item, str(exc)))
        finally:
            await sessions_service.update_item_progress(session.id, index, status=final_status)


def _skipped_item_result(item: SessionProgressItem, reason: str) -> ResultCreate:
    """Costruisce il ``Result`` segnaposto per un item mai misurato.

    Riceve:
        item: l'item di avanzamento saltato, prima di qualunque ripetizione.
        reason: messaggio dell'errore che ha impedito la misura.

    Restituisce:
        Un ``ResultCreate`` con ``status="failed"``, tempi a zero e
        ``actualProto="unknown"``, coerente con come ``measurement.runner``
        rappresenta un fallimento di misura.

    Fa:
        Non ha un ``Target``/``Scenario`` risolti a disposizione (l'errore può
        derivare proprio dalla loro risoluzione), quindi usa ``item.label`` e il
        messaggio d'errore come snapshot leggibili al posto dei campi
        denormalizzati abituali.
    """
    return ResultCreate(
        sessionItemId=item.session_item_id,
        idx=item.done,
        target=item.label,
        scenarioPath=f"(non eseguito: {reason})",
        proto=item.proto,
        actualProto="unknown",
        total=0.0,
        ttfb=0.0,
        kb=0.0,
        status=ResultStatus.FAILED,
        time=datetime.now(UTC),
    )


async def _run_single_item(session_id: str, index: int, item: SessionProgressItem) -> None:
    """Esegue le ripetizioni di un singolo item di una sessione.

    Riceve:
        session_id: identificativo della sessione in esecuzione.
        index: posizione dell'item nella lista ``items``.
        item: l'item di avanzamento, che referenzia il ``SessionItem``.

    Restituisce:
        ``None``.

    Fa:
        Carica il ``SessionItem`` di configurazione, risolve target, scenario e
        client (``NotImplementedFeatureError`` se il client non è curl), elimina
        gli eventuali risultati di una esecuzione precedente e poi esegue
        ``reps`` misurazioni. Dopo ogni ripetizione salva il ``Result`` e
        incrementa ``done`` sul database, così che il polling del frontend veda
        l'avanzamento progredire. Il campo ``total`` è riallineato a ``reps``
        prima di partire, perché è il ``SessionItem`` la fonte di verità sul
        numero di ripetizioni.
    """
    session_item = await session_items_service.get_session_item(item.session_item_id)
    context = await measurement_runner.resolve_context(session_item)

    await results_service.delete_results_by_session_items([session_item.id])
    await sessions_service.update_item_progress(
        session_id, index, total=session_item.reps, done=0
    )

    logger.info(
        "Item %d: %d ripetizioni %s su %s",
        index,
        session_item.reps,
        context.target.protocol.value,
        context.url,
    )

    for idx in range(session_item.reps):
        result = await measurement_runner.measure_once(context, idx)
        await results_service.create_result(result)
        await sessions_service.update_item_progress(session_id, index, done=idx + 1)
