"""Modelli dell'entità SessionItem: l'unità di lavoro configurata."""

from enum import StrEnum

from pydantic import Field

from app.models.common import MongoDocument, MongoId, MongoModel


class ConnectionMode(StrEnum):
    """Politica di gestione della connessione fra ripetizioni."""

    REUSE = "reuse"
    NEW = "new"


class SessionItemBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un SessionItem."""

    target_id: MongoId = Field(alias="targetId", description="Target da misurare")
    scenario_id: MongoId = Field(alias="scenarioId", description="Scenario da eseguire")
    client_id: MongoId = Field(alias="clientId", description="Client che esegue la misura")
    reps: int = Field(ge=1, description="Numero di ripetizioni della misura")
    conn: ConnectionMode = Field(
        default=ConnectionMode.REUSE,
        description="Se riusare la connessione o aprirne una nuova per ogni ripetizione",
    )
    timeout: int = Field(ge=1, description="Timeout della singola richiesta in millisecondi")


class SessionItemCreate(SessionItemBase):
    """Payload di ``POST /api/session-items``."""


class SessionItemUpdate(MongoModel):
    """Payload di ``PUT /api/session-items/{id}``: ogni campo omesso resta invariato."""

    target_id: MongoId | None = Field(default=None, alias="targetId")
    scenario_id: MongoId | None = Field(default=None, alias="scenarioId")
    client_id: MongoId | None = Field(default=None, alias="clientId")
    reps: int | None = Field(default=None, ge=1)
    conn: ConnectionMode | None = None
    timeout: int | None = Field(default=None, ge=1)


class SessionItem(SessionItemBase, MongoDocument):
    """Rappresentazione completa di un SessionItem restituita dall'API."""


class SessionItemBatchResult(MongoModel):
    """Esito della creazione in batch di più SessionItem."""

    ids: list[MongoId] = Field(
        description="Identificativi creati, nello stesso ordine dei payload in ingresso"
    )
