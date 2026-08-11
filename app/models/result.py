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

    session_id: MongoId = Field(
        alias="sessionId",
        description=(
            "Session che ha effettivamente prodotto la misura. Distinto da "
            "sessionItemId: lo stesso SessionItem può essere condiviso da più "
            "sessioni (rilancio/riproposizione), quindi solo sessionId "
            "identifica senza ambiguità i risultati di una singola esecuzione."
        ),
    )
    session_item_id: MongoId = Field(
        alias="sessionItemId", description="SessionItem che ha prodotto il risultato"
    )
    target_id: MongoId = Field(
        alias="targetId",
        description=(
            "Target su cui è stata eseguita la misura. Riferimento diretto, "
            "distinto dallo snapshot leggibile in 'target': permette di "
            "raggruppare/filtrare i risultati per target anche se il campo "
            "'target' (denormalizzato) resta invariato dopo una rinomina."
        ),
    )
    client_id: MongoId = Field(
        alias="clientId",
        description=(
            "Client (motore di misura) che ha prodotto il risultato. "
            "Riferimento diretto per la stessa ragione di targetId: consente di "
            "confrontare o filtrare le misure per motore (curl vs Chrome) senza "
            "doverlo dedurre risalendo al SessionItem."
        ),
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
    response_code: int | None = Field(
        default=None,
        ge=0,
        alias="responseCode",
        description=(
            "Codice di stato HTTP effettivamente ricevuto (0 se nessuna "
            "risposta è arrivata, es. item mai eseguito o errore di rete). "
            "Sempre visibile, indipendentemente da 'status': un 403 o un 500 "
            "finiscono qui anche quando negoziano correttamente il protocollo "
            "richiesto, perché non è quello a decidere se la misura è valida. "
            "'null' compare solo sui Result creati prima dell'introduzione di "
            "questo campo: il valore storico non è ricostruibile (con la "
            "vecchia logica un 403 poteva essere registrato 'completed' senza "
            "che il codice fosse mai salvato), quindi non si è tentato un "
            "backfill indovinato. Ogni Result creato da qui in avanti lo ha "
            "sempre valorizzato."
        ),
    )
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


class ResultPage(MongoModel):
    """Pagina di risultati restituita da ``GET /api/results``.

    È l'unica rotta di elenco che restituisce un *envelope* invece di un array
    nudo: il conteggio totale non avrebbe altro posto dove stare, ed è
    indispensabile al frontend per costruire i controlli di paginazione senza
    dover interrogare l'API una seconda volta. La forma segue il precedente di
    ``SessionItemBatchResult`` (envelope solo dove serve davvero).
    """

    items: list[Result] = Field(description="I risultati della pagina richiesta")
    total: int = Field(
        ge=0,
        description=(
            "Numero totale di risultati che soddisfano i filtri, "
            "**non** il numero di elementi in questa pagina"
        ),
    )
    page: int = Field(ge=1, description="Pagina restituita, 1-based")
    page_size: int = Field(
        ge=1, alias="pageSize", serialization_alias="pageSize",
        description="Dimensione di pagina effettivamente applicata",
    )
