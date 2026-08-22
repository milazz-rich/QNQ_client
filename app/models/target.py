"""Modelli dell'entità Target: il motore web sotto test.

Un ``Target`` è un **motore** (Caddy, nginx, OpenLiteSpeed), non un singolo
endpoint di rete: lo stesso motore è deployato in più ambienti, e i suoi
indirizzi vivono in ``endpoints``, indicizzati per ``Environment``. Né il
protocollo né l'ambiente sono attributi del server — sono parametri della
misura, e stanno su ``SessionItem`` (vedi AGENTS.md §3.3).
"""

from enum import StrEnum

from pydantic import Field

from app.models.common import Environment, MongoDocument, MongoModel


class TargetStatus(StrEnum):
    """Stato di disponibilità di un endpoint."""

    ONLINE = "online"
    IDLE = "idle"
    OFFLINE = "offline"


class TargetEndpoint(MongoModel):
    """Indirizzo del motore in uno specifico ambiente.

    Lo stato è **per endpoint**, non per motore: la stessa build può essere
    raggiungibile in Docker e ferma in KVM, e tenerne un solo stato
    complessivo perderebbe l'informazione.
    """

    host: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:\-\[\]]+$",
        description="Hostname o indirizzo IP, senza schema e senza porta",
    )
    port: int = Field(ge=1, le=65535, description="Porta TCP/UDP del servizio")
    status: TargetStatus = Field(
        default=TargetStatus.OFFLINE, description="Stato di disponibilità di questo endpoint"
    )


class TargetBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un Target."""

    name: str = Field(
        min_length=1, max_length=120, description="Nome del motore (es. 'nginx')"
    )
    endpoints: dict[Environment, TargetEndpoint] = Field(
        description=(
            "Indirizzo del motore per ciascun ambiente. Le chiavi sono i valori "
            "di Environment ('docker', 'kvm'): un dizionario a chiavi chiuse, "
            "non una lista, perché la risoluzione in fase di misura è un "
            "accesso diretto per ambiente — vedi measurement.runner"
        )
    )


class TargetCreate(TargetBase):
    """Payload di ``POST /api/targets``. L'``id`` è generato da MongoDB."""


class TargetUpdate(MongoModel):
    """Payload di ``PUT /api/targets/{id}``: ogni campo omesso resta invariato.

    ``endpoints`` si aggiorna in blocco: inviarlo sostituisce l'intera mappa,
    non fa merge per ambiente. È deliberato — un merge parziale renderebbe
    impossibile *rimuovere* un ambiente, e la mappa ha al più due voci.
    """

    name: str | None = Field(default=None, min_length=1, max_length=120)
    endpoints: dict[Environment, TargetEndpoint] | None = None


class Target(TargetBase, MongoDocument):
    """Rappresentazione completa di un Target restituita dall'API."""
