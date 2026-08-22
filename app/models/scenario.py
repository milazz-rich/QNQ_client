"""Modelli dell'entità Scenario: il percorso da richiedere al target."""

from pydantic import Field

from app.models.common import MongoDocument, MongoModel


class ScenarioBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di uno Scenario."""

    name: str = Field(min_length=1, max_length=120, description="Nome leggibile dello scenario")
    path: str = Field(
        min_length=1,
        max_length=2048,
        pattern=r"^/",
        description="Path della richiesta, deve iniziare con '/'",
    )
    desc: str = Field(default="", max_length=500, description="Descrizione estesa")


class ScenarioCreate(ScenarioBase):
    """Payload di ``POST /api/scenarios``."""


class ScenarioUpdate(MongoModel):
    """Payload di ``PUT /api/scenarios/{id}``: ogni campo omesso resta invariato."""

    name: str | None = Field(default=None, min_length=1, max_length=120)
    path: str | None = Field(default=None, min_length=1, max_length=2048, pattern=r"^/")
    desc: str | None = Field(default=None, max_length=500)


class Scenario(ScenarioBase, MongoDocument):
    """Rappresentazione completa di uno Scenario restituita dall'API."""
