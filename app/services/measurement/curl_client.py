"""Invocazione del binario curl esterno e parsing dei timing.

Il protocollo (HTTP/2 o HTTP/3) è un semplice flag della riga di comando, quindi
non esistono due implementazioni separate: c'è un solo esecutore parametrizzato
sul protocollo. La differenza fra i due casi è confinata in ``_protocol_flag``.
"""

import asyncio
import json
import logging
from dataclasses import dataclass

from app.core.config import settings
from app.models.target import Protocol

logger = logging.getLogger(__name__)

# Template del flag -w di curl: produce una riga JSON per ogni richiesta.
# I valori numerici non sono quotati, quelli testuali sì.
_WRITE_OUT = (
    '{"http_version":"%{http_version}",'
    '"response_code":%{response_code},'
    '"time_total":%{time_total},'
    '"time_starttransfer":%{time_starttransfer},'
    '"size_download":%{size_download}}'
)

# Mappa il valore di %{http_version} di curl sul Protocol di dominio. Solo
# HTTP/2 e HTTP/3 sono misure valide: HTTP/1.0, HTTP/1.1 e qualunque valore non
# riconosciuto restano fuori da questa mappa e sono trattati come fallimento
# (vedi ``_to_measurement``), perché non rappresentano ciò che l'applicazione
# deve confrontare.
_VALID_NEGOTIATED_PROTOCOLS: dict[str, Protocol] = {
    "2": Protocol.HTTP2,
    "3": Protocol.HTTP3,
}


def is_http_success(response_code: int) -> bool:
    """Indica se un codice di stato HTTP rappresenta un successo applicativo.

    Riceve:
        response_code: il codice numerico riportato da curl.

    Restituisce:
        ``True`` solo per la classe 2xx.

    Fa:
        Il protocollo può essere negoziato correttamente anche quando il
        server risponde con un errore (verificato empiricamente: un rate
        limiter per-IP di LiteSpeed produce ``403`` su connessioni HTTP/2
        ravvicinate, con `http_version` comunque `"2"`). Una misura che riceve
        un errore applicativo non è una misura valida della pagina richiesta,
        quindi non basta un protocollo negoziato corretto — serve anche un
        esito HTTP di successo. 3xx, 4xx e 5xx contano come fallimento.
    """
    return 200 <= response_code < 300


@dataclass(frozen=True)
class Measurement:
    """Esito di una singola invocazione di curl.

    Attributi:
        succeeded: ``True`` se curl ha ricevuto una risposta, il protocollo
            negoziato è HTTP/2 o HTTP/3 **e** il codice di stato è 2xx. Un
            fallback su HTTP/1.1, un protocollo non determinabile, o un
            errore applicativo (es. ``403``/``500`` con protocollo corretto)
            contano tutti come fallimento: nessuno dei tre è una misura valida
            della pagina richiesta.
        actual_proto: protocollo effettivamente negoziato, solo se
            ``succeeded`` (sempre e solo HTTP/2 o HTTP/3); ``None`` altrimenti.
        total_ms: durata totale della richiesta in millisecondi.
        ttfb_ms: time-to-first-byte in millisecondi.
        kb: kilobyte scaricati.
        response_code: codice di stato HTTP effettivamente ricevuto, ``0`` se
            la richiesta non è arrivata. Popolato **sempre**, anche quando
            ``succeeded`` è ``False``: è ciò che permette di distinguere un
            errore di rete da un errore applicativo del server.
        error: descrizione del fallimento, ``None`` se andata a buon fine.
    """

    succeeded: bool
    actual_proto: Protocol | None
    total_ms: float
    ttfb_ms: float
    kb: float
    response_code: int
    error: str | None = None


def build_url(host: str, port: int, path: str) -> str:
    """Compone l'URL da richiedere.

    Riceve:
        host: hostname o IP del target, senza schema.
        port: porta del servizio.
        path: path dello scenario, già validato come iniziante con ``/``.

    Restituisce:
        L'URL completo in forma ``https://host:port/path``.

    Fa:
        Usa sempre ``https``: sia HTTP/2 sia HTTP/3 richiedono TLS nella
        pratica (HTTP/3 lo richiede per definizione, essendo su QUIC). La porta
        è sempre esplicita, anche se 443, per non perdere l'informazione
        configurata sul target.
    """
    return f"https://{host}:{port}{path}"


def _protocol_flag(protocol: Protocol) -> str:
    """Restituisce il flag curl che forza il protocollo richiesto.

    Riceve:
        protocol: il protocollo dichiarato dal target.

    Restituisce:
        ``"--http3"`` oppure ``"--http2"``.

    Fa:
        Nota che ``--http3`` non è vincolante: se il server non risponde su
        QUIC, curl ripiega su HTTP/2 o HTTP/1.1. Il fallback viene rilevato a
        posteriori confrontando ``%{http_version}`` con il protocollo richiesto.
    """
    return "--http3" if protocol is Protocol.HTTP3 else "--http2"


def build_command(
    url: str,
    protocol: Protocol,
    timeout_ms: int,
) -> list[str]:
    """Costruisce la riga di comando di curl per una singola misurazione.

    Riceve:
        url: l'URL da richiedere.
        protocol: il protocollo da forzare.
        timeout_ms: timeout della richiesta in millisecondi.

    Restituisce:
        La lista di argomenti da passare a ``asyncio.create_subprocess_exec``.

    Fa:
        Scarta il corpo della risposta (``-o /dev/null``) per non falsare i
        tempi con la scrittura su disco, silenzia la progress bar (``-s``) e
        chiede i timing tramite il template ``-w``. Aggiunge sempre
        ``--no-keepalive``: ogni ripetizione è comunque un processo curl
        separato (nessuna connessione sopravvive fra un processo e l'altro), il
        flag rende esplicito che ogni misura è sempre "a freddo", invece di
        lasciare intendere un riuso che non può avvenire. Se
        ``CURL_CA_BUNDLE_PATH`` è configurata, aggiunge ``--cacert <path>``:
        serve a validare certificati self-signed o emessi da una CA privata
        (es. il target di test `milaz.it`) senza disabilitare la verifica TLS
        (`-k`), che varrebbe per qualunque target e nasconderebbe anche
        problemi reali. Gli argomenti sono passati come lista, mai come
        stringa di shell: host e path arrivano dal database e non devono poter
        essere interpretati come comandi.
    """
    command = [
        settings.curl_path,
        "-s",
        "-S",
        "-o",
        "/dev/null",
        _protocol_flag(protocol),
        "--max-time",
        f"{timeout_ms / 1000:.3f}",
        "-w",
        _WRITE_OUT,
        "--no-keepalive",
    ]
    if settings.curl_ca_bundle:
        command.extend(["--cacert", settings.curl_ca_bundle])
    command.append(url)
    return command


def _parse_write_out(raw: str) -> dict[str, object]:
    """Interpreta la riga JSON prodotta dal flag ``-w`` di curl.

    Riceve:
        raw: lo stdout del processo curl.

    Restituisce:
        Il dizionario dei timing.

    Fa:
        Solleva ``ValueError`` se l'output non è JSON valido, cosa che accade
        quando curl fallisce prima di emettere la riga di ``-w``.
    """
    payload = json.loads(raw.strip())
    if not isinstance(payload, dict):
        raise ValueError("L'output di curl non è un oggetto JSON.")
    return payload


def _to_measurement(payload: dict[str, object], requested: Protocol) -> Measurement:
    """Converte i timing grezzi di curl nel modello di dominio.

    Riceve:
        payload: il dizionario prodotto da ``_parse_write_out``.
        requested: il protocollo richiesto, usato per riconoscere il fallback.

    Restituisce:
        Un ``Measurement`` con i tempi in millisecondi e i byte in kilobyte.
        ``succeeded`` è ``True`` solo se è arrivata una risposta, il protocollo
        negoziato è HTTP/2 o HTTP/3 **e** il codice di stato è 2xx; in tal caso
        ``actual_proto`` è valorizzato di conseguenza (può comunque differire
        da ``requested`` in caso di fallback fra i due). Altrimenti
        ``succeeded=False`` e ``actual_proto=None``: né un fallback su
        HTTP/1.1 né un errore applicativo (4xx/5xx) con protocollo corretto
        sono una misura valida della pagina richiesta.

    Fa:
        curl riporta i tempi in secondi e la dimensione in byte: qui vengono
        convertiti nelle unità del modello ``Result`` (ms e KB). Un
        ``response_code`` pari a 0 significa che nessuna risposta è arrivata e
        viene trattato come fallimento anche se il processo è uscito con 0.
        Il protocollo negoziato non basta da solo: un `403` di un rate limiter
        (§5.3 di AGENTS.md) arriva regolarmente su HTTP/2, quindi occorre
        controllare anche ``is_http_success``. Tempi e byte sono azzerati in
        ogni caso di fallimento, coerentemente: non essendo una misura valida,
        i numeri non devono poter essere scambiati per dati validi da chi
        legge solo `total`/`ttfb` senza controllare `status`. ``response_code``
        resta invece **sempre** popolato con il valore effettivo, anche sui
        fallimenti: è l'unico modo per capire a posteriori se un item è
        fallito per un errore di rete (`response_code=0`) o per un errore
        applicativo del server (`response_code` 4xx/5xx).
    """
    raw_version = str(payload.get("http_version", ""))
    negotiated = _VALID_NEGOTIATED_PROTOCOLS.get(raw_version)
    response_code = int(payload.get("response_code", 0) or 0)
    got_response = response_code > 0
    protocol_ok = got_response and negotiated is not None
    succeeded = protocol_ok and is_http_success(response_code)

    if not got_response:
        error = "Nessuna risposta ricevuta dal server."
    elif negotiated is None:
        error = (
            f"Protocollo negoziato non valido: richiesto {requested.value}, "
            f"ottenuto http_version={raw_version or 'sconosciuto'}."
        )
        logger.warning(
            "Misura scartata, protocollo negoziato non valido (richiesto %s, "
            "http_version=%s)",
            requested.value,
            raw_version or "sconosciuto",
        )
    elif not succeeded:
        error = f"Il server ha risposto con un errore: HTTP {response_code}."
        logger.warning(
            "Misura scartata, risposta HTTP non di successo (protocollo %s, codice %d)",
            negotiated.value,
            response_code,
        )
    else:
        error = None
        if negotiated is not requested:
            logger.info(
                "Fallback di protocollo: richiesto %s, negoziato %s",
                requested.value,
                negotiated.value,
            )

    return Measurement(
        succeeded=succeeded,
        actual_proto=negotiated if succeeded else None,
        total_ms=float(payload.get("time_total", 0.0) or 0.0) * 1000 if succeeded else 0.0,
        ttfb_ms=float(payload.get("time_starttransfer", 0.0) or 0.0) * 1000 if succeeded else 0.0,
        kb=float(payload.get("size_download", 0.0) or 0.0) / 1024 if succeeded else 0.0,
        response_code=response_code,
        error=error,
    )


def _failed(protocol: Protocol, error: str) -> Measurement:
    """Costruisce l'esito di una misurazione fallita.

    Riceve:
        protocol: il protocollo richiesto.
        error: la descrizione del fallimento.

    Restituisce:
        Un ``Measurement`` con ``succeeded=False`` e tempi azzerati.

    Fa:
        Garantisce che ogni tentativo produca comunque un esito strutturato: il
        session runner deve poter salvare un ``Result`` con ``status="failed"``
        invece di saltare silenziosamente la ripetizione.
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


async def measure(
    url: str,
    protocol: Protocol,
    timeout_ms: int,
) -> Measurement:
    """Esegue una singola misurazione invocando il binario curl.

    Riceve:
        url: l'URL da richiedere.
        protocol: il protocollo da forzare (``--http2`` o ``--http3``).
        timeout_ms: timeout della richiesta in millisecondi, dal ``SessionItem``.

    Restituisce:
        Un ``Measurement``: mai un'eccezione per un fallimento di rete, così
        che il chiamante possa registrare il risultato come ``failed``.

    Fa:
        Lancia curl come sottoprocesso asincrono (``create_subprocess_exec``,
        senza shell) e ne attende l'esito con un timeout più generoso di quello
        passato a ``--max-time``: il margine serve a lasciare a curl la
        possibilità di terminare da solo e riportare l'errore, e a garantire che
        un processo bloccato venga comunque ucciso invece di fermare l'intera
        sessione. Se il timeout esterno scatta, il processo viene terminato e
        atteso, per non lasciare processi zombie.
    """
    command = build_command(url, protocol, timeout_ms)
    process_timeout = (timeout_ms + settings.curl_kill_grace_ms) / 1000

    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return _failed(
            protocol,
            f"Binario curl non trovato in '{settings.curl_path}'. "
            "Verificare CURL_BINARY_PATH nel file .env.",
        )
    except OSError as exc:
        return _failed(protocol, f"Impossibile avviare curl: {exc}")

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=process_timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return _failed(protocol, f"curl non ha risposto entro {process_timeout:.1f}s (ucciso).")

    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or f"exit code {process.returncode}"
        return _failed(protocol, f"curl ha fallito: {detail}")

    try:
        payload = _parse_write_out(stdout.decode(errors="replace"))
    except (ValueError, json.JSONDecodeError) as exc:
        return _failed(protocol, f"Output di curl non interpretabile: {exc}")

    return _to_measurement(payload, protocol)
