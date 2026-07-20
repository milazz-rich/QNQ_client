"""Endpoint di health check: verifica la raggiungibilità di MongoDB."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.errors import DatabaseError
from app.db.mongo import ping

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    """Esito del controllo di salute dell'applicazione."""

    status: str = Field(description="'ok' se tutto funziona, 'degraded' altrimenti")
    database: str = Field(description="'connected' oppure 'disconnected'")
    detail: str | None = Field(default=None, description="Dettaglio dell'errore, se presente")


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="Stato dell'applicazione e del database",
)
async def health() -> JSONResponse:
    """Verifica che l'applicazione risponda e che MongoDB sia raggiungibile.

    Riceve:
        Nulla.

    Restituisce:
        ``200`` con ``{"status": "ok", "database": "connected"}`` se il ping al
        database riesce; ``503`` con ``status: "degraded"`` e il dettaglio
        dell'errore se fallisce.

    Fa:
        Esegue un ping reale sul database (non un controllo di configurazione).
        Cattura ``DatabaseError`` invece di propagarla, perché un health check
        non deve mai restituire un errore non gestito agli health probe.
    """
    try:
        await ping()
    except DatabaseError as exc:
        return JSONResponse(
            status_code=503,
            content=HealthResponse(
                status="degraded", database="disconnected", detail=exc.message
            ).model_dump(),
        )
    return JSONResponse(
        status_code=200,
        content=HealthResponse(status="ok", database="connected").model_dump(),
    )
