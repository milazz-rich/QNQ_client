"""Configurazione applicativa letta da variabili d'ambiente / file ``.env``."""

from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Impostazioni dell'applicazione.

    Nessun valore sensibile o dipendente dall'ambiente deve essere hardcoded nel
    codice: tutto passa da qui. In particolare ``MONGO_HOST`` è il gateway della
    rete WSL verso l'host Windows e cambia fra un riavvio e l'altro; va aggiornato
    nel file ``.env`` (recuperabile con ``ip route show | grep default`` da WSL).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = Field(default="QNQ Benchmark API")
    app_env: str = Field(default="development")
    api_prefix: str = Field(default="/api")

    mongo_host: str = Field(default="172.17.32.1")
    mongo_port: int = Field(default=27017, ge=1, le=65535)
    mongo_db: str = Field(default="qnq")
    mongo_uri: str = Field(default="")
    mongo_timeout_ms: int = Field(default=5000, ge=100)

    curl_binary_path: str = Field(default="~/curl/src/curl")
    curl_kill_grace_ms: int = Field(default=2000, ge=0)
    curl_ca_bundle_path: str = Field(default="")

    # ``NoDecode`` per la stessa ragione di ``cors_origins`` (vedi sotto): senza,
    # pydantic-settings tenterebbe di interpretare il valore come JSON.
    # È una lista fin da ora — un solo hash oggi, ma ambienti diversi (Docker,
    # KVM) useranno certificati diversi e Chrome accetta nativamente più hash
    # separati da virgola nel flag --ignore-certificate-errors-spki-list.
    chrome_cert_spki_hash: Annotated[list[str], NoDecode] = Field(default_factory=list)
    chrome_wait_until: Literal["load", "commit", "domcontentloaded"] = Field(default="load")

    # ``NoDecode`` disattiva il parsing JSON che pydantic-settings applicherebbe
    # ai campi complessi prima dei validator: senza, ``CORS_ORIGINS=http://a``
    # farebbe fallire l'avvio con un JSONDecodeError.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:4200"]
    )

    @field_validator("chrome_cert_spki_hash", mode="before")
    @classmethod
    def _split_spki_hashes(cls, value: object) -> object:
        """Accetta gli hash SPKI come stringa separata da virgole.

        Riceve:
            value: il valore grezzo letto dall'ambiente (stringa o lista).

        Restituisce:
            Una lista di hash se l'input era una stringa, altrimenti il valore
            invariato.

        Fa:
            Permette di scrivere sia ``CHROME_CERT_SPKI_HASH=abc=`` sia
            ``CHROME_CERT_SPKI_HASH=abc=,def=`` nel ``.env``, senza doverlo
            formattare come JSON. Un valore vuoto produce lista vuota (nessun
            certificato custom fidato).
        """
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        """Accetta le origini CORS anche come stringa separata da virgole.

        Riceve:
            value: il valore grezzo letto dall'ambiente (stringa o lista).

        Restituisce:
            Una lista di origini se l'input era una stringa, altrimenti il valore
            invariato.

        Fa:
            Permette di scrivere ``CORS_ORIGINS=http://a,http://b`` nel ``.env``
            senza doverlo formattare come JSON.
        """
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mongo_dsn(self) -> str:
        """Stringa di connessione MongoDB effettivamente utilizzata.

        Riceve:
            Nulla (proprietà calcolata).

        Restituisce:
            L'URI di connessione a MongoDB.

        Fa:
            Usa ``MONGO_URI`` se valorizzata, altrimenti compone l'URI a partire
            da ``MONGO_HOST`` e ``MONGO_PORT``.
        """
        if self.mongo_uri:
            return self.mongo_uri
        return f"mongodb://{self.mongo_host}:{self.mongo_port}"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def curl_path(self) -> str:
        """Percorso assoluto del binario curl da invocare.

        Riceve:
            Nulla (proprietà calcolata).

        Restituisce:
            Il percorso del binario con la tilde espansa.

        Fa:
            Espande ``~`` in ``CURL_BINARY_PATH``: ``subprocess`` non passa dalla
            shell e non la espanderebbe da solo, quindi il default
            ``~/curl/src/curl`` fallirebbe con "file non trovato".
        """
        return str(Path(self.curl_binary_path).expanduser())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def curl_ca_bundle(self) -> str:
        """Percorso assoluto del certificato CA custom per curl, se configurato.

        Riceve:
            Nulla (proprietà calcolata).

        Restituisce:
            Il percorso con la tilde espansa, oppure stringa vuota se
            ``CURL_CA_BUNDLE_PATH`` non è valorizzata.

        Fa:
            Stessa espansione di ``curl_path``: ``subprocess`` non passa dalla
            shell e non espanderebbe ``~`` da solo. Una stringa vuota resta
            vuota (nessun bundle configurato), non viene espansa in "home
            dell'utente".
        """
        if not self.curl_ca_bundle_path:
            return ""
        return str(Path(self.curl_ca_bundle_path).expanduser())

    @computed_field  # type: ignore[prop-decorator]
    @property
    def chrome_spki_list(self) -> str:
        """Valore del flag ``--ignore-certificate-errors-spki-list`` per Chrome.

        Riceve:
            Nulla (proprietà calcolata).

        Restituisce:
            Gli hash SPKI uniti da virgola, oppure stringa vuota se non è
            configurato alcun certificato custom.

        Fa:
            Chrome accetta più hash separati da virgola in un solo flag, quindi
            la lista di configurazione si mappa direttamente sul formato atteso
            senza ulteriori trasformazioni. Verificato empiricamente che questo
            flag è l'**unico** modo di far accettare un certificato self-signed
            a QUIC: ``--ignore-certificate-errors`` (generico) fa fallire
            l'handshake HTTP/3 con ``ERR_QUIC_PROTOCOL_ERROR``.
        """
        return ",".join(self.chrome_cert_spki_hash)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_development(self) -> bool:
        """Indica se l'applicazione gira in ambiente di sviluppo.

        Riceve:
            Nulla (proprietà calcolata).

        Restituisce:
            ``True`` se ``APP_ENV`` vale ``development``.

        Fa:
            Usata per decidere se esporre dettagli tecnici nelle risposte di errore.
        """
        return self.app_env.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """Restituisce l'istanza singleton delle impostazioni.

    Riceve:
        Nulla.

    Restituisce:
        L'oggetto ``Settings``, costruito una sola volta e memorizzato in cache.

    Fa:
        Evita di rileggere il file ``.env`` ad ogni accesso e permette di usare
        la funzione come dipendenza FastAPI.
    """
    return Settings()


settings = get_settings()
