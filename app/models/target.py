"""Modelli dell'entità Target: il server sotto test."""

from enum import StrEnum

from pydantic import Field

from app.models.common import MongoDocument, MongoModel


class Protocol(StrEnum):
    """Protocollo applicativo usato per la misura."""

    HTTP2 = "HTTP/2"
    HTTP3 = "HTTP/3"


class TargetStatus(StrEnum):
    """Stato di disponibilità di un target."""

    ONLINE = "online"
    IDLE = "idle"
    OFFLINE = "offline"


class TargetBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un Target."""

    name: str = Field(min_length=1, max_length=120, description="Nome leggibile del target")
    host: str = Field(
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:\-\[\]]+$",
        description="Hostname o indirizzo IP, senza schema e senza porta",
    )
    port: int = Field(ge=1, le=65535, description="Porta TCP/UDP del servizio")
    protocol: Protocol = Field(description="Protocollo esposto dal target")
    maxc: int = Field(ge=1, description="Numero massimo di connessioni concorrenti")
    status: TargetStatus = Field(
        default=TargetStatus.OFFLINE, description="Stato di disponibilità"
    )
    latency: float = Field(default=0.0, ge=0, description="Latenza rilevata in millisecondi")


class TargetCreate(TargetBase):
    """Payload di ``POST /api/targets``. L'``id`` è generato da MongoDB."""


class TargetUpdate(MongoModel):
    """Payload di ``PUT /api/targets/{id}``: ogni campo omesso resta invariato."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    host: str | None = Field(
        default=None, min_length=1, max_length=255, pattern=r"^[A-Za-z0-9._:\-\[\]]+$"
    )
    port: int | None = Field(default=None, ge=1, le=65535)
    protocol: Protocol | None = None
    maxc: int | None = Field(default=None, ge=1)
    status: TargetStatus | None = None
    latency: float | None = Field(default=None, ge=0)


class Target(TargetBase, MongoDocument):
    """Rappresentazione completa di un Target restituita dall'API."""
