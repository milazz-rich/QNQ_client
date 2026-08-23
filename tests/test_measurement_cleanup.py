"""Test delle garanzie di cleanup del sottosistema di misura (§5.7).

Coprono i percorsi che non si possono verificare con una misura reale: cosa
succede quando la chiusura del browser fallisce o non ritorna, e cosa resta su
disco dopo un'interruzione brusca del processo.
"""

import asyncio
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.common import Protocol
from app.services.measurement import chrome_client, firefox_client, navigation


class CloseQuietlyTests(unittest.TestCase):
    """``_close_quietly`` non deve mai poter interrompere un blocco finally."""

    def test_a_failing_close_is_degraded_to_a_warning(self) -> None:
        class Exploding:
            async def close(self) -> None:
                raise RuntimeError("browser già morto")

        asyncio.run(firefox_client._close_quietly(Exploding(), "browser di prova"))

    def test_a_hanging_close_is_bounded_by_a_timeout(self) -> None:
        class Hanging:
            async def close(self) -> None:
                await asyncio.sleep(3600)

        original = firefox_client._BROWSER_CLOSE_TIMEOUT_S
        firefox_client._BROWSER_CLOSE_TIMEOUT_S = 0.05
        try:
            asyncio.run(firefox_client._close_quietly(Hanging(), "browser bloccato"))
        finally:
            firefox_client._BROWSER_CLOSE_TIMEOUT_S = original

    def test_none_is_accepted(self) -> None:
        asyncio.run(firefox_client._close_quietly(None, "nessuna risorsa"))

    def test_profile_is_removed_even_if_closing_the_browser_fails(self) -> None:
        """La regressione che il fix impedisce: profilo orfano su close fallita."""

        class Exploding:
            async def close(self) -> None:
                raise RuntimeError("browser già morto")

        with TemporaryDirectory() as tmp:
            run_profile = Path(tmp) / "measure-xyz"
            run_profile.mkdir()
            (run_profile / "prefs.js").write_text("")

            async def simulated_finally() -> None:
                await firefox_client._close_quietly(Exploding(), "processo Firefox")
                firefox_client._remove_tree(run_profile)

            asyncio.run(simulated_finally())
            self.assertFalse(run_profile.exists())


class StaleRunProfileCleanupTests(unittest.TestCase):
    """Le copie temporanee rimaste da un crash vanno via all'avvio."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self._original = firefox_client._RUN_PROFILE_ROOT
        firefox_client._RUN_PROFILE_ROOT = Path(self._tmp.name)

    def tearDown(self) -> None:
        firefox_client._RUN_PROFILE_ROOT = self._original
        self._tmp.cleanup()

    def test_stale_measure_directories_are_removed(self) -> None:
        root = firefox_client._RUN_PROFILE_ROOT
        for name in ("measure-aaa", "measure-bbb"):
            (root / name).mkdir()
            (root / name / "AlternateServices.bin").write_bytes(b"x")

        self.assertEqual(firefox_client.cleanup_stale_run_profiles(), 2)
        self.assertEqual(list(root.iterdir()), [])

    def test_unrelated_entries_are_left_alone(self) -> None:
        root = firefox_client._RUN_PROFILE_ROOT
        (root / "measure-aaa").mkdir()
        (root / "qualcos-altro").mkdir()
        (root / "README").write_text("non toccare")

        self.assertEqual(firefox_client.cleanup_stale_run_profiles(), 1)
        self.assertTrue((root / "qualcos-altro").exists())
        self.assertTrue((root / "README").exists())

    def test_a_missing_directory_is_not_an_error(self) -> None:
        firefox_client._RUN_PROFILE_ROOT = Path(self._tmp.name) / "mai-creata"
        self.assertEqual(firefox_client.cleanup_stale_run_profiles(), 0)


class LocalhostPrimingServerTests(unittest.TestCase):
    """Il server di priming è vincolato a loopback e chiudibile sempre."""

    def test_it_binds_only_to_the_loopback_interface(self) -> None:
        server = firefox_client.LocalhostPrimingServer()
        try:
            self.assertTrue(server.url.startswith("http://127.0.0.1:"))
            self.assertEqual(server._server.server_address[0], "127.0.0.1")
            # Porta effimera assegnata dal kernel: mai una porta fissa, quindi
            # il conflitto "porta già occupata" non può presentarsi.
            self.assertGreater(server._server.server_address[1], 0)
        finally:
            server.close()

    def test_close_is_safe_when_start_was_never_called(self) -> None:
        """Senza il fix, ``shutdown()`` su un server mai avviato non ritorna."""
        server = firefox_client.LocalhostPrimingServer()
        server.close()

    def test_close_is_idempotent(self) -> None:
        server = firefox_client.LocalhostPrimingServer().start()
        server.close()
        server.close()


class ChromeNavigationFailureLoggingTests(unittest.TestCase):
    """Un'eccezione di ``page.goto`` (rifiuto di connessione, timeout, DNS)
    deve produrre una riga nel log col nome del motore, non solo silenzio.

    Prima del fix, ``navigation_error`` veniva catturato senza alcun
    ``logger.warning``: l'unica traccia era la riga generica di
    ``measurement.runner``, priva del nome del motore.
    """

    def test_navigation_failure_is_logged_with_engine_name(self) -> None:
        with self.assertLogs(chrome_client.logger, level="WARNING") as captured:
            chrome_client._log_navigation_failure(
                "net::ERR_CONNECTION_REFUSED at https://h:1/", "net::ERR_CONNECTION_REFUSED"
            )
        joined = "\n".join(captured.output)
        self.assertIn("Chrome", joined)
        self.assertIn("ERR_CONNECTION_REFUSED", joined)

    def test_falls_back_to_the_measurement_error_when_no_navigation_exception(self) -> None:
        """Il secondo percorso (nessuna eccezione, ma nessun Document): logga
        comunque, usando il messaggio finito nel ``Measurement``."""
        with self.assertLogs(chrome_client.logger, level="WARNING") as captured:
            chrome_client._log_navigation_failure(
                None, "Nessuna risposta di tipo Document ricevuta."
            )
        self.assertIn("Nessuna risposta di tipo Document ricevuta.", "\n".join(captured.output))


class CrossClientConsistencyTests(unittest.TestCase):
    """I tre motori devono restare allineati sul contratto condiviso."""

    def test_the_three_engines_expose_the_same_measure_signature(self) -> None:
        import inspect

        from app.services.measurement import curl_client
        from app.services.measurement.runner import MEASUREMENT_BACKENDS

        self.assertEqual(
            sorted(MEASUREMENT_BACKENDS), ["chrome", "curl", "firefox"]
        )
        expected = ["url", "protocol", "timeout_ms"]
        for module in (curl_client, chrome_client, firefox_client):
            params = list(inspect.signature(module.measure).parameters)
            self.assertEqual(params, expected, module.__name__)

    def test_chrome_and_firefox_apply_the_same_navigation_rule(self) -> None:
        """Le due implementazioni condividono ``navigation``, non due copie."""
        cases = [
            ("https://h:8444/content/discord/", "https://h:8444/content/discord/a.html", True),
            ("https://h:8444/content/discord/", "https://h:8444/content/altro/a.html", False),
            ("https://h:8444/content/discord/", "https://h:9444/content/discord/", False),
            ("https://h:8444/content/discord/", "http://h:8444/content/discord/", False),
            ("https://h:8444/a/index.html", "https://h:8444/a/sub/page.html", True),
        ]
        for target, candidate, expected in cases:
            with self.subTest(candidate=candidate):
                self.assertEqual(
                    chrome_client._is_allowed_internal_navigation(target, candidate),
                    firefox_client._is_allowed_internal_navigation(target, candidate),
                )
                self.assertEqual(
                    navigation.is_allowed_internal_navigation(target, candidate, "Test"),
                    expected,
                )

    def test_every_failure_leaves_timings_at_zero_and_no_actual_proto(self) -> None:
        """Invariante condiviso: un fallimento non deve mai esporre numeri."""
        measurement = firefox_client._to_measurement(
            Protocol.HTTP3,
            200,
            {"nextHopProtocol": "h2", "responseStart": 12.0, "responseEnd": 99.0},
            {},
        )
        self.assertFalse(measurement.succeeded)
        self.assertIsNone(measurement.actual_proto)
        self.assertEqual((measurement.total_ms, measurement.ttfb_ms, measurement.kb), (0.0, 0.0, 0.0))
        self.assertEqual(measurement.response_code, 200)


if __name__ == "__main__":
    unittest.main()
