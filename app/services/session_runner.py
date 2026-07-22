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
        in ``completed`` se tutti gli item sono riusciti, in ``failed`` se
        almeno uno è terminato con ``status="failed"`` (configurazione rotta,
        client non supportato, errore imprevisto). Non solleva mai eccezioni
        verso il chiamante: gira come background task, dove un'eccezione non
        gestita finirebbe solo nei log lasciando la sessione bloccata in
        ``running`` per sempre. Ogni errore viene registrato e la sessione
        viene comunque chiusa; un'eccezione inattesa **prima** che
        ``_run_items`` possa riportare l'esito reale è trattata come
        ``failed``, non ``completed``, perché non c'è garanzia che gli item
        siano stati eseguiti correttamente.
    """
    logger.info("Avvio esecuzione sessione %s", session_id)
    all_items_succeeded = False
    try:
        session = await sessions_service.get_session(session_id)
        await sessions_service.set_status(session_id, RunStatus.RUNNING)
        all_items_succeeded = await _run_items(session)
    except Exception:
        logger.exception("Esecuzione della sessione %s interrotta da un errore", session_id)
    finally:
        final_status = RunStatus.COMPLETED if all_items_succeeded else RunStatus.FAILED
        try:
            await sessions_service.set_status(session_id, final_status)
            logger.info("Sessione %s conclusa con stato %s", session_id, final_status.value)
        except AppError:
            logger.exception("Impossibile impostare lo stato finale della sessione %s", session_id)


async def _run_items(session: Session) -> bool:
    """Esegue in sequenza tutti gli item di avanzamento di una sessione.

    Riceve:
        session: la sessione già caricata, con la lista ``items`` da eseguire.

    Restituisce:
        ``True`` se tutti gli item sono terminati con ``status="completed"``,
        ``False`` se almeno uno è terminato con ``status="failed"``. Usato da
        ``start_session`` per decidere lo stato finale della sessione.

    Fa:
        Per ogni item aggiorna ``currentIndex``, lo porta in ``running``, esegue
        le ripetizioni e lo chiude in ``completed`` **solo se sono riuscite
        tutte**. Due modi distinti in cui un item può risultare ``failed``:

        1. *Mai eseguito* — configurazione rotta o client non supportato:
           ``_run_single_item`` solleva un'eccezione prima di qualunque
           ripetizione. Viene comunque salvato un ``Result`` segnaposto con
           ``status="failed"`` così il fallimento resta tracciato invece di
           sparire silenziosamente (vedi ``_save_skipped_item_result``).
        2. *Eseguito ma senza successo* — ``_run_single_item`` porta a termine
           tutte le ripetizioni (``done`` raggiunge ``total``), ma almeno una
           ha prodotto un ``Result`` con ``status="failed"``: l'item non ha
           prodotto una misura valida e non deve risultare ``completed`` solo
           perché ha *tentato* tutte le ripetizioni.

        In entrambi i casi l'esecuzione prosegue con l'item successivo — una
        configurazione sbagliata o una misura fallita su un item non deve
        invalidare l'intera sessione (che comunque, nel suo complesso,
        risulterà ``failed`` se anche un solo item lo è).
    """
    any_item_failed = False
    for index, item in enumerate(session.items):
        await sessions_service.set_current_index(session.id, index)
        await sessions_service.update_item_progress(
            session.id, index, status=RunStatus.RUNNING, done=0
        )
        final_status = RunStatus.COMPLETED
        try:
            item_succeeded = await _run_single_item(session.id, index, item)
            if not item_succeeded:
                final_status = RunStatus.FAILED
                any_item_failed = True
        except AppError as exc:
            logger.error(
                "Item %d della sessione %s saltato: [%s] %s",
                index,
                session.id,
                exc.code,
                exc.message,
            )
            final_status = RunStatus.FAILED
            any_item_failed = True
            await _save_skipped_item_result(session.id, item, exc.message)
        except Exception as exc:
            logger.exception("Item %d della sessione %s interrotto", index, session.id)
            final_status = RunStatus.FAILED
            any_item_failed = True
            await _save_skipped_item_result(session.id, item, str(exc))
        finally:
            await sessions_service.update_item_progress(session.id, index, status=final_status)
    return not any_item_failed


async def _save_skipped_item_result(session_id: str, item: SessionProgressItem, reason: str) -> None:
    """Tenta di salvare il ``Result`` segnaposto per un item mai misurato.

    Riceve:
        session_id: identificativo della sessione in esecuzione, salvato in
            ``Result.sessionId``.
        item: l'item di avanzamento saltato, prima di qualunque ripetizione.
        reason: messaggio dell'errore che ha impedito la misura.

    Restituisce:
        ``None``.

    Fa:
        ``targetId`` è un riferimento obbligatorio in ``Result``: per
        valorizzarlo occorre recuperare il ``SessionItem`` di configurazione
        (l'errore che ha fatto saltare l'item può derivare dalla risoluzione
        di Target/Scenario/Client, ma il ``SessionItem`` stesso — e quindi il
        suo ``targetId`` — è di solito ancora leggibile). Se anche il
        ``SessionItem`` non esiste più (cancellato dopo essere stato
        referenziato da questa sessione), non c'è alcun target a cui legare
        il risultato: il fallimento resta comunque nei log applicativi, ma
        nessun ``Result`` viene creato per questo item.
    """
    try:
        session_item = await session_items_service.get_session_item(item.session_item_id)
    except AppError:
        logger.warning(
            "Nessun Result creato per l'item %d della sessione %s: "
            "SessionItem '%s' non più risolvibile (targetId sconosciuto).",
            item.done,
            session_id,
            item.session_item_id,
        )
        return

    result = ResultCreate(
        sessionId=session_id,
        sessionItemId=item.session_item_id,
        targetId=session_item.target_id,
        idx=item.done,
        target=item.label,
        scenarioPath=f"(non eseguito: {reason})",
        proto=item.proto,
        actualProto=None,
        total=0.0,
        ttfb=0.0,
        kb=0.0,
        status=ResultStatus.FAILED,
        time=datetime.now(UTC),
    )
    await results_service.create_result(result)


async def _run_single_item(session_id: str, index: int, item: SessionProgressItem) -> bool:
    """Esegue le ripetizioni di un singolo item di una sessione.

    Riceve:
        session_id: identificativo della sessione in esecuzione.
        index: posizione dell'item nella lista ``items``.
        item: l'item di avanzamento, che referenzia il ``SessionItem``.

    Restituisce:
        ``True`` se **tutte** le ripetizioni sono terminate con un ``Result``
        ``status="completed"``, ``False`` se almeno una è ``"failed"``. Usato
        da ``_run_items`` per decidere lo stato finale dell'item: un item i cui
        tentativi sono tutti falliti (o anche solo in parte) non ha prodotto
        una misura valida, e non deve risultare ``completed`` solo perché
        ``done`` ha raggiunto ``total``. Questo è distinto dal caso — gestito
        dal chiamante tramite eccezione — di un item mai eseguito perché la
        configurazione era rotta o il client non supportato.

    Fa:
        Carica il ``SessionItem`` di configurazione, risolve target, scenario e
        client (``NotImplementedFeatureError`` se il client non è curl), elimina
        gli eventuali risultati di una **precedente esecuzione di questa stessa
        sessione** per questo item — scoping per ``(sessionId, sessionItemId)``,
        non per solo ``sessionItemId``: il ``SessionItem`` può essere condiviso
        da altre sessioni, i cui risultati non vanno toccati — e poi esegue
        ``reps`` misurazioni. Dopo ogni ripetizione salva il ``Result`` e
        incrementa ``done`` sul database, così che il polling del frontend veda
        l'avanzamento progredire. Il campo ``total`` è riallineato a ``reps``
        prima di partire, perché è il ``SessionItem`` la fonte di verità sul
        numero di ripetizioni.
    """
    session_item = await session_items_service.get_session_item(item.session_item_id)
    context = await measurement_runner.resolve_context(session_item)

    await results_service.delete_results_by_session_and_item(session_id, session_item.id)
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

    any_rep_failed = False
    for idx in range(session_item.reps):
        result = await measurement_runner.measure_once(context, idx, session_id)
        await results_service.create_result(result)
        if result.status is ResultStatus.FAILED:
            any_rep_failed = True
        await sessions_service.update_item_progress(session_id, index, done=idx + 1)
    return not any_rep_failed
