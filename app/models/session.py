"""Modelli dell'entità Session: l'esecuzione ordinata di più SessionItem."""

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from app.models.common import MongoDocument, MongoId, MongoModel, Protocol


class RunStatus(StrEnum):
    """Stato di avanzamento di una sessione o di un suo item.

    Su ``SessionProgressItem.status``, ``FAILED`` indica un item mai misurato
    (configurazione rotta, client non supportato). Su ``Session.status``,
    ``FAILED`` è lo stato finale se **almeno un item** della sessione è
    terminato ``failed``; ``COMPLETED`` solo se lo sono stati tutti (§5.1 di
    AGENTS.md).
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class SessionProgressItem(MongoModel):
    """Avanzamento di un singolo item all'interno di una sessione.

    Documento embedded nel documento ``sessions``: non ha una collezione propria
    e quindi nemmeno un ``id``; punta al ``SessionItem`` di configurazione.
    """

    session_item_id: MongoId = Field(
        alias="sessionItemId", description="SessionItem di configurazione"
    )
    label: str = Field(min_length=1, max_length=200, description="Etichetta leggibile")
    proto: Protocol = Field(description="Protocollo richiesto per questo item")
    total: int = Field(ge=0, description="Ripetizioni previste")
    done: int = Field(default=0, ge=0, description="Ripetizioni completate")
    status: RunStatus = Field(default=RunStatus.PENDING, description="Stato dell'item")

    @model_validator(mode="after")
    def _done_within_total(self) -> "SessionProgressItem":
        """Verifica che le ripetizioni completate non superino quelle previste.

        Riceve:
            Nulla (validatore di istanza).

        Restituisce:
            L'istanza validata.

        Fa:
            Solleva ``ValueError`` se ``done > total``, condizione che indica un
            bug nel session runner.
        """
        if self.done > self.total:
            raise ValueError("'done' non può superare 'total'.")
        return self


class SessionBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di una Session.

    Una sessione è **un motore misurato da un client**, declinato su più
    scenari, protocolli e ambienti: ``targetId`` e ``clientId`` sono quindi
    suoi, non dei singoli ``SessionItem``, che li ereditano tutti uguali.
    Anche ``reps`` e ``timeout`` sono qui e non sul ``SessionItem``, per lo
    stesso motivo: sono impostati una volta sola nello step di configurazione
    del wizard e restano gli stessi per ogni combinazione Scenario × Protocollo
    × Ambiente generata dal batch.
    """

    name: str = Field(min_length=1, max_length=120, description="Nome leggibile della sessione")
    target_id: MongoId = Field(
        alias="targetId", description="Motore sotto test, uguale per tutti gli item"
    )
    client_id: MongoId = Field(
        alias="clientId", description="Client che esegue le misure, uguale per tutti gli item"
    )
    reps: int = Field(ge=1, description="Ripetizioni per item, uguali per tutta la sessione")
    timeout: int = Field(
        ge=1, description="Timeout della singola richiesta in millisecondi, uguale per tutta la sessione"
    )
    when: datetime = Field(description="Istante di pianificazione o avvio, in UTC")
    status: RunStatus = Field(default=RunStatus.PENDING, description="Stato della sessione")
    current_index: int = Field(
        default=0, alias="currentIndex", ge=0, description="Indice dell'item in esecuzione"
    )
    items: list[SessionProgressItem] = Field(
        default_factory=list, description="Avanzamento degli item, in ordine di esecuzione"
    )


class SessionCreate(SessionBase):
    """Payload di ``POST /api/sessions``."""


class SessionUpdate(MongoModel):
    """Payload di ``PUT /api/sessions/{id}``: ogni campo omesso resta invariato."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    target_id: MongoId | None = Field(default=None, alias="targetId")
    client_id: MongoId | None = Field(default=None, alias="clientId")
    reps: int | None = Field(default=None, ge=1)
    timeout: int | None = Field(default=None, ge=1)
    when: datetime | None = None
    status: RunStatus | None = None
    current_index: int | None = Field(default=None, alias="currentIndex", ge=0)
    items: list[SessionProgressItem] | None = None


class Session(SessionBase, MongoDocument):
    """Rappresentazione completa di una Session restituita dall'API."""
