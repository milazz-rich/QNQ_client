"""Misurazione tramite Chromium headless (Playwright) e Chrome DevTools Protocol.

Espone lo **stesso contratto** di ``curl_client``::

    async def measure(url, protocol, timeout_ms) -> Measurement

così che ``measurement.runner`` possa scegliere il motore di misura senza
conoscerne l'implementazione (vedi la mappa ``MEASUREMENT_BACKENDS``).

A differenza di curl, il protocollo non è un semplice flag: va imposto al
processo browser all'avvio, e la scelta dei flag è stata verificata
empiricamente (vedi AGENTS.md §5.6). In particolare il certificato self-signed
del target richiede ``--ignore-certificate-errors-spki-list``: il flag generico
``--ignore-certificate-errors`` fa fallire l'handshake QUIC.

I timing non provengono da un template come il ``-w`` di curl, ma dagli eventi
del dominio *Network* del CDP, filtrati sulla richiesta di tipo ``Document``:
un browser carica anche le sotto-risorse della pagina, che non vanno confuse
con la misura del documento principale.
"""

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings
from app.models.common import Protocol
from app.services.measurement.curl_client import Measurement, is_http_success

logger = logging.getLogger(__name__)

# Mappa il valore di ``response.protocol`` del CDP sul Protocol di dominio.
# Solo h2 e h3 sono misure valide: "http/1.1" e qualunque altro valore restano
# fuori da questa mappa e sono trattati come fallimento, esattamente come in
# ``curl_client``. Serve perché Chrome, con --disable-quic su un server che non
# parla HTTP/2, ripiega **in silenzio** su HTTP/1.1 rispondendo 200.
_VALID_NEGOTIATED_PROTOCOLS: dict[str, Protocol] = {
    "h2": Protocol.HTTP2,
    "h3": Protocol.HTTP3,
}

# Margine, oltre il timeout di navigazione, concesso all'evento
# ``Network.loadingFinished`` del documento: arriva subito dopo il termine
# della navigazione, ma non necessariamente prima che ``goto`` ritorni.
_LOADING_FINISHED_GRACE_MS = 2000


def build_browser_args(protocol: Protocol, url: str) -> list[str]:
    """Costruisce i flag di avvio di Chromium per il protocollo richiesto.

    Riceve:
        protocol: il protocollo da imporre al browser.
        url: l'URL da misurare, da cui si ricava l'origine ``host:porta``.

    Restituisce:
        La lista di flag da passare a ``chromium.launch(args=...)``.

    Fa:
        Per HTTP/2 disattiva QUIC (``--disable-quic``), così il browser non può
        salire su HTTP/3. Per HTTP/3 lo abilita e **forza** l'origine con
        ``--origin-to-force-quic-on``: senza, Chrome userebbe HTTP/2, perché la
        scoperta automatica via header ``alt-svc`` non è affidabile (il target
        annuncia ``h3=":443"`` anche quando lo si interroga su un'altra porta).
        Aggiunge sempre ``--ignore-certificate-errors-spki-list`` se
        configurato, unico modo di far accettare un certificato self-signed
        anche a QUIC, e ``--no-sandbox``, necessario per girare in container o
        come utente senza privilegi.

        L'origine per QUIC è normalizzata a ``host:porta``: senza porta
        esplicita Chrome **ignora in silenzio** la forzatura e ripiega su
        HTTP/2, producendo una misura del protocollo sbagliato invece di un
        errore. Gli URL costruiti da ``curl_client.build_url`` hanno sempre la
        porta, ma la funzione non deve dipendere da quell'invariante.
    """
    args = ["--no-sandbox"]

    if settings.chrome_spki_list:
        args.append(f"--ignore-certificate-errors-spki-list={settings.chrome_spki_list}")

    if protocol is Protocol.HTTP3:
        split = urlsplit(url)
        origin = split.netloc if split.port else f"{split.netloc}:443"
        args += ["--enable-quic", f"--origin-to-force-quic-on={origin}"]
    else:
        args.append("--disable-quic")

    return args


def _failed(error: str) -> Measurement:
    """Costruisce l'esito di una misurazione fallita.

    Riceve:
        error: la descrizione del fallimento.

    Restituisce:
        Un ``Measurement`` con ``succeeded=False``, ``actual_proto=None`` e
        tempi azzerati.

    Fa:
        Garantisce che ogni tentativo produca un esito strutturato invece di
        propagare un'eccezione: il session runner deve poter salvare un
        ``Result`` con ``status="failed"`` e proseguire.
    """
    return Measurement(
        succeeded=False,
        actual_proto=None,
        total_ms=0.0,
        ttfb_ms=0.0,
        kb=0.0,
        response_code=0,
        error=error,
    )


def _to_measurement(response: dict[str, Any], finished: dict[str, Any]) -> Measurement:
    """Converte gli eventi CDP del documento nel modello di dominio.

    Riceve:
        response: il payload di ``Network.responseReceived`` del documento.
        finished: il payload di ``Network.loadingFinished`` corrispondente.

    Restituisce:
        Un ``Measurement`` con tempi in millisecondi e byte in kilobyte.
        ``succeeded`` è ``True`` solo se è arrivata una risposta, il protocollo
        negoziato è HTTP/2 o HTTP/3 **e** lo status HTTP è 2xx (stesso criterio
        di ``curl_client``, vedi ``is_http_success``): un ``403`` di un rate
        limiter arriva regolarmente col protocollo corretto e non deve
        registrarsi come misura valida solo per quello.

    Fa:
        Ricava il tempo totale dalla differenza fra il ``timestamp`` di
        ``loadingFinished`` e il ``requestTime`` del blocco ``timing`` (entrambi
        in secondi su base monotona), e il TTFB da ``receiveHeadersStart``, già
        in millisecondi relativi a ``requestTime``. I byte vengono da
        ``encodedDataLength``, che a differenza di ``size_download`` di curl
        **include gli header compressi**: i due valori non sono confrontabili
        1:1 fra i due client (vedi AGENTS.md §5.6). ``response_code`` è
        popolato sempre, anche sui fallimenti.
    """
    raw_protocol = str(response.get("protocol", ""))
    negotiated = _VALID_NEGOTIATED_PROTOCOLS.get(raw_protocol)
    status = int(response.get("status", 0) or 0)
    timing = response.get("timing") or {}
    got_response = status > 0 and bool(timing)
    protocol_ok = got_response and negotiated is not None
    succeeded = protocol_ok and is_http_success(status)

    if not got_response:
        error = "Nessuna risposta utilizzabile ricevuta dal server."
    elif negotiated is None:
        error = (
            f"Protocollo negoziato non valido: ottenuto "
            f"{raw_protocol or 'sconosciuto'!s}."
        )
        logger.warning(
            "Misura scartata, protocollo negoziato non valido (Chrome, protocol=%s)",
            raw_protocol or "sconosciuto",
        )
    elif not succeeded:
        error = f"Il server ha risposto con un errore: HTTP {status}."
        logger.warning(
            "Misura scartata, risposta HTTP non di successo (Chrome, protocollo %s, codice %d)",
            negotiated.value,
            status,
        )
    else:
        error = None

    if not succeeded:
        # Tempi e byte azzerati come per ogni altro fallimento: non essendo una
        # misura del protocollo richiesto, i numeri non devono poter essere
        # scambiati per dati validi da chi non controlla ``status``.
        return Measurement(
            succeeded=False,
            actual_proto=None,
            total_ms=0.0,
            ttfb_ms=0.0,
            kb=0.0,
            response_code=status,
            error=error,
        )

    total_ms = (float(finished["timestamp"]) - float(timing["requestTime"])) * 1000
    return Measurement(
        succeeded=True,
        actual_proto=negotiated,
        total_ms=max(total_ms, 0.0),
        ttfb_ms=float(timing.get("receiveHeadersStart", 0.0) or 0.0),
        kb=float(finished.get("encodedDataLength", 0.0) or 0.0) / 1024,
        response_code=status,
        error=None,
    )


async def measure(url: str, protocol: Protocol, timeout_ms: int) -> Measurement:
    """Esegue una singola misurazione con Chromium headless.

    Riceve:
        url: l'URL da richiedere.
        protocol: il protocollo da imporre al browser.
        timeout_ms: timeout di navigazione in millisecondi, dalla ``Session``.

    Restituisce:
        Un ``Measurement``: mai un'eccezione per un fallimento di rete o di
        avvio del browser, così che il chiamante possa registrare il risultato
        come ``failed``.

    Fa:
        Avvia un browser **nuovo per ogni misura** — come per curl, ogni
        ripetizione è quindi "a freddo" e include l'handshake completo — con i
        flag prodotti da ``build_browser_args``, apre una sessione CDP, abilita
        il dominio *Network* e naviga. Raccoglie gli eventi del documento e li
        traduce in ``Measurement``.

        Gestisce i due percorsi di fallimento distinti verificati
        empiricamente: HTTP/3 su un server che non parla QUIC solleva
        ``ERR_QUIC_PROTOCOL_ERROR`` durante la navigazione (eccezione, qui
        catturata), mentre HTTP/2 su un server che non parla h2 **riesce**
        ripiegando in silenzio su HTTP/1.1 (nessuna eccezione: il fallimento
        emerge dal controllo sul protocollo negoziato in ``_to_measurement``).
        Anche l'assenza del browser o delle sue librerie di sistema produce un
        fallimento strutturato, non un crash.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return _failed(
            f"Playwright non installato ({exc}). "
            "Eseguire: pip install -r requirements.txt && playwright install chromium"
        )

    document_response: dict[str, Any] | None = None
    finished_by_request: dict[str, dict[str, Any]] = {}
    document_done = asyncio.Event()

    def on_response(params: dict[str, Any]) -> None:
        """Trattiene il primo evento di risposta di tipo ``Document``."""
        nonlocal document_response
        if document_response is None and params.get("type") == "Document":
            document_response = params
            if params["requestId"] in finished_by_request:
                document_done.set()

    def on_finished(params: dict[str, Any]) -> None:
        """Registra la fine del caricamento e sblocca l'attesa del documento."""
        finished_by_request[params["requestId"]] = params
        if document_response is not None and params["requestId"] == document_response["requestId"]:
            document_done.set()

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                headless=True,
                # Il Chrome completo, non "headless shell": è la build con lo
                # stack QUIC pieno, necessaria per misurare HTTP/3.
                channel="chromium",
                args=build_browser_args(protocol, url),
            )
            try:
                context = await browser.new_context(ignore_https_errors=True)
                page = await context.new_page()
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                cdp.on("Network.responseReceived", on_response)
                cdp.on("Network.loadingFinished", on_finished)

                navigation_error: str | None = None
                try:
                    await page.goto(
                        url,
                        wait_until=settings.chrome_wait_until,
                        timeout=timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001 - tradotto in misura fallita
                    navigation_error = str(exc).splitlines()[0].replace("Page.goto: ", "")

                if document_response is not None:
                    try:
                        await asyncio.wait_for(
                            document_done.wait(),
                            timeout=_LOADING_FINISHED_GRACE_MS / 1000,
                        )
                    except TimeoutError:
                        logger.warning(
                            "Evento loadingFinished non ricevuto entro %d ms (Chrome)",
                            _LOADING_FINISHED_GRACE_MS,
                        )
            finally:
                await browser.close()
    except Exception as exc:  # noqa: BLE001 - avvio browser, libreria mancante, ...
        logger.warning("Avvio o esecuzione di Chromium fallita: %s", exc)
        return _failed(f"Chromium non utilizzabile: {exc}")

    if document_response is None:
        return _failed(navigation_error or "Nessuna risposta di tipo Document ricevuta.")

    finished = finished_by_request.get(document_response["requestId"])
    if finished is None:
        return _failed(
            navigation_error
            or "Caricamento del documento non completato (loadingFinished assente)."
        )

    # Il payload dell'evento contiene i dati della risposta nel sotto-oggetto
    # "response"; i metadati di primo livello (requestId, type, ...) non servono.
    return _to_measurement(document_response["response"], finished)
