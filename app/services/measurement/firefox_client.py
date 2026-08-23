"""Misurazione tramite Firefox headless (Playwright) e Resource Timing.

Espone lo **stesso contratto** di ``curl_client`` e ``chrome_client``::

    async def measure(url, protocol, timeout_ms) -> Measurement

così che ``measurement.runner`` possa scegliere il motore di misura senza
conoscerne l'implementazione (vedi la mappa ``MEASUREMENT_BACKENDS``).

Firefox non espone un equivalente di ``--http3`` o
``--origin-to-force-quic-on``. Per HTTP/3 il client usa quindi un
precondizionamento esplicito del profilo: apprende l'``Alt-Svc`` una volta,
lo lascia persistito in ``AlternateServices.bin`` e, per ogni misura reale,
avvia un nuovo processo Firefox con una copia temporanea di quel profilo.
Prima della richiesta misurata viene effettuata una richiesta HTTP/1.1 neutra a
``127.0.0.1`` per inizializzare il DataStorage di Firefox; quella richiesta non
contatta il target e non entra nei tempi/byte della misura.
"""

import asyncio
import hashlib
import logging
import re
import shutil
import tempfile
import threading
from contextlib import suppress
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import SplitResult, urlsplit, urlunsplit

from app.core.config import settings
from app.models.common import Protocol
from app.services.measurement.curl_client import Measurement, is_http_success

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIREFOX_RUNTIME_ROOT = _REPO_ROOT / ".runtime" / "firefox"
_PREPARED_PROFILE_ROOT = _FIREFOX_RUNTIME_ROOT / "profiles"
_RUN_PROFILE_ROOT = _FIREFOX_RUNTIME_ROOT / "runs"
_PROFILE_LOCKS: dict[str, asyncio.Lock] = {}
_PROFILE_LOCKS_GUARD = asyncio.Lock()
_NAVIGATION_TIMING_RETRIES = 4
_NAVIGATION_TIMING_BACKOFFS = (0.05, 0.1, 0.2)
_MAIN_FRAME_QUIET_MS = 250
_MAIN_FRAME_SETTLE_TIMEOUT_MS = 2500
_EXECUTION_CONTEXT_DESTROYED = (
    "Execution context was destroyed, most likely because of a navigation"
)

_VALID_NEGOTIATED_PROTOCOLS: dict[str, Protocol] = {
    "h2": Protocol.HTTP2,
    "h3": Protocol.HTTP3,
}

_NAVIGATION_ENTRY_JS = """() => {
    const entry = performance.getEntriesByType('navigation')[0];
    if (!entry) return null;
    return {
        name: entry.name,
        nextHopProtocol: entry.nextHopProtocol,
        responseStart: entry.responseStart,
        responseEnd: entry.responseEnd,
        transferSize: entry.transferSize,
        encodedBodySize: entry.encodedBodySize,
    };
}"""


class NavigationTimingError(RuntimeError):
    """Errore recuperabile solo come misura fallita, non come eccezione al runner."""


class AdditionalMainFrameNavigationError(NavigationTimingError):
    """Indica una nuova navigazione del main frame dopo quella misurata."""


class ExternalMainFrameNavigationError(NavigationTimingError):
    """Indica una navigazione main-frame fuori dal contenuto QNQ misurato."""


class MainFrameStabilityTimeoutError(NavigationTimingError):
    """Indica che la catena main-frame non si e stabilizzata nel tempo massimo."""


@dataclass(frozen=True)
class OriginProfile:
    """Identifica il profilo Firefox persistente associato a una origin."""

    scheme: str
    host: str
    port: int
    key: str
    directory: Path


@dataclass(frozen=True)
class MainFrameNavigationMark:
    """Punto della catena main-frame prima della navigazione misurata."""

    navigation_count: int
    response_count: int


@dataclass(frozen=True)
class MainFrameNavigationRecord:
    """Response e timing associati a una navigazione del main frame."""

    url: str
    status: int
    timing: dict[str, Any]
    byte_size_hint: float | None = None


@dataclass(frozen=True)
class NavigationSnapshot:
    """Dati coerenti del documento finale effettivamente misurato."""

    url: str
    status: int
    timing: dict[str, Any]
    entry: dict[str, Any] | None
    byte_size_hint: float | None = None


class _PrimingHandler(BaseHTTPRequestHandler):
    """Risponde alla richiesta locale che inizializza lo stack HTTP di Firefox."""

    server_version = "QNQFirefoxPriming/1.0"
    sys_version = ""

    def do_GET(self) -> None:  # noqa: N802 - nome imposto da BaseHTTPRequestHandler
        body = b"ok\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        logger.debug("Firefox localhost priming: " + format, *args)


class LocalhostPrimingServer:
    """Piccolo server HTTP locale usato solo per il priming DataStorage."""

    def __init__(self) -> None:
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _PrimingHandler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self._server.server_port}/"

    def start(self) -> "LocalhostPrimingServer":
        """Avvia il server locale su una porta dinamica."""
        self._thread.start()
        return self

    def close(self) -> None:
        """Ferma il server locale e rilascia la porta."""
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)


class MainFrameNavigationTracker:
    """Osserva le navigazioni del main frame senza alterare la misura."""

    def __init__(self, page: Any) -> None:
        self._page = page
        self._main_navigation_count = 0
        self._main_navigation_urls: list[str] = []
        self._main_navigation_responses: list[MainFrameNavigationRecord] = []
        page.on("framenavigated", self._on_frame_navigated)
        page.on("response", self._on_response)

    def mark(self) -> MainFrameNavigationMark:
        """Restituisce il contatore corrente delle navigazioni main-frame."""
        return MainFrameNavigationMark(
            navigation_count=self._main_navigation_count,
            response_count=len(self._main_navigation_responses),
        )

    def count_since(self, mark: MainFrameNavigationMark) -> int:
        """Conta le navigazioni main-frame osservate dopo un mark."""
        return self._main_navigation_count - mark.navigation_count

    def urls_since(self, mark: MainFrameNavigationMark) -> list[str]:
        """Elenca gli URL main-frame osservati dopo un mark."""
        return self._main_navigation_urls[mark.navigation_count:]

    def responses_since(self, mark: MainFrameNavigationMark) -> list[MainFrameNavigationRecord]:
        """Elenca le response main-frame osservate dopo un mark."""
        return self._main_navigation_responses[mark.response_count:]

    def _on_frame_navigated(self, frame: Any) -> None:
        if frame == self._page.main_frame:
            self._main_navigation_count += 1
            self._main_navigation_urls.append(str(frame.url))

    def _on_response(self, response: Any) -> None:
        request = response.request
        try:
            is_main_navigation = (
                request.is_navigation_request()
                and request.frame == self._page.main_frame
            )
        except Exception:
            return
        if not is_main_navigation:
            return
        byte_size_hint = _content_length(response)
        self._main_navigation_responses.append(
            MainFrameNavigationRecord(
                url=str(response.url),
                status=int(response.status),
                timing=dict(request.timing),
                byte_size_hint=byte_size_hint,
            )
        )


def build_user_prefs(protocol: Protocol) -> dict[str, Any]:
    """Costruisce le preference di avvio di Firefox per il protocollo richiesto.

    Riceve:
        protocol: il protocollo che si vuole ottenere.

    Restituisce:
        Il dizionario da passare a Playwright per l'avvio di Firefox.

    Fa:
        Per HTTP/2 disabilita HTTP/3 in modo esplicito. Per HTTP/3 abilita lo
        stack h3 e mantiene le preference già verificate sperimentalmente per
        evitare 0-RTT e session identifiers persistenti; non aggiunge preference
        non dimostrate. La preference sui third-party roots resta necessaria sui
        target QNQ con certificato non pubblico, altrimenti Firefox può
        disabilitare HTTP/3 anche se Alt-Svc è presente.
    """
    if protocol is Protocol.HTTP2:
        return {"network.http.http3.enable": False}

    return {
        "network.http.http3.enable": True,
        "network.http.http3.disable_when_third_party_roots_found": False,
        "network.http.http3.enable_0rtt": False,
        "security.tls.enable_0rtt_data": False,
        "security.ssl.disable_session_identifiers": True,
        "browser.cache.disk.enable": False,
        "browser.cache.memory.enable": False,
    }


def _failed(error: str) -> Measurement:
    """Costruisce l'esito di una misurazione fallita."""
    return Measurement(
        succeeded=False,
        actual_proto=None,
        total_ms=0.0,
        ttfb_ms=0.0,
        kb=0.0,
        response_code=0,
        error=error,
    )


def _content_length(response: Any) -> float | None:
    """Legge Content-Length dalla response Playwright, se disponibile e valido."""
    try:
        value = response.headers.get("content-length")
    except Exception:
        return None
    if value is None:
        return None
    try:
        size = float(value)
    except ValueError:
        return None
    return size if size >= 0 else None


def _split_url(url: str) -> SplitResult:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"URL non valido per la misura Firefox: {url!r}.")
    return parsed


def _origin_profile(url: str) -> OriginProfile:
    parsed = _split_url(url)
    host = parsed.hostname.lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    key = f"{parsed.scheme}://{host}:{port}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    safe_host = re.sub(r"[^A-Za-z0-9_.-]+", "_", host)[:80]
    directory = _PREPARED_PROFILE_ROOT / f"{parsed.scheme}_{safe_host}_{port}_{digest}"
    return OriginProfile(parsed.scheme, host, port, key, directory)


def _netloc(host: str, port: int) -> str:
    display_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{display_host}:{port}"


def _origin_root_url(origin: OriginProfile) -> str:
    return urlunsplit((origin.scheme, _netloc(origin.host, origin.port), "/", "", ""))


def _preparation_urls(url: str, origin: OriginProfile) -> list[str]:
    root = _origin_root_url(origin)
    return [root] if root == url else [root, url]


def _altsvc_file(profile_dir: Path) -> Path:
    return profile_dir / "AlternateServices.bin"


def _profile_contains_h3_altsvc(profile_dir: Path, origin: OriginProfile) -> bool:
    altsvc_file = _altsvc_file(profile_dir)
    if not altsvc_file.exists():
        return False
    try:
        content = altsvc_file.read_bytes()
    except OSError:
        return False
    return origin.host.encode("utf-8") in content and b"h3" in content


async def _origin_lock(key: str) -> asyncio.Lock:
    async with _PROFILE_LOCKS_GUARD:
        lock = _PROFILE_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _PROFILE_LOCKS[key] = lock
        return lock


def _remove_tree(path: Path) -> None:
    with suppress(FileNotFoundError):
        shutil.rmtree(path)


def _copy_prepared_profile(prepared_profile: Path) -> Path:
    _RUN_PROFILE_ROOT.mkdir(parents=True, exist_ok=True)
    run_profile = Path(tempfile.mkdtemp(prefix="measure-", dir=_RUN_PROFILE_ROOT))
    shutil.rmtree(run_profile)
    shutil.copytree(
        prepared_profile,
        run_profile,
        ignore=shutil.ignore_patterns("lock", ".parentlock", "parent.lock", "mozlog*"),
    )
    return run_profile


async def _navigate(page: Any, url: str, timeout_ms: int) -> tuple[int, dict[str, Any], Any]:
    response = await page.goto(
        url,
        wait_until=settings.firefox_wait_until,
        timeout=timeout_ms,
    )
    if response is None:
        return 0, {}, None
    timing = dict(response.request.timing)
    return response.status, timing, response


def _failed_with_response(error: str, response_code: int) -> Measurement:
    return Measurement(
        succeeded=False,
        actual_proto=None,
        total_ms=0.0,
        ttfb_ms=0.0,
        kb=0.0,
        response_code=response_code,
        error=error,
    )


def _is_execution_context_destroyed(exc: Exception) -> bool:
    return _EXECUTION_CONTEXT_DESTROYED in str(exc)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _same_origin(first: SplitResult, second: SplitResult) -> bool:
    return (
        first.scheme == second.scheme
        and (first.hostname or "").lower() == (second.hostname or "").lower()
        and (first.port or _default_port(first.scheme))
        == (second.port or _default_port(second.scheme))
    )


def _allowed_path_prefix(target_path: str) -> str:
    if not target_path or target_path == "/":
        return "/"
    if target_path.endswith("/"):
        return target_path
    return target_path.rsplit("/", 1)[0] + "/"


def _is_allowed_internal_navigation(target_url: str, candidate_url: str) -> bool:
    target = _split_url(target_url)
    candidate = _split_url(candidate_url)
    if not _same_origin(target, candidate):
        return False
    return candidate.path.startswith(_allowed_path_prefix(target.path))


def _validate_navigation_chain(target_url: str, urls: list[str]) -> None:
    for navigated_url in urls:
        try:
            allowed = _is_allowed_internal_navigation(target_url, navigated_url)
        except ValueError:
            allowed = False
        if not allowed:
            raise ExternalMainFrameNavigationError(
                "Navigazione principale fuori dal contenuto QNQ misurato: "
                f"{navigated_url}."
            )


async def _wait_for_main_frame_quiet(
    page: Any,
    tracker: Any,
    mark: MainFrameNavigationMark,
    target_url: str,
) -> None:
    """Attende brevemente che emergano navigazioni main-frame immediate."""
    deadline = asyncio.get_running_loop().time() + (_MAIN_FRAME_SETTLE_TIMEOUT_MS / 1000)
    previous_count = tracker.count_since(mark)

    while True:
        await asyncio.sleep(_MAIN_FRAME_QUIET_MS / 1000)
        current_count = tracker.count_since(mark)
        _validate_navigation_chain(target_url, tracker.urls_since(mark))
        if current_count == previous_count:
            return

        previous_count = current_count
        remaining_ms = int((deadline - asyncio.get_running_loop().time()) * 1000)
        if remaining_ms <= 0:
            raise MainFrameStabilityTimeoutError(
                "Timeout durante la stabilizzazione della catena main-frame Firefox."
            )
        with suppress(Exception):
            await page.wait_for_load_state(
                settings.firefox_wait_until,
                timeout=min(remaining_ms, 500),
            )


async def _read_navigation_entry(
    page: Any,
    tracker: Any,
    mark: MainFrameNavigationMark,
    target_url: str,
) -> dict[str, Any] | None:
    """Legge Navigation Timing senza confondere documenti diversi."""
    await _wait_for_main_frame_quiet(page, tracker, mark, target_url)

    last_error: Exception | None = None
    for attempt in range(_NAVIGATION_TIMING_RETRIES):
        try:
            entry = await page.evaluate(_NAVIGATION_ENTRY_JS)
        except Exception as exc:  # noqa: BLE001 - matching selettivo sotto
            if not _is_execution_context_destroyed(exc):
                raise

            last_error = exc
            await _wait_for_main_frame_quiet(page, tracker, mark, target_url)
            logger.debug(
                "Firefox Navigation Timing retry %d/%d: execution context destroyed",
                attempt + 1,
                _NAVIGATION_TIMING_RETRIES,
            )
            if attempt == _NAVIGATION_TIMING_RETRIES - 1:
                break
            await asyncio.sleep(_NAVIGATION_TIMING_BACKOFFS[attempt])
            continue

        await _wait_for_main_frame_quiet(page, tracker, mark, target_url)
        return entry

    raise NavigationTimingError(
        "Navigation Timing Firefox non leggibile dopo retry bounded: "
        f"{last_error or 'errore sconosciuto'}."
    )


def _final_navigation_record(
    tracker: Any,
    mark: MainFrameNavigationMark,
    fallback_response: Any,
) -> MainFrameNavigationRecord | None:
    records = tracker.responses_since(mark)
    if records:
        return records[-1]
    if fallback_response is None:
        return None
    return MainFrameNavigationRecord(
        url=str(fallback_response.url),
        status=int(fallback_response.status),
        timing=dict(fallback_response.request.timing),
        byte_size_hint=_content_length(fallback_response),
    )


def _entry_matches_navigation(entry: dict[str, Any] | None, record: MainFrameNavigationRecord) -> bool:
    entry_name = str((entry or {}).get("name") or "")
    return not entry_name or entry_name == record.url


async def _navigate_and_read_final_document(
    page: Any,
    tracker: MainFrameNavigationTracker,
    url: str,
    timeout_ms: int,
) -> NavigationSnapshot:
    mark = tracker.mark()
    response = await page.goto(
        url,
        wait_until=settings.firefox_wait_until,
        timeout=timeout_ms,
    )
    if response is None:
        return NavigationSnapshot(url=url, status=0, timing={}, entry=None)

    await _wait_for_main_frame_quiet(page, tracker, mark, url)
    record = _final_navigation_record(tracker, mark, response)
    if record is None:
        return NavigationSnapshot(url=url, status=0, timing={}, entry=None)

    entry = await _read_navigation_entry(page, tracker, mark, url)
    if not _entry_matches_navigation(entry, record):
        raise NavigationTimingError(
            "Navigation Timing Firefox non coerente con la response finale: "
            f"entry={entry.get('name') if entry else None}, response={record.url}."
        )
    return NavigationSnapshot(
        url=record.url,
        status=record.status,
        timing=record.timing,
        entry=entry,
        byte_size_hint=record.byte_size_hint,
    )


async def _ensure_http3_profile(playwright: Any, url: str, timeout_ms: int) -> OriginProfile:
    origin = _origin_profile(url)
    lock = await _origin_lock(origin.key)

    async with lock:
        if _profile_contains_h3_altsvc(origin.directory, origin):
            return origin

        _remove_tree(origin.directory)
        origin.directory.mkdir(parents=True, exist_ok=True)
        prep_urls = _preparation_urls(url, origin)
        last_error = "Alt-Svc HTTP/3 non rilevato durante il precondizionamento."

        for attempt in range(2):
            context = None
            try:
                context = await playwright.firefox.launch_persistent_context(
                    user_data_dir=str(origin.directory),
                    headless=True,
                    ignore_https_errors=True,
                    firefox_user_prefs=build_user_prefs(Protocol.HTTP3),
                )
                page = await context.new_page()
                for prep_url in prep_urls:
                    status, _, response = await _navigate(page, prep_url, timeout_ms)
                    alt_svc = ""
                    if response is not None:
                        alt_svc = response.headers.get("alt-svc", "")
                    if "h3" in alt_svc.lower():
                        logger.info(
                            "Profilo Firefox HTTP/3 precondizionato per %s via %s "
                            "(status=%d, attempt=%d)",
                            origin.key,
                            prep_url,
                            status,
                            attempt + 1,
                        )
                        break
                    last_error = (
                        f"Precondizionamento Firefox senza Alt-Svc h3 su {prep_url} "
                        f"(status={status}, alt-svc={alt_svc or 'assente'})."
                    )
            except Exception as exc:  # noqa: BLE001 - riportato come misura fallita
                last_error = f"Precondizionamento Firefox fallito per {origin.key}: {exc}"
                logger.warning(last_error)
            finally:
                if context is not None:
                    await context.close()

            await asyncio.sleep(0.25)
            if _profile_contains_h3_altsvc(origin.directory, origin):
                return origin

        _remove_tree(origin.directory)
        raise RuntimeError(last_error)


async def _measure_http2(playwright: Any, url: str, timeout_ms: int) -> Measurement:
    browser = await playwright.firefox.launch(
        headless=True,
        firefox_user_prefs=build_user_prefs(Protocol.HTTP2),
    )
    try:
        context = await browser.new_context(ignore_https_errors=True)
        try:
            page = await context.new_page()
            tracker = MainFrameNavigationTracker(page)
            try:
                snapshot = await _navigate_and_read_final_document(
                    page, tracker, url, timeout_ms
                )
            except NavigationTimingError as exc:
                return _failed_with_response(str(exc), 0)
        finally:
            await context.close()
    finally:
        await browser.close()

    if snapshot.status == 0:
        return _failed("Nessuna risposta ricevuta dal server.")
    if snapshot.entry is None:
        return _failed("Navigation Timing non disponibile per il documento.")
    return _to_measurement(
        Protocol.HTTP2,
        snapshot.status,
        snapshot.entry,
        snapshot.timing,
        snapshot.byte_size_hint,
    )


async def _prime_datastorage(page: Any, timeout_ms: int) -> None:
    server = LocalhostPrimingServer().start()
    try:
        status, _, _ = await _navigate(page, server.url, min(timeout_ms, 5_000))
        if status != 200:
            raise RuntimeError(f"localhost priming fallito: HTTP {status}.")
    finally:
        server.close()


async def _measure_http3(playwright: Any, url: str, timeout_ms: int) -> Measurement:
    origin = await _ensure_http3_profile(playwright, url, timeout_ms)
    run_profile = _copy_prepared_profile(origin.directory)
    context = None
    try:
        context = await playwright.firefox.launch_persistent_context(
            user_data_dir=str(run_profile),
            headless=True,
            ignore_https_errors=True,
            firefox_user_prefs=build_user_prefs(Protocol.HTTP3),
        )
        page = await context.new_page()
        tracker = MainFrameNavigationTracker(page)
        await _prime_datastorage(page, timeout_ms)

        try:
            snapshot = await _navigate_and_read_final_document(
                page, tracker, url, timeout_ms
            )
        except NavigationTimingError as exc:
            return _failed_with_response(str(exc), 0)
    finally:
        if context is not None:
            await context.close()
        _remove_tree(run_profile)

    if snapshot.status == 0:
        return _failed("Nessuna risposta ricevuta dal server.")
    if snapshot.entry is None:
        return _failed("Navigation Timing non disponibile per il documento.")
    return _to_measurement(
        Protocol.HTTP3,
        snapshot.status,
        snapshot.entry,
        snapshot.timing,
        snapshot.byte_size_hint,
    )


def _to_measurement(
    requested: Protocol,
    status: int,
    entry: dict[str, Any] | None,
    timing: dict[str, Any],
    byte_size_hint: float | None = None,
) -> Measurement:
    """Converte i dati di Navigation/Resource Timing nel modello di dominio."""
    raw_protocol = str((entry or {}).get("nextHopProtocol") or "")
    negotiated = _VALID_NEGOTIATED_PROTOCOLS.get(raw_protocol)
    got_response = status > 0 and entry is not None
    protocol_ok = got_response and negotiated is requested
    succeeded = protocol_ok and is_http_success(status)

    if not got_response:
        error = "Nessuna risposta utilizzabile ricevuta dal server."
    elif negotiated is None:
        error = f"Protocollo negoziato non valido: ottenuto {raw_protocol or 'sconosciuto'}."
        logger.warning(
            "Misura scartata, protocollo negoziato non valido (Firefox, protocol=%s)",
            raw_protocol or "sconosciuto",
        )
    elif negotiated is not requested:
        error = (
            f"Protocollo negoziato diverso da quello richiesto: richiesto "
            f"{requested.value}, ottenuto {negotiated.value}."
        )
        logger.warning(
            "Misura scartata, protocollo negoziato diverso dal richiesto "
            "(Firefox, richiesto %s, ottenuto %s)",
            requested.value,
            negotiated.value,
        )
    elif not succeeded:
        error = f"Il server ha risposto con un errore: HTTP {status}."
        logger.warning(
            "Misura scartata, risposta HTTP non di successo (Firefox, protocollo %s, codice %d)",
            negotiated.value,
            status,
        )
    else:
        error = None

    if not succeeded:
        return Measurement(
            succeeded=False,
            actual_proto=None,
            total_ms=0.0,
            ttfb_ms=0.0,
            kb=0.0,
            response_code=status,
            error=error,
        )

    ttfb_ms = float((entry or {}).get("responseStart") or timing.get("responseStart", 0.0) or 0.0)
    total_ms = float((entry or {}).get("responseEnd") or timing.get("responseEnd", 0.0) or 0.0)
    transfer_size = float((entry or {}).get("transferSize", 0.0) or 0.0)
    encoded_body_size = float((entry or {}).get("encodedBodySize", 0.0) or 0.0)
    measured_bytes = transfer_size or encoded_body_size or float(byte_size_hint or 0.0)
    return Measurement(
        succeeded=True,
        actual_proto=negotiated,
        total_ms=max(total_ms, 0.0),
        ttfb_ms=max(ttfb_ms, 0.0),
        kb=measured_bytes / 1024,
        response_code=status,
        error=None,
    )


async def measure(url: str, protocol: Protocol, timeout_ms: int) -> Measurement:
    """Esegue una singola misurazione con Firefox headless.

    Riceve:
        url: l'URL da richiedere.
        protocol: il protocollo richiesto.
        timeout_ms: timeout di navigazione in millisecondi, dalla ``Session``.

    Restituisce:
        Un ``Measurement``: mai un'eccezione per un fallimento di rete o di
        avvio del browser, così che il chiamante possa registrare il risultato
        come ``failed``.

    Fa:
        Per HTTP/2 avvia un browser effimero con HTTP/3 disabilitato e naviga
        una sola volta. Per HTTP/3 garantisce prima un profilo persistente per
        la origin, contenente l'``Alt-Svc`` appreso fuori misura; la misura
        reale usa poi una copia temporanea di quel profilo, avvia un nuovo
        processo Firefox, inizializza DataStorage con una richiesta locale a
        ``127.0.0.1`` e solo dopo richiede il target. Il priming locale e il
        precondizionamento non entrano nei tempi o nei byte restituiti.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        return _failed(
            f"Playwright non installato ({exc}). "
            "Eseguire: pip install -r requirements.txt && playwright install firefox"
        )

    try:
        async with async_playwright() as playwright:
            if protocol is Protocol.HTTP2:
                return await _measure_http2(playwright, url, timeout_ms)
            return await _measure_http3(playwright, url, timeout_ms)
    except Exception as exc:  # noqa: BLE001 - avvio browser, timeout, filesystem, ...
        logger.warning("Misura Firefox fallita: %s", exc)
        return _failed(f"Firefox non utilizzabile: {exc}")
