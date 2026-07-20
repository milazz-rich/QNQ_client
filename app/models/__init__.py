"""Barrel dei modelli Pydantic: punto unico di import per routers e services."""

from app.models.client import Client, ClientBase, ClientCreate, ClientUpdate
from app.models.common import (
    ErrorDetail,
    ErrorResponse,
    MongoDocument,
    MongoId,
    MongoModel,
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
    ConnectionMode,
    SessionItem,
    SessionItemBase,
    SessionItemBatchResult,
    SessionItemCreate,
    SessionItemUpdate,
)
from app.models.target import Protocol, Target, TargetBase, TargetCreate, TargetStatus, TargetUpdate

__all__ = [
    # common
    "ErrorDetail",
    "ErrorResponse",
    "MongoDocument",
    "MongoId",
    "MongoModel",
    "to_object_id",
    # target
    "Protocol",
    "Target",
    "TargetBase",
    "TargetCreate",
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
    "ConnectionMode",
    "SessionItem",
    "SessionItemBase",
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
