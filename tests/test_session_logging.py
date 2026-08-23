"""Test del meccanismo di cattura su file dei log di sessione (§5.10)."""

import asyncio
import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core import session_logging

SESSION_A = "a" * 24
SESSION_B = "b" * 24


class SessionLoggingTests(unittest.TestCase):
    """Verifica correlazione, isolamento e validazione del nome file."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._original_root = session_logging.SESSION_LOG_ROOT
        session_logging.SESSION_LOG_ROOT = Path(self._tmp.name)
        # Il logger radice, con la sua soglia, è ciò che alimenta l'handler:
        # senza livello INFO i record dei service non lo raggiungerebbero.
        self._root = logging.getLogger()
        self._original_level = self._root.level
        self._root.setLevel(logging.INFO)

    def tearDown(self) -> None:
        session_logging.SESSION_LOG_ROOT = self._original_root
        self._root.setLevel(self._original_level)
        self._tmp.cleanup()

    def test_records_are_routed_to_the_file_of_the_active_session(self) -> None:
        logger = logging.getLogger("app.services.measurement.fake_client")
        with session_logging.session_log_context(SESSION_A) as path:
            logger.warning("misura scartata")

        self.assertIsNotNone(path)
        self.assertIn("misura scartata", path.read_text(encoding="utf-8"))

    def test_two_sessions_in_sequence_do_not_mix(self) -> None:
        logger = logging.getLogger("app.services.session_runner")
        with session_logging.session_log_context(SESSION_A):
            logger.info("riga della sessione A")
        with session_logging.session_log_context(SESSION_B):
            logger.info("riga della sessione B")

        text_a = session_logging.read_session_log(SESSION_A)
        text_b = session_logging.read_session_log(SESSION_B)
        self.assertIn("sessione A", text_a)
        self.assertNotIn("sessione B", text_a)
        self.assertIn("sessione B", text_b)
        self.assertNotIn("sessione A", text_b)

    def test_records_outside_a_session_are_not_captured(self) -> None:
        session_logging.setup_session_logging()
        logging.getLogger("app.services.session_runner").info("fuori da ogni sessione")

        self.assertIsNone(session_logging.read_session_log(SESSION_A))

    def test_log_remains_readable_after_a_failed_session(self) -> None:
        logger = logging.getLogger("app.services.session_runner")
        with self.assertRaises(RuntimeError):
            with session_logging.session_log_context(SESSION_A):
                logger.info("prima dell'errore")
                raise RuntimeError("esecuzione interrotta")

        self.assertIn("prima dell'errore", session_logging.read_session_log(SESSION_A))

    def test_relaunching_a_session_truncates_the_previous_log(self) -> None:
        logger = logging.getLogger("app.services.session_runner")
        with session_logging.session_log_context(SESSION_A):
            logger.info("prima esecuzione")
        with session_logging.session_log_context(SESSION_A):
            logger.info("seconda esecuzione")

        text = session_logging.read_session_log(SESSION_A)
        self.assertIn("seconda esecuzione", text)
        self.assertNotIn("prima esecuzione", text)

    def test_context_var_is_inherited_by_awaited_coroutines(self) -> None:
        """È il presupposto per catturare i log dei client senza passare l'id."""
        logger = logging.getLogger("app.services.measurement.firefox_client")

        async def deep_measure() -> None:
            await asyncio.sleep(0)
            logger.warning("emesso da una coroutine annidata")

        async def run() -> None:
            with session_logging.session_log_context(SESSION_A):
                await deep_measure()

        asyncio.run(run())
        self.assertIn(
            "coroutine annidata", session_logging.read_session_log(SESSION_A)
        )

    def test_malformed_session_id_never_composes_a_path(self) -> None:
        for bogus in ("../../etc/passwd", "nothex" * 4, "", "a" * 23, "a" * 25):
            with self.assertRaises(ValueError):
                session_logging.session_log_path(bogus)

    def test_malformed_session_id_does_not_break_the_execution(self) -> None:
        with session_logging.session_log_context("../../etc/passwd") as path:
            logging.getLogger("app.services.session_runner").info("la sessione gira comunque")
        self.assertIsNone(path)

    def test_delete_removes_the_file_and_is_idempotent(self) -> None:
        with session_logging.session_log_context(SESSION_A):
            logging.getLogger("app.services.session_runner").info("da cancellare")

        self.assertTrue(session_logging.delete_session_log(SESSION_A))
        self.assertIsNone(session_logging.read_session_log(SESSION_A))
        self.assertFalse(session_logging.delete_session_log(SESSION_A))


if __name__ == "__main__":
    unittest.main()
