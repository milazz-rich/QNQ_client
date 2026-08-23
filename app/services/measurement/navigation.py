"""Regole di validazione della catena di navigazione main-frame.

Condivise fra ``chrome_client`` e ``firefox_client``: non sono dettagli
implementativi di un motore, ma la **definizione metodologica** di cosa conta
come "documento misurato" quando un contenuto HTTrack fa una navigazione
client-side immediata verso il mirror effettivo (§5.6, §5.7 di AGENTS.md).

Vivono qui, e non duplicate nei due client, proprio perché devono restare
identiche: se Chrome e Firefox applicassero criteri anche solo leggermente
diversi su quali navigazioni interne accettare, misurerebbero documenti diversi
e il confronto fra motori — che è l'unica cosa che questa applicazione deve
garantire — perderebbe significato senza che nulla lo segnali.

I due client conservano invece la propria eccezione di dominio: il messaggio
d'errore finisce in ``Result.error`` e deve dire quale motore ha rifiutato la
navigazione.
"""

from urllib.parse import SplitResult, urlsplit


def split_url(url: str, engine: str) -> SplitResult:
    """Scompone un URL rifiutando gli schemi non misurabili.

    Riceve:
        url: l'URL da scomporre.
        engine: nome del motore di misura, usato solo nel messaggio d'errore.

    Restituisce:
        Il ``SplitResult`` dell'URL.

    Fa:
        Solleva ``ValueError`` per qualunque schema diverso da ``http``/``https``
        o per un URL senza host: un `data:`/`about:` non è un documento
        misurabile, e trattarlo come tale produrrebbe timing privi di senso.
    """
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError(f"URL non valido per la misura {engine}: {url!r}.")
    return parsed


def default_port(scheme: str) -> int:
    """Restituisce la porta implicita di uno schema.

    Riceve:
        scheme: ``http`` o ``https``.

    Restituisce:
        ``443`` per ``https``, ``80`` altrimenti.

    Fa:
        Serve a confrontare due URL di cui uno ha la porta esplicita e l'altro
        no: ``https://host/`` e ``https://host:443/`` sono la stessa origin.
    """
    return 443 if scheme == "https" else 80


def same_origin(first: SplitResult, second: SplitResult) -> bool:
    """Indica se due URL condividono schema, host e porta.

    Riceve:
        first: il primo URL già scomposto.
        second: il secondo URL già scomposto.

    Restituisce:
        ``True`` se le due origin coincidono.

    Fa:
        Normalizza l'host in minuscolo e la porta implicita, così che il
        confronto non dipenda da come l'URL è stato scritto.
    """
    return (
        first.scheme == second.scheme
        and (first.hostname or "").lower() == (second.hostname or "").lower()
        and (first.port or default_port(first.scheme))
        == (second.port or default_port(second.scheme))
    )


def allowed_path_prefix(target_path: str) -> str:
    """Ricava il sottoalbero entro cui una navigazione interna è ammessa.

    Riceve:
        target_path: il path dello scenario richiesto.

    Restituisce:
        Il prefisso di path che le navigazioni successive devono rispettare.

    Fa:
        Per un path che è già una directory (``/content/discord/``) il prefisso
        è il path stesso; per un file (``/content/discord/index.html``) è la
        directory che lo contiene. È ciò che permette la navigazione HTTrack
        ``/content/discord/`` → ``/content/discord/discord.com/index.html``
        senza ammettere un salto a un contenuto diverso.
    """
    if not target_path or target_path == "/":
        return "/"
    if target_path.endswith("/"):
        return target_path
    return target_path.rsplit("/", 1)[0] + "/"


def is_allowed_internal_navigation(target_url: str, candidate_url: str, engine: str) -> bool:
    """Indica se una navigazione main-frame resta dentro il contenuto misurato.

    Riceve:
        target_url: l'URL dello scenario effettivamente richiesto.
        candidate_url: l'URL verso cui il main frame è navigato.
        engine: nome del motore, per il messaggio di ``split_url``.

    Restituisce:
        ``True`` solo se il candidato ha la stessa origin **e** resta sotto il
        prefisso di path dello scenario.

    Fa:
        È il criterio unico con cui entrambi i motori decidono se una
        navigazione automatica è la prosecuzione del documento misurato o una
        fuga verso altro contenuto, che invalida la misura.
    """
    target = split_url(target_url, engine)
    candidate = split_url(candidate_url, engine)
    if not same_origin(target, candidate):
        return False
    return candidate.path.startswith(allowed_path_prefix(target.path))


def find_disallowed_navigation(target_url: str, urls: list[str], engine: str) -> str | None:
    """Cerca la prima navigazione main-frame non ammessa in una catena.

    Riceve:
        target_url: l'URL dello scenario richiesto.
        urls: gli URL main-frame osservati dopo l'inizio della misura.
        engine: nome del motore, per il messaggio di ``split_url``.

    Restituisce:
        L'URL che viola la regola, oppure ``None`` se la catena è tutta interna.

    Fa:
        Restituisce l'URL invece di sollevare, così che ogni client possa
        confezionare la propria eccezione di dominio (il messaggio finisce in
        ``Result.error`` e deve nominare il motore). Un URL non scomponibile
        conta come non ammesso: è comunque una destinazione che non
        rappresenta il documento richiesto.
    """
    for navigated_url in urls:
        try:
            allowed = is_allowed_internal_navigation(target_url, navigated_url, engine)
        except ValueError:
            allowed = False
        if not allowed:
            return navigated_url
    return None
