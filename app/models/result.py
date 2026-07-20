"""Modelli dell'entità Result: l'esito di una singola ripetizione di misura."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from app.models.common import MongoDocument, MongoId, MongoModel
from app.models.target import Protocol


class ResultStatus(StrEnum):
    """Esito di una misura."""

    COMPLETED = "completed"
    FAILED = "failed"


class ResultBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un Result.

    ``target`` e ``scenario_path`` sono denormalizzati di proposito: un risultato
    deve restare leggibile anche se il target o lo scenario vengono modificati o
    eliminati dopo la misura.
    """

    session_item_id: MongoId = Field(
        alias="sessionItemId", description="SessionItem che ha prodotto il risultato"
    )
    idx: int = Field(ge=0, description="Indice della ripetizione, a partire da 0")
    target: str = Field(min_length=1, max_length=300, description="Snapshot leggibile del target")
    scenario_path: str = Field(
        alias="scenarioPath", max_length=2048, description="Snapshot del path richiesto"
    )
    proto: Protocol = Field(description="Protocollo richiesto")
    actual_proto: Protocol | None = Field(
        default=None,
        alias="actualProto",
        description=(
            "Protocollo effettivamente negoziato: valorizzato solo quando "
            "status='completed', sempre e solo HTTP/2 o HTTP/3. Assente "
            "(null) quando status='failed', anche se curl ha negoziato "
            "qualcos'altro (es. HTTP/1.1) o non ha completato la richiesta."
        ),
    )
    total: float = Field(ge=0, description="Durata totale della richiesta in millisecondi")
    ttfb: float = Field(ge=0, description="Time-to-first-byte in millisecondi")
    kb: float = Field(ge=0, description="Kilobyte trasferiti")
    status: ResultStatus = Field(description="Esito della misura")
    time: datetime = Field(description="Istante di completamento, in UTC")

    @model_validator(mode="after")
    def _actual_proto_matches_status(self) -> "ResultBase":
        """Vincola ``actualProto`` alla coerenza con ``status``.

        Riceve:
            Nulla (validatore di istanza).

        Restituisce:
            L'istanza validata.

        Fa:
            Solleva ``ValueError`` se ``status='completed'`` senza
            ``actualProto`` valorizzato, o se ``status='failed'`` con
            ``actualProto`` valorizzato: i due campi devono essere coerenti,
            non è il chiamante a doverlo garantire caso per caso.
        """
        if self.status is ResultStatus.COMPLETED and self.actual_proto is None:
            raise ValueError("'actualProto' è obbligatorio quando status='completed'.")
        if self.status is ResultStatus.FAILED and self.actual_proto is not None:
            raise ValueError("'actualProto' deve essere assente quando status='failed'.")
        return self


class ResultCreate(ResultBase):
    """Payload di creazione di un Result, prodotto dal servizio di misurazione."""


class Result(ResultBase, MongoDocument):
    """Rappresentazione completa di un Result restituita dall'API."""
