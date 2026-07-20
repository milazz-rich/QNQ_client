"""Modelli dell'entità Client: l'agente che esegue le misure."""

from pydantic import Field

from app.models.common import MongoDocument, MongoModel


class ClientBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un Client."""

    name: str = Field(min_length=1, max_length=120, description="Nome leggibile del client")


class ClientCreate(ClientBase):
    """Payload di ``POST /api/clients``."""


class ClientUpdate(MongoModel):
    """Payload di ``PUT /api/clients/{id}``: ogni campo omesso resta invariato."""

    name: str | None = Field(default=None, min_length=1, max_length=120)


class Client(ClientBase, MongoDocument):
    """Rappresentazione completa di un Client restituita dall'API."""
