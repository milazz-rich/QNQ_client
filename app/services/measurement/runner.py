"""Esecuzione di una misurazione a partire dalle entità del dominio.

Collega ``SessionItem`` → ``Target`` / ``Scenario`` / ``Client`` all'invocazione
di curl e produce il ``Result`` corrispondente.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from app.core.errors import NotImplementedFeatureError
from app.models.result import ResultCreate, ResultStatus
from app.models.scenario import Scenario
from app.models.session_item import SessionItem
from app.models.target import Target
from app.services import clients_service, scenarios_service, targets_service
from app.services.measurement import curl_client

logger = logging.getLogger(__name__)

# Unico client supportato: il confronto è case-insensitive per tolleranza
# sull'input dell'utente, ma il nome deve corrispondere esattamente a "curl".
SUPPORTED_CLIENT = "curl"


@dataclass(frozen=True)
class MeasurementContext:
    """Entità risolte necessarie a eseguire le ripetizioni di un session item.

    Attributi:
        session_item: la configurazione della misura.
        target: il server sotto test.
        scenario: il path da richiedere.
        url: l'URL già composto, costante per tutte le ripetizioni.
        target_label: snapshot leggibile del target, salvato in ogni ``Result``.
    """

    session_item: SessionItem
    target: Target
    scenario: Scenario
    url: str
    target_label: str


async def resolve_context(session_item: SessionItem) -> MeasurementContext:
    """Risolve le entità referenziate da un session item.

    Riceve:
        session_item: il session item da eseguire.

    Restituisce:
        Un ``MeasurementContext`` con target, scenario e URL già pronti.

    Fa:
        Carica ``Target``, ``Scenario`` e ``Client`` dal database — sollevando
        ``NotFoundError`` se uno dei riferimenti è rotto — e verifica che il
        client sia ``curl``. Per qualunque altro client (Chrome, Firefox)
        solleva ``NotImplementedFeatureError`` (HTTP 501, codice
        ``NOT_IMPLEMENTED``) invece di un errore generico, così che il frontend
        possa spiegare all'utente che quel motore non è ancora disponibile.
        La risoluzione avviene una sola volta per session item, non a ogni
        ripetizione.
    """
    target = await targets_service.get_target(session_item.target_id)
    scenario = await scenarios_service.get_scenario(session_item.scenario_id)
    client = await clients_service.get_client(session_item.client_id)

    if client.name.strip().lower() != SUPPORTED_CLIENT:
        raise NotImplementedFeatureError(
            f"Il client '{client.name}' non è supportato: al momento è "
            f"implementato solo '{SUPPORTED_CLIENT}'.",
            details={"clientId": client.id, "clientName": client.name},
        )

    url = curl_client.build_url(target.host, target.port, scenario.path)
    return MeasurementContext(
        session_item=session_item,
        target=target,
        scenario=scenario,
        url=url,
        target_label=f"{target.name} ({target.host}:{target.port})",
    )


async def measure_once(context: MeasurementContext, idx: int) -> ResultCreate:
    """Esegue una singola ripetizione e ne costruisce il risultato.

    Riceve:
        context: il contesto risolto da ``resolve_context``.
        idx: indice della ripetizione, a partire da 0.

    Restituisce:
        Un ``ResultCreate`` pronto per essere salvato, con ``status`` pari a
        ``completed`` o ``failed``.

    Fa:
        Invoca curl tramite ``curl_client.measure`` e traduce l'esito nel
        modello ``Result``. Un fallimento non solleva eccezioni: produce un
        risultato con ``status="failed"``, tempi a zero e ``actualProto`` pari a
        ``"unknown"``, in modo che l'esecuzione della sessione prosegua e il
        fallimento resti visibile nei dati. Il campo ``proto`` conserva sempre
        il protocollo *richiesto*; l'eventuale fallback finisce in
        ``actualProto``.
    """
    item = context.session_item
    measurement = await curl_client.measure(
        url=context.url,
        protocol=context.target.protocol,
        timeout_ms=item.timeout,
    )

    if not measurement.succeeded:
        logger.warning(
            "Misurazione fallita (item=%s, idx=%d): %s", item.id, idx, measurement.error
        )

    return ResultCreate(
        sessionItemId=item.id,
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
