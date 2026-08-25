"""Test dei SessionItem orfani (§5.5): identificazione e pulizia esplicita.

Verificati end-to-end con un server e un database reali in una sessione
precedente (due Session che condividono lo stesso item, cancellate una alla
volta, `GET`/`DELETE /session-items/orphaned`). Questi test coprono la logica
di calcolo isolata, con un finto motor collection senza bisogno di MongoDB.
"""

import unittest
from datetime import UTC, datetime
from unittest.mock import patch

from bson import ObjectId

from app.core.errors import ConflictError, NotFoundError
from app.models.session_item import OrphanedSessionItem

ITEM_A = "a" * 24
ITEM_B = "b" * 24
ITEM_C = "c" * 24


def _item_doc(item_id: str, protocol: str = "HTTP/2", environment: str = "docker") -> dict:
    return {
        "_id": ObjectId(item_id),
        "scenarioId": "1" * 24,
        "protocol": protocol,
        "environment": environment,
    }


def _session_doc(session_id: str, item_ids: list[str]) -> dict:
    return {
        "_id": ObjectId(session_id),
        "items": [{"sessionItemId": item_id} for item_id in item_ids],
    }


class FakeSessionItemsCollection:
    """Sostituto minimale della collezione motor ``session_items``."""

    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    def find(self, query: dict):
        excluded = set(query.get("_id", {}).get("$nin", []))
        matched = [doc for doc in self.documents if doc["_id"] not in excluded]
        return _Cursor(matched)

    async def find_one(self, query: dict) -> dict | None:
        for doc in self.documents:
            if doc["_id"] == query.get("_id"):
                return doc
        return None

    async def delete_one(self, query: dict):
        before = len(self.documents)
        self.documents = [doc for doc in self.documents if doc["_id"] != query.get("_id")]
        deleted = before - len(self.documents)
        return type("Result", (), {"deleted_count": deleted})()


class _Cursor:
    def __init__(self, documents: list[dict]) -> None:
        self._documents = documents

    async def to_list(self, length: int | None = None) -> list[dict]:
        return self._documents


class FakeSessionsCollection:
    """Sostituto minimale della collezione motor ``sessions``."""

    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    async def distinct(self, field: str) -> list[str]:
        assert field == "items.sessionItemId"
        ids: set[str] = set()
        for doc in self.documents:
            for item in doc.get("items", []):
                ids.add(item["sessionItemId"])
        return list(ids)

    async def find_one(self, query: dict) -> dict | None:
        wanted = query["items"]["$elemMatch"]["sessionItemId"]
        for doc in self.documents:
            if any(item["sessionItemId"] == wanted for item in doc.get("items", [])):
                return doc
        return None


def _patch_collections(items_docs: list[dict], session_docs: list[dict]):
    """Instrada ``get_collection`` sulla collezione finta giusta, per nome."""
    from app.db.collections import SESSION_ITEMS, SESSIONS

    items_collection = FakeSessionItemsCollection(items_docs)
    sessions_collection = FakeSessionsCollection(session_docs)

    def fake_get_collection(name: str):
        if name == SESSION_ITEMS:
            return items_collection
        if name == SESSIONS:
            return sessions_collection
        raise AssertionError(f"collezione inattesa: {name}")

    return fake_get_collection, items_collection, sessions_collection


class ListOrphanedSessionItemsTests(unittest.IsolatedAsyncioTestCase):
    """Un item è orfano se il suo id non compare in nessun array ``items``."""

    async def test_item_referenced_by_a_session_is_not_orphaned(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        sessions = [_session_doc("a" * 24, [ITEM_A])]
        fake_get_collection, *_ = _patch_collections(items, sessions)

        with patch.object(session_items_service, "get_collection", fake_get_collection):
            orphaned = await session_items_service.list_orphaned_session_items()

        self.assertEqual(orphaned, [])

    async def test_item_referenced_by_no_session_is_orphaned(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])

        with patch.object(session_items_service, "get_collection", fake_get_collection):
            orphaned = await session_items_service.list_orphaned_session_items()

        self.assertEqual(len(orphaned), 1)
        self.assertIsInstance(orphaned[0], OrphanedSessionItem)
        self.assertEqual(orphaned[0].id, ITEM_A)

    async def test_created_at_is_derived_from_the_object_id(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])

        with patch.object(session_items_service, "get_collection", fake_get_collection):
            orphaned = await session_items_service.list_orphaned_session_items()

        expected = ObjectId(ITEM_A).generation_time
        self.assertEqual(orphaned[0].created_at, expected)

    async def test_item_freed_after_the_only_referencing_session_is_deleted(self) -> None:
        """Lo scenario del task: X condiviso da due sessioni, liberato una alla volta."""
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        # Ancora referenziato da una delle due sessioni.
        sessions_one_left = [_session_doc("b" * 24, [ITEM_A])]
        fake_get_collection, *_ = _patch_collections(items, sessions_one_left)
        with patch.object(session_items_service, "get_collection", fake_get_collection):
            orphaned = await session_items_service.list_orphaned_session_items()
        self.assertEqual(orphaned, [])

        # Anche la seconda sessione è stata cancellata: nessuno lo referenzia più.
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])
        with patch.object(session_items_service, "get_collection", fake_get_collection):
            orphaned = await session_items_service.list_orphaned_session_items()
        self.assertEqual([item.id for item in orphaned], [ITEM_A])


class DeleteOrphanedSessionItemsTests(unittest.IsolatedAsyncioTestCase):
    """``delete_orphaned_session_items`` ricalcola e cancella, tollerando le race."""

    async def test_deletes_every_currently_orphaned_item(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A), _item_doc(ITEM_B), _item_doc(ITEM_C)]
        sessions = [_session_doc("a" * 24, [ITEM_B])]  # solo B è ancora in uso
        fake_get_collection, items_collection, _ = _patch_collections(items, sessions)

        with patch.object(session_items_service, "get_collection", fake_get_collection):
            deleted = await session_items_service.delete_orphaned_session_items()

        self.assertEqual(sorted(deleted), sorted([ITEM_A, ITEM_C]))
        remaining_ids = {doc["_id"] for doc in items_collection.documents}
        self.assertEqual(remaining_ids, {ObjectId(ITEM_B)})

    async def test_returns_empty_list_when_nothing_is_orphaned(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        sessions = [_session_doc("a" * 24, [ITEM_A])]
        fake_get_collection, *_ = _patch_collections(items, sessions)

        with patch.object(session_items_service, "get_collection", fake_get_collection):
            deleted = await session_items_service.delete_orphaned_session_items()

        self.assertEqual(deleted, [])

    async def test_recomputes_the_list_instead_of_reusing_a_stale_one(self) -> None:
        """Chiama list_orphaned_session_items internamente, non un valore passato."""
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])

        with (
            patch.object(session_items_service, "get_collection", fake_get_collection),
            patch.object(
                session_items_service,
                "list_orphaned_session_items",
                wraps=session_items_service.list_orphaned_session_items,
            ) as spy,
        ):
            await session_items_service.delete_orphaned_session_items()

        spy.assert_awaited_once_with()

    async def test_skips_an_item_reassigned_between_computation_and_delete(self) -> None:
        """La race che il vincolo di integrità deve continuare a intercettare."""
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])

        with (
            patch.object(session_items_service, "get_collection", fake_get_collection),
            patch.object(
                session_items_service,
                "delete_session_item",
                side_effect=ConflictError("riassegnato"),
            ),
        ):
            deleted = await session_items_service.delete_orphaned_session_items()

        self.assertEqual(deleted, [])

    async def test_skips_an_item_already_deleted_concurrently(self) -> None:
        from app.services import session_items_service

        items = [_item_doc(ITEM_A)]
        fake_get_collection, *_ = _patch_collections(items, session_docs=[])

        with (
            patch.object(session_items_service, "get_collection", fake_get_collection),
            patch.object(
                session_items_service,
                "delete_session_item",
                side_effect=NotFoundError("già cancellato"),
            ),
        ):
            deleted = await session_items_service.delete_orphaned_session_items()

        self.assertEqual(deleted, [])


if __name__ == "__main__":
    unittest.main()
