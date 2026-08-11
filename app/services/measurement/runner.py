"""Esecuzione di una misurazione a partire dalle entità del dominio.

Collega ``SessionItem`` → ``Target`` / ``Scenario`` / ``Client`` all'invocazione
di curl e produce il ``Result`` corrispondente.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType

from app.core.errors import NotImplementedFeatureError
from app.models.client import Client
from app.models.result import ResultCreate, ResultStatus
from app.models.scenario import Scenario
from app.models.session_item import SessionItem
from app.models.target import Target
from app.services import clients_service, scenarios_service, targets_service
from app.services.measurement import chrome_client, curl_client

logger = logging.getLogger(__name__)

# Motori di misura disponibili, indicizzati per nome del ``Client``. La chiave è
# in minuscolo: il confronto è case-insensitive per tolleranza sull'input
# dell'utente ("Chrome", "chrome", "CURL" sono equivalenti).
#
# Ogni modulo qui dentro deve esporre lo stesso contratto::
#
#     async def measure(url: str, protocol: Protocol, timeout_ms: int) -> Measurement
#
# È il punto di estensione del sistema: aggiungere un motore significa scrivere
# un modulo che rispetti quel contratto e registrarlo qui, senza toccare né i
# router, né i service CRUD, né il session runner (vedi docs/ARCHITETTURA.md §6.2).
MEASUREMENT_BACKENDS: dict[str, ModuleType] = {
    "curl": curl_client,
    "chrome": chrome_client,
}


@dataclass(frozen=True)
class MeasurementContext:
    """Entità risolte necessarie a eseguire le ripetizioni di un session item.

    Attributi:
        session_item: la configurazione della misura.
        target: il server sotto test.
        scenario: il path da richiedere.
        client: il motore di misura scelto, come entità di dominio; il suo
            ``id`` finisce in ``Result.clientId``.
        url: l'URL già composto, costante per tutte le ripetizioni.
        target_label: snapshot leggibile del target, salvato in ogni ``Result``.
        backend: il modulo che esegue materialmente la misura (``curl_client``
            o ``chrome_client``), già risolto dal nome del ``Client``: le
            ripetizioni non devono ridecidere quale motore usare.
    """

    session_item: SessionItem
    target: Target
    scenario: Scenario
    client: Client
    url: str
    target_label: str
    backend: ModuleType


async def resolve_context(session_item: SessionItem) -> MeasurementContext:
    """Risolve le entità referenziate da un session item.

    Riceve:
        session_item: il session item da eseguire.

    Restituisce:
        Un ``MeasurementContext`` con target, scenario e URL già pronti.

    Fa:
        Carica ``Target``, ``Scenario`` e ``Client`` dal database — sollevando
        ``NotFoundError`` se uno dei riferimenti è rotto — e risolve il nome del
        client nel motore di misura corrispondente tramite
        ``MEASUREMENT_BACKENDS``. Un client non presente nella mappa (es.
        Firefox) solleva ``NotImplementedFeatureError`` (HTTP 501, codice
        ``NOT_IMPLEMENTED``) invece di un errore generico, così che il frontend
        possa spiegare all'utente che quel motore non è ancora disponibile.
        La risoluzione avviene una sola volta per session item, non a ogni
        ripetizione.
    """
    target = await targets_service.get_target(session_item.target_id)
    scenario = await scenarios_service.get_scenario(session_item.scenario_id)
    client = await clients_service.get_client(session_item.client_id)

    backend = MEASUREMENT_BACKENDS.get(client.name.strip().lower())
    if backend is None:
        supported = ", ".join(sorted(MEASUREMENT_BACKENDS))
        raise NotImplementedFeatureError(
            f"Il client '{client.name}' non è supportato: al momento sono "
            f"implementati solo {supported}.",
            details={"clientId": client.id, "clientName": client.name},
        )

    # La composizione dell'URL è identica per ogni motore: sta in curl_client
    # solo perché è dove è nata, non perché sia specifica di curl.
    url = curl_client.build_url(target.host, target.port, scenario.path)
    return MeasurementContext(
        session_item=session_item,
        target=target,
        scenario=scenario,
        client=client,
        url=url,
        target_label=f"{target.name} ({target.host}:{target.port})",
        backend=backend,
    )


async def measure_once(context: MeasurementContext, idx: int, session_id: str) -> ResultCreate:
    """Esegue una singola ripetizione e ne costruisce il risultato.

    Riceve:
        context: il contesto risolto da ``resolve_context``.
        idx: indice della ripetizione, a partire da 0.
        session_id: identificativo della sessione che sta eseguendo la misura,
            salvato in ``Result.sessionId`` per legare il risultato alla singola
            esecuzione (il ``SessionItem`` può essere condiviso fra sessioni).

    Restituisce:
        Un ``ResultCreate`` pronto per essere salvato, con ``status`` pari a
        ``completed`` o ``failed``.

    Fa:
        Invoca il motore di misura risolto in ``context.backend`` (curl o
        Chrome) e traduce l'esito nel
        modello ``Result``. Un fallimento non solleva eccezioni: produce un
        risultato con ``status="failed"``, tempi a zero e ``actualProto=None``,
        in modo che l'esecuzione della sessione prosegua e il fallimento resti
        visibile nei dati. Questo include il caso in cui curl riceve una
        risposta ma il protocollo negoziato non è HTTP/2 né HTTP/3 (es.
        fallback su HTTP/1.1): non è una misura valida del protocollo
        richiesto, quindi conta come fallimento anche se la richiesta di rete
        è andata a buon fine. Il campo ``proto`` conserva sempre il protocollo
        *richiesto*; ``actualProto`` è valorizzato solo quando ``status`` è
        ``completed``, ed è sempre HTTP/2 o HTTP/3 in quel caso. ``targetId`` e
        ``clientId`` sono presi dalle entità già risolte da ``resolve_context``.
    """
    item = context.session_item
    measurement = await context.backend.measure(
        url=context.url,
        protocol=context.target.protocol,
        timeout_ms=item.timeout,
    )

    if not measurement.succeeded:
        logger.warning(
            "Misurazione fallita (item=%s, idx=%d): %s", item.id, idx, measurement.error
        )

    return ResultCreate(
        sessionId=session_id,
        sessionItemId=item.id,
        targetId=context.target.id,
        clientId=context.client.id,
        idx=idx,
        target=context.target_label,
        scenarioPath=context.scenario.path,
        proto=context.target.protocol,
        actualProto=measurement.actual_proto,
        total=measurement.total_ms,
        ttfb=measurement.ttfb_ms,
        kb=measurement.kb,
        status=ResultStatus.COMPLETED if measurement.succeeded else ResultStatus.FAILED,
        time=datetime.now(UTC),
    )
