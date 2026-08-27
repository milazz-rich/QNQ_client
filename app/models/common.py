"""Tipi e classi base condivisi da tutti i modelli Pydantic."""

from enum import StrEnum
from typing import Annotated, Any

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.core.errors import ValidationError


class Protocol(StrEnum):
    """Protocollo applicativo sotto confronto.

    Vive qui e non in un modulo di entità perché è **trasversale**: è un
    parametro della misura (``SessionItem.protocol``), un dato dell'esito
    (``Result.proto`` / ``Result.actualProto``), un campo di avanzamento
    (``SessionProgressItem.proto``) e il tipo su cui è parametrizzato ogni
    motore di misura. Stava in ``target.py`` finché il protocollo era un
    attributo del server sotto test; non lo è più (vedi AGENTS.md §3.3).
    """

    HTTP2 = "HTTP/2"
    HTTP3 = "HTTP/3"


class Environment(StrEnum):
    """Ambiente di deploy su cui gira l'istanza del server sotto test.

    Trasversale come ``Protocol`` e per la stessa ragione: è un parametro della
    misura (``SessionItem.environment``), un dato dell'esito
    (``Result.environment``) **e** la chiave con cui si sceglie l'endpoint da
    interrogare (``Target.endpoints[environment]``, vedi AGENTS.md §3.3).

    I valori sono chiusi di proposito: il confronto containerizzato vs
    virtualizzato nativo è una dimensione fissa dell'esperimento, non
    un'etichetta libera. Prima era codificata nel campo ``tag`` del ``Target``,
    con tutti i limiti di una stringa arbitraria (refusi, valori incoerenti fra
    le righe duplicate dello stesso server).
    """

    DOCKER = "docker"
    KVM = "kvm"


def _to_object_id_str(value: Any) -> Any:
    """Normalizza un identificativo Mongo in stringa.

    Riceve:
        value: un ``ObjectId`` o una stringa.

    Restituisce:
        La rappresentazione esadecimale a 24 caratteri come ``str``.

    Fa:
        Converte gli ``ObjectId`` letti dal database; lascia passare invariati
        gli altri valori, che verranno poi validati dal pattern del campo.
    """
    if isinstance(value, ObjectId):
        return str(value)
    return value


MongoId = Annotated[
    str,
    BeforeValidator(_to_object_id_str),
    Field(pattern=r"^[0-9a-fA-F]{24}$", description="Identificativo MongoDB (24 hex)"),
]
"""Identificativo Mongo esposto come stringa esadecimale a 24 caratteri."""


class MongoModel(BaseModel):
    """Base di tutti i modelli dell'API.

    Imposta le convenzioni comuni: popolamento per nome oltre che per alias
    (necessario per mappare ``_id`` su ``id``), strip automatico degli spazi
    nelle stringhe e rifiuto dei campi non previsti.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        str_strip_whitespace=True,
        extra="forbid",
    )


class MongoDocument(MongoModel):
    """Base dei modelli che rappresentano un documento persistito.

    Il campo ``id`` è letto dal ``_id`` di Mongo ma serializzato come ``id``:
    ``alias="_id"`` governa la validazione (i documenti arrivano dal driver con
    ``_id``), ``serialization_alias="id"`` governa la risposta HTTP. Senza il
    secondo, FastAPI — che serializza con ``by_alias=True`` — restituirebbe
    ``_id`` al client.
    """

    id: MongoId = Field(
        alias="_id",
        serialization_alias="id",
        description="Identificativo del documento",
    )


class ErrorDetail(BaseModel):
    """Corpo di un errore restituito dall'API."""

    code: str = Field(description="Codice stabile dell'errore, in SCREAMING_SNAKE_CASE")
    message: str = Field(description="Messaggio leggibile")
    details: Any = Field(default=None, description="Informazioni aggiuntive opzionali")


class ErrorResponse(BaseModel):
    """Forma comune di tutte le risposte di errore.

    Documentata nello schema OpenAPI per ogni rotta che può fallire.
    """

    error: ErrorDetail


def to_object_id(value: str, what: str = "Identificativo") -> ObjectId:
    """Converte una stringa nel corrispondente ``ObjectId``.

    Riceve:
        value: la stringa da convertire.
        what: etichetta usata nel messaggio d'errore (default ``"Identificativo"``).

    Restituisce:
        L'``ObjectId`` corrispondente.

    Fa:
        Solleva ``ValidationError`` applicativa (HTTP 422) se la stringa non è un
        ObjectId valido, invece di lasciar propagare ``InvalidId`` come 500.
    """
    try:
        return ObjectId(value)
    except (InvalidId, TypeError) as exc:
        raise ValidationError(f"{what} '{value}' non è un ObjectId valido.") from exc
