"""Orchestrazione dell'esecuzione di una sessione di misurazioni.

Gira come background task di FastAPI: ``POST /api/sessions/{id}/start`` risponde
subito e l'esecuzione prosegue qui, così che il polling del frontend veda
l'avanzamento in tempo reale.

Le misurazioni sono eseguite **in sequenza**, mai in parallelo: due richieste
concorrenti si contenderebbero la banda e falserebbero il confronto fra HTTP/2 e
HTTP/3, che è l'unica cosa che questa applicazione deve misurare. Fra una
ripetizione e la successiva viene inserita una pausa fissa
(``settings.measurement_delay_ms``), applicata sempre e per ogni combinazione
di client/target/protocollo: vedi ``_run_single_item`` e AGENTS.md §5.3.
"""

import asyncio
import logging
from datetime import UTC, datetime

from app.core import session_logging
from app.core.config import settings
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

        L'intera esecuzione gira dentro ``session_logging.session_log_context``:
        tutto ciò che viene loggato qui dentro — da questo modulo, da
        ``measurement.runner`` e dai tre client di misura — finisce anche in
        ``logs/sessions/{sessionId}.log`` (§5.10). Il contesto avvolge anche il
        ``finally``, così la riga di esito finale è l'ultima del file invece di
        restare fuori dalla cattura.
    """
    with session_logging.session_log_context(session_id):
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
                logger.exception(
                    "Impossibile impostare lo stato finale della sessione %s", session_id
                )


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
            item_succeeded = await _run_single_item(session, index, item)
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
            await _save_skipped_item_result(session, item, exc.message)
        except Exception as exc:
            logger.exception("Item %d della sessione %s interrotto", index, session.id)
            final_status = RunStatus.FAILED
            any_item_failed = True
            await _save_skipped_item_result(session, item, str(exc))
        finally:
            await sessions_service.update_item_progress(session.id, index, status=final_status)
    return not any_item_failed


async def _save_skipped_item_result(
    session: Session, item: SessionProgressItem, reason: str
) -> None:
    """Tenta di salvare il ``Result`` segnaposto per un item mai misurato.

    Riceve:
        session: la sessione in esecuzione; fornisce ``sessionId``, ``targetId``
            e ``clientId``, che ora vivono sulla Session e non sull'item.
        item: l'item di avanzamento saltato, prima di qualunque ripetizione.
        reason: messaggio dell'errore che ha impedito la misura.

    Restituisce:
        ``None``.

    Fa:
        ``targetId`` e ``clientId`` sono sempre disponibili (li porta la
        ``Session``), mentre ``scenarioId`` ed ``environment`` — anch'essi
        obbligatori in ``Result`` — vivono sul ``SessionItem`` e vanno
        riletti. Il ``SessionItem`` non può essere stato cancellato nel
        frattempo: ``session_items_service.delete_session_item`` rifiuta la
        cancellazione (``ConflictError``) finché una ``Session`` lo referenzia
        ancora nel proprio ``items``, quindi la rilettura qui non fallisce mai
        per un id inesistente.
    """
    session_item = await session_items_service.get_session_item(item.session_item_id)

    result = ResultCreate(
        sessionId=session.id,
        sessionItemId=item.session_item_id,
        targetId=session.target_id,
        clientId=session.client_id,
        scenarioId=session_item.scenario_id,
        environment=session_item.environment,
        idx=item.done,
        target=item.label,
        scenarioPath=f"(non eseguito: {reason})",
        proto=item.proto,
        actualProto=None,
        total=0.0,
        ttfb=0.0,
        kb=0.0,
        responseCode=0,
        status=ResultStatus.FAILED,
        time=datetime.now(UTC),
    )
    await results_service.create_result(result)


async def _run_single_item(session: Session, index: int, item: SessionProgressItem) -> bool:
    """Esegue le ripetizioni di un singolo item di una sessione.

    Riceve:
        session: la sessione in esecuzione; fornisce id, ``targetId`` e
            ``clientId`` (che ora vivono sulla Session, non sull'item).
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
        Carica il ``SessionItem`` di configurazione e risolve il contesto:
        target e client vengono dalla ``Session``, mentre scenario, protocollo
        e ambiente vengono dall'item — ed è l'ambiente a selezionare quale
        endpoint del target interrogare (``target.endpoints[environment]``,
        vedi ``measurement.runner.resolve_context``). Elimina
        gli eventuali risultati di una **precedente esecuzione di questa stessa
        sessione** per questo item — scoping per ``(sessionId, sessionItemId)``,
        non per solo ``sessionItemId``: il ``SessionItem`` può essere condiviso
        da altre sessioni, i cui risultati non vanno toccati — e poi esegue
        ``session.reps`` misurazioni, ciascuna con timeout ``session.timeout``:
        entrambi vivono sulla ``Session``, non sul ``SessionItem`` (§3.3 di
        AGENTS.md), perché sono uguali per ogni combinazione Scenario ×
        Protocollo × Ambiente della stessa sessione. Dopo ogni ripetizione
        salva il ``Result``, incrementa ``done`` sul database (così che il
        polling del frontend veda l'avanzamento progredire) e attende
        ``settings.measurement_delay_ms`` prima della ripetizione successiva —
        sempre, riuscita o fallita che sia la misura appena fatta, per
        qualunque client/target/protocollo: la pausa mitiga il rate limiter
        sulle nuove connessioni osservato su OpenLiteSpeed (§5.3 di AGENTS.md),
        ma è deliberatamente uniforme e non condizionata al caso specifico, per
        non introdurre una variabile in più fra i dati raccolti su target
        diversi. Il campo ``total`` è riallineato a ``session.reps`` prima di
        partire, perché è la ``Session`` la fonte di verità sul numero di
        ripetizioni.
    """
    session_item = await session_items_service.get_session_item(item.session_item_id)
    context = await measurement_runner.resolve_context(
        session_item, target_id=session.target_id, client_id=session.client_id
    )

    await results_service.delete_results_by_session_and_item(session.id, session_item.id)
    await sessions_service.update_item_progress(
        session.id, index, total=session.reps, done=0
    )

    logger.info(
        "Item %d: %d ripetizioni %s su %s [%s]",
        index,
        session.reps,
        session_item.protocol.value,
        context.url,
        session_item.environment.value,
    )

    any_rep_failed = False
    for idx in range(session.reps):
        result = await measurement_runner.measure_once(
            context, idx, session.id, timeout_ms=session.timeout
        )
        await results_service.create_result(result)
        if result.status is ResultStatus.FAILED:
            any_rep_failed = True
        await sessions_service.update_item_progress(session.id, index, done=idx + 1)
        await asyncio.sleep(settings.measurement_delay_ms / 1000)
    return not any_rep_failed
