"""Configurazione applicativa letta da variabili d'ambiente / file ``.env``."""

from functools import lru_cache
from typing import Annotated

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

    # ``NoDecode`` disattiva il parsing JSON che pydantic-settings applicherebbe
    # ai campi complessi prima dei validator: senza, ``CORS_ORIGINS=http://a``
    # farebbe fallire l'avvio con un JSONDecodeError.
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:4200"]
    )

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
