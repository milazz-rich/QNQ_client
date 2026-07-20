"""Accesso a MongoDB: connessione, handle del database, nomi delle collezioni."""

from app.db.mongo import (
    close_mongo_connection,
    connect_to_mongo,
    get_collection,
    get_database,
    ping,
)

__all__ = [
    "close_mongo_connection",
    "connect_to_mongo",
    "get_collection",
    "get_database",
    "ping",
]
