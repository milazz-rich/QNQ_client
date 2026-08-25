"""Test del lock su sessioni concorrenti e del recupero da crash (§5.1).

Verificati end-to-end con un server reale in una sessione precedente (409 su
un secondo avvio, recupero effettivo dopo un riavvio con una Session forzata
a ``running``). Questi test coprono invece — con un finto motor collection,
senza bisogno di MongoDB — la logica di decisione isolata: cosa restituisce
``get_running_session``/``recover_interrupted_sessions`` dati certi documenti,
e come il router ``start_session`` reagisce ai vari stati.
"""

import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from bson import ObjectId

from app.core.errors import ConflictError, ValidationError
from app.models.session import RunStatus, Session


def _session_doc(session_id: str, status: str, **overrides: object) -> dict:
    """Costruisce un documento Session grezzo, come lo restituirebbe motor."""
    doc = {
        "_id": ObjectId(session_id),
        "name": overrides.pop("name", "sessione di test"),
        "targetId": "1" * 24,
        "clientId": "2" * 24,
        "reps": 1,
        "timeout": 5000,
        "when": datetime.now(UTC),
        "status": status,
        "currentIndex": 0,
        "items": [],
    }
    doc.update(overrides)
    return doc


class FakeSessionsCollection:
    """Sostituto minimale della collezione motor ``sessions``.

    Espone solo i metodi usati da ``sessions_service`` in questi test:
    ``find_one`` e ``update_many``. Non riproduce l'indice unico parziale di
    Mongo (quella garanzia è verificata con un database reale, non qui) — qui
    si verifica solo che il codice Python interroghi e scriva con il filtro
    corretto.
    """

    def __init__(self, documents: list[dict]) -> None:
        self.documents = documents

    async def find_one(self, query: dict) -> dict | None:
        for doc in self.documents:
            if all(doc.get(key) == value for key, value in query.items()):
                return doc
        return None

    async def update_many(self, query: dict, update: dict):
        matched = [
            doc for doc in self.documents
            if all(doc.get(key) == value for key, value in query.items())
        ]
        for doc in matched:
            doc.update(update.get("$set", {}))
        return type("Result", (), {"modified_count": len(matched)})()


class GetRunningSessionTests(unittest.IsolatedAsyncioTestCase):
    """``get_running_session`` deve trovare al più una sessione ``running``."""

    async def test_returns_none_when_nothing_is_running(self) -> None:
        from app.services import sessions_service

        docs = [_session_doc("a" * 24, "completed"), _session_doc("b" * 24, "pending")]
        with patch.object(
            sessions_service, "get_collection", return_value=FakeSessionsCollection(docs)
        ):
            result = await sessions_service.get_running_session()
        self.assertIsNone(result)

    async def test_returns_the_running_session(self) -> None:
        from app.services import sessions_service

        docs = [
            _session_doc("a" * 24, "completed"),
            _session_doc("b" * 24, "running", name="quella in corso"),
        ]
        with patch.object(
            sessions_service, "get_collection", return_value=FakeSessionsCollection(docs)
        ):
            result = await sessions_service.get_running_session()
        self.assertIsInstance(result, Session)
        self.assertEqual(result.name, "quella in corso")
        self.assertEqual(result.status, RunStatus.RUNNING)


class RecoverInterruptedSessionsTests(unittest.IsolatedAsyncioTestCase):
    """``recover_interrupted_sessions`` riporta a ``failed`` ogni 'running' residua."""

    async def test_recovers_running_sessions_with_an_explanatory_note(self) -> None:
        from app.services import sessions_service

        docs = [
            _session_doc("a" * 24, "running"),
            _session_doc("b" * 24, "completed"),
        ]
        with patch.object(
            sessions_service, "get_collection", return_value=FakeSessionsCollection(docs)
        ):
            recovered = await sessions_service.recover_interrupted_sessions()

        self.assertEqual(recovered, 1)
        self.assertEqual(docs[0]["status"], "failed")
        self.assertIn("crash", docs[0]["note"].lower())
        # La sessione già completata non va toccata.
        self.assertEqual(docs[1]["status"], "completed")
        self.assertNotIn("note", docs[1])

    async def test_is_a_noop_when_nothing_is_running(self) -> None:
        from app.services import sessions_service

        docs = [_session_doc("a" * 24, "completed"), _session_doc("b" * 24, "failed")]
        with patch.object(
            sessions_service, "get_collection", return_value=FakeSessionsCollection(docs)
        ):
            recovered = await sessions_service.recover_interrupted_sessions()
        self.assertEqual(recovered, 0)

    async def test_recovers_multiple_running_sessions_at_once(self) -> None:
        """Il caso che l'indice unico impedirà d'ora in poi, ma che può ancora
        esistere come residuo scritto prima che l'indice esistesse."""
        from app.services import sessions_service

        docs = [_session_doc("a" * 24, "running"), _session_doc("b" * 24, "running")]
        with patch.object(
            sessions_service, "get_collection", return_value=FakeSessionsCollection(docs)
        ):
            recovered = await sessions_service.recover_interrupted_sessions()
        self.assertEqual(recovered, 2)
        self.assertTrue(all(doc["status"] == "failed" for doc in docs))


class StartSessionRouterTests(unittest.IsolatedAsyncioTestCase):
    """La logica di decisione di ``POST /sessions/{id}/start`` (§5.1)."""

    def _session(self, session_id: str, status: RunStatus, items: bool = True) -> Session:
        return Session.model_validate(
            _session_doc(
                session_id,
                status.value,
                items=[
                    {
                        "sessionItemId": "3" * 24,
                        "label": "item",
                        "proto": "HTTP/2",
                        "total": 1,
                    }
                ]
                if items
                else [],
            )
        )

    async def test_rejects_when_this_session_is_already_running(self) -> None:
        from app.routers.sessions import start_session

        target = self._session("a" * 24, RunStatus.RUNNING)
        with patch(
            "app.routers.sessions.sessions_service.get_session", AsyncMock(return_value=target)
        ):
            with self.assertRaises(ConflictError):
                await start_session(background_tasks=MagicMock(), session_id="a" * 24)

    async def test_rejects_when_another_session_is_running(self) -> None:
        from app.routers.sessions import start_session

        target = self._session("a" * 24, RunStatus.PENDING)
        other = self._session("b" * 24, RunStatus.RUNNING)
        object.__setattr__(other, "name", "quella bloccante")
        with (
            patch(
                "app.routers.sessions.sessions_service.get_session",
                AsyncMock(return_value=target),
            ),
            patch(
                "app.routers.sessions.sessions_service.get_running_session",
                AsyncMock(return_value=other),
            ),
        ):
            with self.assertRaises(ConflictError) as ctx:
                await start_session(background_tasks=MagicMock(), session_id="a" * 24)
        # Il messaggio deve nominare la sessione bloccante, non essere generico.
        self.assertIn("quella bloccante", str(ctx.exception))
        self.assertIn("b" * 24, str(ctx.exception))

    async def test_rejects_when_no_items(self) -> None:
        from app.routers.sessions import start_session

        target = self._session("a" * 24, RunStatus.PENDING, items=False)
        with (
            patch(
                "app.routers.sessions.sessions_service.get_session",
                AsyncMock(return_value=target),
            ),
            patch(
                "app.routers.sessions.sessions_service.get_running_session",
                AsyncMock(return_value=None),
            ),
        ):
            with self.assertRaises(ValidationError):
                await start_session(background_tasks=MagicMock(), session_id="a" * 24)

    async def test_starts_when_nothing_else_is_running(self) -> None:
        from app.models.session import RunStatus as RS
        from app.routers.sessions import start_session

        target = self._session("a" * 24, RunStatus.PENDING)
        set_status_mock = AsyncMock()
        background_tasks = MagicMock()
        with (
            patch(
                "app.routers.sessions.sessions_service.get_session",
                AsyncMock(return_value=target),
            ),
            patch(
                "app.routers.sessions.sessions_service.get_running_session",
                AsyncMock(return_value=None),
            ),
            patch("app.routers.sessions.sessions_service.set_status", set_status_mock),
        ):
            response = await start_session(background_tasks=background_tasks, session_id="a" * 24)

        set_status_mock.assert_awaited_once_with("a" * 24, RS.RUNNING)
        background_tasks.add_task.assert_called_once()
        self.assertEqual(response.status, RS.RUNNING)


if __name__ == "__main__":
    unittest.main()
