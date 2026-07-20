"""Router HTTP, uno per entità. Registrati in ``app.main``."""

from app.routers import clients, health, scenarios, session_items, targets

__all__ = ["clients", "health", "scenarios", "session_items", "targets"]
