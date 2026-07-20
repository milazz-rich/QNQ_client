"""Router HTTP, uno per entità. Registrati in ``app.main``."""

from app.routers import (
    clients,
    health,
    results,
    scenarios,
    session_items,
    sessions,
    targets,
)

__all__ = [
    "clients",
    "health",
    "results",
    "scenarios",
    "session_items",
    "sessions",
    "targets",
]
