"""Modelli dell'entità SessionItem: l'unità di lavoro configurata.

Un ``SessionItem`` descrive **una variante da misurare** all'interno di una
sessione: quale scenario, con quale protocollo, su quale ambiente. *Chi*
misura (il client), *cosa* si misura (il motore) e *quanto* misurarlo (numero
di ripetizioni, timeout) non stanno più qui: sono scelte della ``Session``,
uguali per tutti i suoi item (vedi AGENTS.md §3.3). ``reps`` e ``timeout`` in
particolare sono impostati una volta sola nello step di configurazione del
wizard e non variano fra le combinazioni Scenario × Protocollo × Ambiente
generate dal batch — non avrebbe senso duplicarli su ogni item.

Protocollo e ambiente sono **parametri della misura**, non attributi del
server: lo stesso motore, sullo stesso endpoint, può essere interrogato in
HTTP/2 o HTTP/3 — ed è esattamente il confronto che l'applicazione esiste per
fare.
"""

from datetime import datetime

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


class SessionItemCreate(SessionItemBase):
    """Payload di ``POST /api/session-items``."""


class SessionItemUpdate(MongoModel):
    """Payload di ``PUT /api/session-items/{id}``: ogni campo omesso resta invariato."""

    scenario_id: MongoId | None = Field(default=None, alias="scenarioId")
    protocol: Protocol | None = None
    environment: Environment | None = None


class SessionItem(SessionItemBase, MongoDocument):
    """Rappresentazione completa di un SessionItem restituita dall'API."""


class OrphanedSessionItem(SessionItem):
    """Un ``SessionItem`` non referenziato da nessuna ``Session`` esistente.

    Estende ``SessionItem`` con ``createdAt``, che non è un campo persistito:
    nessuna entità di questo progetto ha un campo "data di creazione" salvato
    esplicitamente. Deriva invece dall'``ObjectId`` stesso — i primi 4 byte
    codificano un timestamp Unix (``ObjectId.generation_time``) — quindi non
    richiede né una migrazione né una scrittura in più a ogni creazione: è
    sempre disponibile e sempre accurato, per qualunque ``SessionItem`` sia
    mai stato creato, compresi quelli esistenti prima di questo endpoint.
    """

    created_at: datetime = Field(
        alias="createdAt",
        description=(
            "Istante di creazione, ricavato dall'ObjectId (nessun campo "
            "persistito): utile per decidere se un item orfano sia un residuo "
            "recente di una sessione appena eliminata o uno dimenticato da tempo"
        ),
    )


class SessionItemBatchCreate(MongoModel):
    """Payload di ``POST /api/session-items/batch``: una specifica, non una lista.

    Il wizard "Nuova sessione" sceglie **un** target e **un** client per la
    sessione, e poi quali scenari, protocolli e ambienti mettere a confronto:
    il prodotto cartesiano lo costruisce il backend (vedi
    ``session_items_service.create_session_items_batch``).

    Target e client non compaiono qui proprio perché non sono più dell'item:
    vengono impostati sulla ``Session`` che raccoglierà questi item. Lo stesso
    vale per ``reps`` e ``timeout``: sono uguali per l'intera batch, quindi
    vivono sulla ``Session`` e non su ogni combinazione generata qui.
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


class SessionItemBatchResult(MongoModel):
    """Esito della creazione in batch di più SessionItem."""

    ids: list[MongoId] = Field(
        description=(
            "Identificativi creati, in ordine deterministico: scenario esterno, "
            "poi protocollo, poi ambiente"
        )
    )


class OrphanedSessionItemsDeleteResult(MongoModel):
    """Esito di ``DELETE /api/session-items/orphaned``."""

    count: int = Field(ge=0, description="Numero di SessionItem orfani cancellati")
    ids: list[MongoId] = Field(
        description="Identificativi effettivamente cancellati"
    )
