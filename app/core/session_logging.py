"""Cattura su file dei log emessi durante l'esecuzione di una ``Session``.

Il problema: durante una sessione i log utili non arrivano solo dal
``session_runner``, ma anche dai tre motori di misura (`curl_client`,
`chrome_client`, `firefox_client`), che loggano protocolli scartati, fallimenti
di navigazione e problemi di browser. Quei moduli non conoscono — né devono
conoscere — la sessione in corso: passare un ``sessionId`` fino a ogni chiamata
di ``logger.warning`` significherebbe cambiare la firma di mezzo sottosistema
per una preoccupazione che non è la sua.

La soluzione è una ``ContextVar``: ``session_log_context`` la valorizza per la
durata dell'esecuzione, e un ``Handler`` installato sul logger radice la legge a
ogni record per decidere su quale file scrivere. La ``ContextVar`` è ereditata
automaticamente da tutte le coroutine avviate dentro quel contesto, quindi ogni
`logger.*` chiamato lungo la catena `session_runner` → `measurement.runner` →
client viene correlato senza che nessuno di quei moduli sappia di esistere in
una sessione.

**Limite noto**: i record emessi da *thread* separati (l'unico caso attuale è
l'handler del server di priming locale di Firefox, che logga a livello `DEBUG`)
non vedono la ``ContextVar`` — le ``ContextVar`` sono per contesto di
esecuzione, non globali — e restano quindi solo sullo stream. Non è una perdita
significativa: sono tracce di servizio, non esiti di misura, e con il livello
radice a `INFO` non verrebbero comunque emesse.
"""

import logging
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from typing import TextIO

# Stesso formato dello stream handler configurato in ``app.main``: il file di
# sessione deve essere leggibile con le stesse aspettative del log a console,
# non in un dialetto diverso.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"

_REPO_ROOT = Path(__file__).resolve().parents[2]
SESSION_LOG_ROOT = _REPO_ROOT / "logs" / "sessions"

# Il nome del file è derivato dall'id della sessione, che arriva dall'URL: va
# validato prima di comporre un path, altrimenti un id come "../../etc/passwd"
# farebbe scrivere (o leggere) fuori dalla cartella dei log. Gli id sono
# ObjectId MongoDB, quindi 24 caratteri esadecimali e nulla d'altro.
_SESSION_ID_PATTERN = re.compile(r"\A[0-9a-fA-F]{24}\Z")

_current_session_id: ContextVar[str | None] = ContextVar("qnq_session_id", default=None)

_handler: "SessionFileHandler | None" = None
_install_guard = threading.Lock()


def session_log_path(session_id: str) -> Path:
    """Compone il percorso del file di log di una sessione.

    Riceve:
        session_id: identificativo della sessione (24 caratteri esadecimali).

    Restituisce:
        Il ``Path`` del file ``logs/sessions/{sessionId}.log``.

    Fa:
        Solleva ``ValueError`` se l'id non ha la forma di un ObjectId: il nome
        del file deriva direttamente dall'input dell'utente e non deve poter
        contenere separatori di path o riferimenti alla directory superiore.
        Il nome è deliberatamente il solo id, senza timestamp né suffissi, così
        che il file sia raggiungibile conoscendo la sola sessione.
    """
    if not _SESSION_ID_PATTERN.match(session_id):
        raise ValueError(f"Id di sessione non valido per un file di log: {session_id!r}.")
    return SESSION_LOG_ROOT / f"{session_id}.log"


class SessionFileHandler(logging.Handler):
    """Instrada ogni record al file della sessione attiva nel suo contesto.

    Un solo handler serve tutte le sessioni: la destinazione non è fissata alla
    costruzione — come sarebbe in un ``FileHandler`` — ma decisa record per
    record dalla ``ContextVar``. I record emessi fuori da una sessione vengono
    scartati, così l'handler è inerte durante il normale servizio HTTP.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self._streams: dict[str, TextIO] = {}
        self._guard = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        """Scrive un record sul file della sessione corrente, se ce n'è una.

        Riceve:
            record: il record prodotto da un qualunque logger dell'applicazione.

        Restituisce:
            ``None``.

        Fa:
            Ignora silenziosamente i record emessi fuori da una sessione. Il
            flush è immediato dopo ogni riga: il log deve essere leggibile
            **durante** l'esecuzione, non solo alla fine, perché serve a seguire
            una sessione lunga mentre gira. Un errore di scrittura passa da
            ``handleError`` e non risale al codice che ha chiamato ``logger.*``:
            un disco pieno non deve far fallire una misura.
        """
        session_id = _current_session_id.get()
        if session_id is None:
            return
        try:
            line = self.format(record)
            with self._guard:
                stream = self._stream_for(session_id)
                stream.write(line + "\n")
                stream.flush()
        except Exception:  # noqa: BLE001 - contratto di logging.Handler
            self.handleError(record)

    def _stream_for(self, session_id: str) -> TextIO:
        """Restituisce lo stream aperto per una sessione, aprendolo se serve."""
        stream = self._streams.get(session_id)
        if stream is None or stream.closed:
            path = session_log_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            stream = path.open("a", encoding="utf-8")
            self._streams[session_id] = stream
        return stream

    def close_session(self, session_id: str) -> None:
        """Chiude il file di una sessione conclusa.

        Riceve:
            session_id: la sessione di cui rilasciare lo stream.

        Restituisce:
            ``None``.

        Fa:
            Il file resta **leggibile** dopo la chiusura: chiudere lo stream in
            scrittura non cancella nulla, serve solo a non tenere aperto un
            descrittore per ogni sessione mai eseguita dall'avvio del processo.
            È idempotente: una sessione mai aperta non produce errore.
        """
        with self._guard:
            stream = self._streams.pop(session_id, None)
        if stream is not None:
            with suppress(Exception):
                stream.close()

    def close(self) -> None:
        """Chiude tutti gli stream aperti e rimuove l'handler."""
        with self._guard:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            with suppress(Exception):
                stream.close()
        super().close()


def setup_session_logging() -> SessionFileHandler:
    """Installa (una sola volta) l'handler di sessione sul logger radice.

    Riceve:
        Nulla.

    Restituisce:
        L'istanza singleton di ``SessionFileHandler``.

    Fa:
        Aggancia l'handler al logger **radice**, non a quello del
        ``session_runner``: solo così cattura anche i record dei tre client di
        misura, che hanno logger propri (``app.services.measurement.*``) e
        propagano alla radice. Non modifica il livello del logger radice né
        tocca gli handler esistenti: il log a console resta identico, il file è
        una destinazione in più.

        Ne consegue un limite da tenere presente: il file contiene ciò che
        **arriva** alla radice, quindi il livello radice (``INFO``, impostato in
        ``app.main``) resta la soglia effettiva. Abbassarlo a ``DEBUG`` fa
        finire nel file anche il dettaglio di Playwright, non solo quello
        dell'applicazione.
    """
    global _handler
    with _install_guard:
        if _handler is None:
            handler = SessionFileHandler()
            handler.setFormatter(logging.Formatter(LOG_FORMAT))
            logging.getLogger().addHandler(handler)
            _handler = handler
        return _handler


@contextmanager
def session_log_context(session_id: str) -> Iterator[Path | None]:
    """Dirotta su file tutti i log emessi dentro il blocco.

    Riceve:
        session_id: la sessione in esecuzione.

    Restituisce:
        Un context manager che cede il ``Path`` del file di log, o ``None`` se
        l'id non permette di comporne uno.

    Fa:
        Tronca il file all'ingresso: rilanciare la stessa sessione riscrive il
        log da capo, coerentemente con ``session_runner``, che cancella anche i
        ``Result`` della run precedente della stessa sessione (§5.1). Sessioni
        **diverse** non possono mescolarsi, perché il nome del file deriva
        dall'id.

        All'uscita ripristina il valore precedente della ``ContextVar`` e chiude
        lo stream, sia che il blocco sia terminato normalmente sia che sia stato
        interrotto da un'eccezione: una sessione ``failed`` deve lasciare un log
        leggibile esattamente come una ``completed``.

        Un id malformato non fa fallire l'esecuzione: la sessione gira senza
        cattura su file, perché perdere il log è meno grave che non misurare.
    """
    try:
        path = session_log_path(session_id)
    except ValueError:
        logging.getLogger(__name__).warning(
            "Log su file non attivato: id di sessione non valido (%r).", session_id
        )
        yield None
        return

    handler = setup_session_logging()
    handler.close_session(session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with suppress(OSError):
        path.write_text("", encoding="utf-8")

    token = _current_session_id.set(session_id)
    try:
        yield path
    finally:
        _current_session_id.reset(token)
        handler.close_session(session_id)


def delete_session_log(session_id: str) -> bool:
    """Rimuove il file di log di una sessione, se esiste.

    Riceve:
        session_id: la sessione di cui cancellare il log.

    Restituisce:
        ``True`` se un file è stato rimosso, ``False`` se non c'era nulla.

    Fa:
        Chiude prima lo stream eventualmente ancora aperto, così il descrittore
        non sopravvive al file. Serve alla cascata di
        ``sessions_service.delete_session``: senza, il log di una sessione
        cancellata resterebbe su disco per sempre, non più raggiungibile da
        nessun endpoint (l'unico che lo legge richiede che la sessione esista).
        Non solleva mai: la cancellazione della sessione è già avvenuta, e un
        file di log che resiste non deve trasformarsi in un errore per il
        chiamante — viene loggato e basta.
    """
    try:
        path = session_log_path(session_id)
    except ValueError:
        return False

    if _handler is not None:
        _handler.close_session(session_id)

    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Impossibile cancellare il log della sessione %s: %s", session_id, exc
        )
        return False
    return True


def read_session_log(session_id: str) -> str | None:
    """Legge il contenuto del file di log di una sessione.

    Riceve:
        session_id: la sessione di cui leggere il log.

    Restituisce:
        Il contenuto del file, oppure ``None`` se non esiste (sessione mai
        avviata, o log non ancora scritto).

    Fa:
        Restituisce ``None`` invece di sollevare: è il chiamante — il service —
        a decidere che un log assente vale un ``404``, perché è lì che vive la
        semantica applicativa. Legge con ``errors="replace"``: un log troncato
        da un crash a metà di una scrittura resta comunque consultabile.
    """
    path = session_log_path(session_id)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
