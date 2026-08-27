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
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

from app.core.config import settings
from app.models.common import Protocol
from app.services.measurement import navigation
from app.services.measurement.curl_client import Measurement, is_http_success

logger = logging.getLogger(__name__)

# Nome del motore nei messaggi d'errore prodotti da ``navigation``.
_ENGINE = "Chrome"

# Margine, oltre il timeout di navigazione, concesso all'evento
# ``Network.loadingFinished`` del documento: arriva subito dopo il termine
# della navigazione, ma non necessariamente prima che ``goto`` ritorni. È
# specifico del CDP, quindi resta qui e non in ``navigation``.
_LOADING_FINISHED_GRACE_MS = 2000


class ChromeNavigationError(RuntimeError):
    """Indica una catena di navigazione Chrome non misurabile in modo coerente."""


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


def _log_navigation_failure(navigation_error: str | None, error: str) -> None:
    """Logga un fallimento di navigazione Chrome col nome del motore.

    Riceve:
        navigation_error: il messaggio catturato da ``page.goto()``, o
            ``None`` se il fallimento non viene da lì (nessun ``Document``
            ricevuto pur senza eccezione).
        error: il messaggio effettivamente registrato nel ``Measurement``.

    Restituisce:
        ``None``.

    Fa:
        Un fallimento di rete a basso livello (``ERR_CONNECTION_REFUSED``,
        ``ERR_CONNECTION_TIMED_OUT``, DNS non risolto, ...) solleva
        un'eccezione da ``page.goto()`` che veniva catturata **senza alcun
        log**: l'unica traccia era la riga generica di
        ``measurement.runner`` (``Misurazione fallita (item=..., idx=...):
        ...``), che non nomina né il motore né riporta esplicitamente
        host/porta. Per curl e Firefox questa stessa classe di errore produce
        comunque una riga col nome del motore — curl perché il proprio
        stderr inizia con ``curl:``, Firefox perché logga esplicitamente
        ``Misura Firefox fallita`` — mentre Chrome restava silenzioso,
        rompendo la coerenza fra i tre client su un caso diagnostico
        importante quanto un protocollo negoziato male. Logga a `WARNING`,
        coerentemente con gli altri scarti di questo client.
    """
    logger.warning("Misura scartata, navigazione Chrome fallita: %s", navigation_error or error)


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


def _is_allowed_internal_navigation(target_url: str, candidate_url: str) -> bool:
    """Applica la regola condivisa di navigazione interna col nome di questo motore."""
    return navigation.is_allowed_internal_navigation(target_url, candidate_url, _ENGINE)


def _validate_navigation_chain(target_url: str, urls: list[str]) -> None:
    """Solleva l'eccezione di dominio Chrome se la catena esce dal contenuto misurato."""
    disallowed = navigation.find_disallowed_navigation(target_url, urls, _ENGINE)
    if disallowed is not None:
        raise ChromeNavigationError(
            "Navigazione principale Chrome fuori dal contenuto QNQ misurato: "
            f"{disallowed}."
        )


async def _wait_for_main_frame_quiet(
    page: Any,
    mark: int,
    main_frame_urls: list[str],
    target_url: str,
) -> None:
    """Attende una finestra bounded di quiete della catena main-frame."""
    deadline = asyncio.get_running_loop().time() + (
        navigation.MAIN_FRAME_SETTLE_TIMEOUT_MS / 1000
    )
    previous_count = len(main_frame_urls) - mark

    while True:
        await asyncio.sleep(navigation.MAIN_FRAME_QUIET_MS / 1000)
        current_count = len(main_frame_urls) - mark
        _validate_navigation_chain(target_url, main_frame_urls[mark:])
        if current_count == previous_count:
            return

        previous_count = current_count
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            raise ChromeNavigationError(
                "Timeout durante la stabilizzazione della catena main-frame Chrome."
            )
        # Un'attesa scaduta o interrotta non è un errore: serve solo a dare
        # tempo alla catena di stabilizzarsi, e il ciclo verifica comunque la
        # condizione di uscita al giro successivo. Stessa forma usata da
        # ``firefox_client`` nel punto equivalente.
        with suppress(Exception):
            await page.wait_for_load_state(
                settings.chrome_wait_until,
                timeout=min(remaining_ms, 500),
            )


def _final_document_response(
    document_responses: list[dict[str, Any]],
    target_url: str,
) -> dict[str, Any] | None:
    """Seleziona l'ultimo Document CDP ancora interno al contenuto QNQ."""
    for response in reversed(document_responses):
        response_url = str((response.get("response") or {}).get("url") or "")
        if response_url and _is_allowed_internal_navigation(target_url, response_url):
            return response
    return None


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
    negotiated = navigation.VALID_NEGOTIATED_PROTOCOLS.get(raw_protocol)
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
    document_responses: list[dict[str, Any]] = []
    finished_by_request: dict[str, dict[str, Any]] = {}
    finished_events: dict[str, asyncio.Event] = {}
    main_frame_urls: list[str] = []

    def on_response(params: dict[str, Any]) -> None:
        """Trattiene gli eventi di risposta di tipo ``Document``."""
        if params.get("type") == "Document":
            document_responses.append(params)
            finished_events.setdefault(params["requestId"], asyncio.Event())
            if params["requestId"] in finished_by_request:
                finished_events[params["requestId"]].set()

    def on_finished(params: dict[str, Any]) -> None:
        """Registra la fine del caricamento e sblocca l'attesa del documento."""
        finished_by_request[params["requestId"]] = params
        finished_events.setdefault(params["requestId"], asyncio.Event()).set()

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
                page.on(
                    "framenavigated",
                    lambda frame: main_frame_urls.append(str(frame.url))
                    if frame == page.main_frame
                    else None,
                )
                cdp = await context.new_cdp_session(page)
                await cdp.send("Network.enable")
                cdp.on("Network.responseReceived", on_response)
                cdp.on("Network.loadingFinished", on_finished)

                navigation_error: str | None = None
                navigation_mark = len(main_frame_urls)
                try:
                    await page.goto(
                        url,
                        wait_until=settings.chrome_wait_until,
                        timeout=timeout_ms,
                    )
                except Exception as exc:  # noqa: BLE001 - tradotto in misura fallita
                    navigation_error = str(exc).splitlines()[0].replace("Page.goto: ", "")

                if document_responses:
                    await _wait_for_main_frame_quiet(
                        page,
                        navigation_mark,
                        main_frame_urls,
                        url,
                    )
                    document_response = _final_document_response(document_responses, url)
                    if document_response is not None:
                        try:
                            await asyncio.wait_for(
                                finished_events.setdefault(
                                    document_response["requestId"], asyncio.Event()
                                ).wait(),
                                timeout=_LOADING_FINISHED_GRACE_MS / 1000,
                            )
                        except TimeoutError:
                            logger.warning(
                                "Evento loadingFinished non ricevuto entro %d ms (Chrome)",
                                _LOADING_FINISHED_GRACE_MS,
                            )
            finally:
                await browser.close()
    except ChromeNavigationError as exc:
        # Loggato al pari degli altri scarti del client (protocollo errato,
        # status non 2xx) e come fa firefox_client sul percorso equivalente:
        # senza, questo era l'unico fallimento di Chrome visibile solo dal
        # runner, quindi assente dal log di sessione col nome del motore.
        logger.warning("Misura scartata, catena di navigazione non valida (Chrome): %s", exc)
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001 - avvio browser, libreria mancante, ...
        logger.warning("Avvio o esecuzione di Chromium fallita: %s", exc)
        return _failed(f"Chromium non utilizzabile: {exc}")

    if document_response is None:
        error = navigation_error or "Nessuna risposta di tipo Document ricevuta."
        _log_navigation_failure(navigation_error, error)
        return _failed(error)

    finished = finished_by_request.get(document_response["requestId"])
    if finished is None:
        error = (
            navigation_error
            or "Caricamento del documento non completato (loadingFinished assente)."
        )
        _log_navigation_failure(navigation_error, error)
        return _failed(error)

    # Il payload dell'evento contiene i dati della risposta nel sotto-oggetto
    # "response"; i metadati di primo livello (requestId, type, ...) non servono.
    return _to_measurement(document_response["response"], finished)
