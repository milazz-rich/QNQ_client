"""Esecuzione di una misurazione a partire dalle entità del dominio.

Collega ``SessionItem`` → ``Target`` / ``Scenario`` / ``Client`` all'invocazione
del motore di misura e produce il ``Result`` corrispondente.

Il ``Target`` e il ``Client`` non arrivano dal ``SessionItem`` ma dalla
``Session`` che lo esegue (vedi AGENTS.md §3.3): sono uguali per tutti gli item
di una sessione, quindi il chiamante li passa espliciti. Dal ``SessionItem``
arrivano invece i parametri che variano fra un item e l'altro — scenario,
protocollo e **ambiente**, quest'ultimo usato per scegliere quale endpoint del
target interrogare.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from types import ModuleType

from app.core.errors import NotFoundError, NotImplementedFeatureError
from app.models.client import Client
from app.models.result import ResultCreate, ResultStatus
from app.models.scenario import Scenario
from app.models.session_item import SessionItem
from app.models.target import Target, TargetEndpoint
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
        session_item: la configurazione della misura (scenario, protocollo,
            ambiente). Ripetizioni e timeout non sono più qui: vivono sulla
            ``Session`` (§3.3 di AGENTS.md) e arrivano a ``measure_once`` come
            parametro esplicito, non tramite questo contesto.
        target: il motore sotto test, con tutti i suoi endpoint.
        endpoint: l'endpoint effettivamente interrogato, già risolto
            dall'ambiente dell'item.
        scenario: il path da richiedere.
        client: il motore di misura scelto, come entità di dominio; il suo
            ``id`` finisce in ``Result.clientId``.
        url: l'URL già composto, costante per tutte le ripetizioni.
        target_label: snapshot leggibile del target, salvato in ogni ``Result``;
            include l'ambiente, perché lo stesso ``Target`` ne serve due.
        backend: il modulo che esegue materialmente la misura (``curl_client``
            o ``chrome_client``), già risolto dal nome del ``Client``: le
            ripetizioni non devono ridecidere quale motore usare.
    """

    session_item: SessionItem
    target: Target
    endpoint: TargetEndpoint
    scenario: Scenario
    client: Client
    url: str
    target_label: str
    backend: ModuleType


async def resolve_context(
    session_item: SessionItem, target_id: str, client_id: str
) -> MeasurementContext:
    """Risolve le entità necessarie a eseguire un session item.

    Riceve:
        session_item: il session item da eseguire; fornisce scenario,
            protocollo e ambiente.
        target_id: il motore sotto test, **dalla Session** (uguale per tutti
            gli item della sessione).
        client_id: il client di misura, **dalla Session**.

    Restituisce:
        Un ``MeasurementContext`` con target, endpoint risolto, scenario e URL
        già pronti.

    Fa:
        Carica ``Target``, ``Scenario`` e ``Client`` dal database — sollevando
        ``NotFoundError`` se uno dei riferimenti è rotto — e risolve il nome del
        client nel motore di misura corrispondente tramite
        ``MEASUREMENT_BACKENDS``. Un client non presente nella mappa (es.
        Firefox) solleva ``NotImplementedFeatureError`` (HTTP 501, codice
        ``NOT_IMPLEMENTED``) invece di un errore generico, così che il frontend
        possa spiegare all'utente che quel motore non è ancora disponibile.

        Sceglie poi l'endpoint da interrogare con
        ``target.endpoints[session_item.environment]``: è qui che l'ambiente
        smette di essere un'etichetta e diventa un indirizzo concreto. Un
        target che non espone quell'ambiente solleva ``NotFoundError``, così
        l'item viene marcato ``failed`` e tracciato (§5.4) invece di far
        fallire l'intera sessione.

        La risoluzione avviene una sola volta per session item, non a ogni
        ripetizione.
    """
    target = await targets_service.get_target(target_id)
    scenario = await scenarios_service.get_scenario(session_item.scenario_id)
    client = await clients_service.get_client(client_id)

    backend = MEASUREMENT_BACKENDS.get(client.name.strip().lower())
    if backend is None:
        supported = ", ".join(sorted(MEASUREMENT_BACKENDS))
        raise NotImplementedFeatureError(
            f"Il client '{client.name}' non è supportato: al momento sono "
            f"implementati solo {supported}.",
            details={"clientId": client.id, "clientName": client.name},
        )

    environment = session_item.environment
    endpoint = target.endpoints.get(environment)
    if endpoint is None:
        available = ", ".join(sorted(e.value for e in target.endpoints)) or "nessuno"
        raise NotFoundError(
            f"Il target '{target.name}' non espone un endpoint per l'ambiente "
            f"'{environment.value}' (disponibili: {available})."
        )

    # La composizione dell'URL è identica per ogni motore: sta in curl_client
    # solo perché è dove è nata, non perché sia specifica di curl.
    url = curl_client.build_url(endpoint.host, endpoint.port, scenario.path)
    return MeasurementContext(
        session_item=session_item,
        target=target,
        endpoint=endpoint,
        scenario=scenario,
        client=client,
        url=url,
        # L'ambiente entra nell'etichetta: lo stesso Target produce misure su
        # due endpoint diversi, e uno snapshot che non li distinguesse
        # renderebbe i Result ambigui a colpo d'occhio.
        target_label=f"{target.name} [{environment.value}] ({endpoint.host}:{endpoint.port})",
        backend=backend,
    )


async def measure_once(
    context: MeasurementContext, idx: int, session_id: str, timeout_ms: int
) -> ResultCreate:
    """Esegue una singola ripetizione e ne costruisce il risultato.

    Riceve:
        context: il contesto risolto da ``resolve_context``.
        idx: indice della ripetizione, a partire da 0.
        session_id: identificativo della sessione che sta eseguendo la misura,
            salvato in ``Result.sessionId`` per legare il risultato alla singola
            esecuzione (il ``SessionItem`` può essere condiviso fra sessioni).
        timeout_ms: timeout della richiesta in millisecondi, **dalla Session**
            (§3.3 di AGENTS.md): non è più un campo del ``SessionItem``, quindi
            il chiamante lo passa esplicito invece di leggerlo dal contesto.

    Restituisce:
        Un ``ResultCreate`` pronto per essere salvato, con ``status`` pari a
        ``completed`` o ``failed``.

    Fa:
        Invoca il motore di misura risolto in ``context.backend`` (curl o
        Chrome) e traduce l'esito nel
        modello ``Result``. Un fallimento non solleva eccezioni: produce un
        risultato con ``status="failed"``, tempi a zero e ``actualProto=None``,
        in modo che l'esecuzione della sessione prosegua e il fallimento resti
        visibile nei dati. Questo include sia il caso in cui la risposta arriva
        con un protocollo diverso da HTTP/2 o HTTP/3 (es. fallback su HTTP/1.1)
        sia il caso in cui il protocollo è corretto ma lo status HTTP non è 2xx
        (es. un `403` di un rate limiter, verificato empiricamente su un target
        di test): nessuno dei due è una misura valida della pagina richiesta.
        Il campo ``proto`` conserva sempre il protocollo *richiesto*;
        ``actualProto`` è valorizzato solo quando ``status`` è ``completed``, ed
        è sempre HTTP/2 o HTTP/3 in quel caso. ``responseCode`` è invece sempre
        popolato, riuscita o no, con il codice di stato HTTP effettivo (``0``
        se nessuna risposta è arrivata): è ciò che permette di distinguere a
        posteriori un errore di rete da un errore applicativo del server.
        ``targetId``, ``clientId`` e ``scenarioId`` sono presi dalle entità già
        risolte da ``resolve_context``.
    """
    item = context.session_item
    measurement = await context.backend.measure(
        url=context.url,
        protocol=item.protocol,
        timeout_ms=timeout_ms,
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
        scenarioId=context.scenario.id,
        environment=item.environment,
        idx=idx,
        target=context.target_label,
        scenarioPath=context.scenario.path,
        proto=item.protocol,
        actualProto=measurement.actual_proto,
        total=measurement.total_ms,
        ttfb=measurement.ttfb_ms,
        kb=measurement.kb,
        responseCode=measurement.response_code,
        status=ResultStatus.COMPLETED if measurement.succeeded else ResultStatus.FAILED,
        time=datetime.now(UTC),
    )
