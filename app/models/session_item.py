"""Modelli dell'entità SessionItem: l'unità di lavoro configurata.

Un ``SessionItem`` descrive **una variante da misurare** all'interno di una
sessione: quale scenario, con quale protocollo, su quale ambiente. *Chi*
misura (il client) e *cosa* si misura (il motore) non stanno più qui: sono
scelte della ``Session``, uguali per tutti i suoi item (vedi AGENTS.md §3.3).

Protocollo e ambiente sono **parametri della misura**, non attributi del
server: lo stesso motore, sullo stesso endpoint, può essere interrogato in
HTTP/2 o HTTP/3 — ed è esattamente il confronto che l'applicazione esiste per
fare.
"""

from pydantic import Field

from app.models.common import Environment, MongoDocument, MongoId, MongoModel, Protocol


class SessionItemBase(MongoModel):
    """Campi comuni a creazione e rappresentazione di un SessionItem."""

    scenario_id: MongoId = Field(alias="scenarioId", description="Scenario da eseguire")
    protocol: Protocol = Field(description="Protocollo con cui interrogare il target")
    environment: Environment = Field(
        description=(
            "Ambiente da interrogare: seleziona l'endpoint del Target della "
            "sessione (``target.endpoints[environment]``)"
        )
    )
    reps: int = Field(ge=1, description="Numero di ripetizioni della misura")
    timeout: int = Field(ge=1, description="Timeout della singola richiesta in millisecondi")


class SessionItemCreate(SessionItemBase):
    """Payload di ``POST /api/session-items``."""


class SessionItemUpdate(MongoModel):
    """Payload di ``PUT /api/session-items/{id}``: ogni campo omesso resta invariato."""

    scenario_id: MongoId | None = Field(default=None, alias="scenarioId")
    protocol: Protocol | None = None
    environment: Environment | None = None
    reps: int | None = Field(default=None, ge=1)
    timeout: int | None = Field(default=None, ge=1)


class SessionItem(SessionItemBase, MongoDocument):
    """Rappresentazione completa di un SessionItem restituita dall'API."""


class SessionItemBatchCreate(MongoModel):
    """Payload di ``POST /api/session-items/batch``: una specifica, non una lista.

    Il wizard "Nuova sessione" sceglie **un** target e **un** client per la
    sessione, e poi quali scenari, protocolli e ambienti mettere a confronto:
    il prodotto cartesiano lo costruisce il backend (vedi
    ``session_items_service.create_session_items_batch``).

    Target e client non compaiono qui proprio perché non sono più dell'item:
    vengono impostati sulla ``Session`` che raccoglierà questi item.
    """

    scenario_ids: list[MongoId] = Field(
        min_length=1, alias="scenarioIds", description="Scenari da includere nel prodotto"
    )
    protocols: list[Protocol] = Field(
        min_length=1, description="Protocolli da testare su ogni combinazione"
    )
    environments: list[Environment] = Field(
        min_length=1, description="Ambienti da testare su ogni combinazione"
    )
    reps: int = Field(ge=1, description="Ripetizioni, uguali per ogni item generato")
    timeout: int = Field(ge=1, description="Timeout in ms, uguale per ogni item generato")


class SessionItemBatchResult(MongoModel):
    """Esito della creazione in batch di più SessionItem."""

    ids: list[MongoId] = Field(
        description=(
            "Identificativi creati, in ordine deterministico: scenario esterno, "
            "poi protocollo, poi ambiente"
        )
    )
