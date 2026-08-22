"""Barrel dei modelli Pydantic: punto unico di import per routers e services."""

from app.models.client import Client, ClientBase, ClientCreate, ClientUpdate
from app.models.common import (
    Environment,
    ErrorDetail,
    ErrorResponse,
    MongoDocument,
    MongoId,
    MongoModel,
    Protocol,
    to_object_id,
)
from app.models.result import Result, ResultBase, ResultCreate, ResultStatus
from app.models.scenario import Scenario, ScenarioBase, ScenarioCreate, ScenarioUpdate
from app.models.session import (
    RunStatus,
    Session,
    SessionBase,
    SessionCreate,
    SessionProgressItem,
    SessionUpdate,
)
from app.models.session_item import (
    SessionItem,
    SessionItemBase,
    SessionItemBatchCreate,
    SessionItemBatchResult,
    SessionItemCreate,
    SessionItemUpdate,
)
from app.models.target import (
    Target,
    TargetBase,
    TargetCreate,
    TargetEndpoint,
    TargetStatus,
    TargetUpdate,
)

__all__ = [
    # common
    "ErrorDetail",
    "ErrorResponse",
    "MongoDocument",
    "MongoId",
    "Environment",
    "MongoModel",
    "Protocol",
    "to_object_id",
    # target
    "Target",
    "TargetBase",
    "TargetCreate",
    "TargetEndpoint",
    "TargetStatus",
    "TargetUpdate",
    # scenario
    "Scenario",
    "ScenarioBase",
    "ScenarioCreate",
    "ScenarioUpdate",
    # client
    "Client",
    "ClientBase",
    "ClientCreate",
    "ClientUpdate",
    # session item
    "SessionItem",
    "SessionItemBase",
    "SessionItemBatchCreate",
    "SessionItemBatchResult",
    "SessionItemCreate",
    "SessionItemUpdate",
    # session
    "RunStatus",
    "Session",
    "SessionBase",
    "SessionCreate",
    "SessionProgressItem",
    "SessionUpdate",
    # result
    "Result",
    "ResultBase",
    "ResultCreate",
    "ResultStatus",
]
