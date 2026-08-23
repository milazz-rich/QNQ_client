"""Test unitari del client Firefox production."""

import shutil
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from app.models.common import Protocol
from app.services.measurement import firefox_client


class FirefoxClientTests(unittest.TestCase):
    """Copre le parti pure e locali del client Firefox."""

    def test_build_user_prefs_http2_disables_http3(self) -> None:
        prefs = firefox_client.build_user_prefs(Protocol.HTTP2)

        self.assertEqual(prefs, {"network.http.http3.enable": False})

    def test_build_user_prefs_http3_enables_verified_methodology_prefs(self) -> None:
        prefs = firefox_client.build_user_prefs(Protocol.HTTP3)

        self.assertIs(prefs["network.http.http3.enable"], True)
        self.assertIs(prefs["network.http.http3.disable_when_third_party_roots_found"], False)
        self.assertIs(prefs["network.http.http3.enable_0rtt"], False)
        self.assertIs(prefs["security.tls.enable_0rtt_data"], False)
        self.assertIs(prefs["security.ssl.disable_session_identifiers"], True)

    def test_origin_profile_is_keyed_by_scheme_host_and_port(self) -> None:
        first = firefox_client._origin_profile("https://Example.test:8444/path")
        second = firefox_client._origin_profile("https://example.test:9444/path")

        self.assertEqual(first.key, "https://example.test:8444")
        self.assertEqual(second.key, "https://example.test:9444")
        self.assertNotEqual(first.directory, second.directory)

    def test_localhost_priming_server_serves_minimal_response_and_closes(self) -> None:
        server = firefox_client.LocalhostPrimingServer().start()
        try:
            with urlopen(server.url, timeout=2) as response:
                body = response.read()
                headers = response.headers
        finally:
            server.close()

        self.assertEqual(body, b"ok\n")
        self.assertEqual(headers.get("Alt-Svc"), None)
        self.assertEqual(headers.get("Connection"), "close")

    def test_profile_copy_can_be_cleaned_up(self) -> None:
        original_run_root = firefox_client._RUN_PROFILE_ROOT
        with tempfile.TemporaryDirectory() as tmp:
            prepared = Path(tmp) / "prepared"
            prepared.mkdir()
            (prepared / "AlternateServices.bin").write_bytes(b"example.test h3")
            try:
                firefox_client._RUN_PROFILE_ROOT = Path(tmp) / "runs"

                run_profile = firefox_client._copy_prepared_profile(prepared)
                self.assertTrue((run_profile / "AlternateServices.bin").exists())

                firefox_client._remove_tree(run_profile)
                self.assertFalse(run_profile.exists())
            finally:
                firefox_client._RUN_PROFILE_ROOT = original_run_root

    def test_http3_measurement_fails_when_firefox_negotiates_h2(self) -> None:
        measurement = firefox_client._to_measurement(
            Protocol.HTTP3,
            200,
            {"nextHopProtocol": "h2", "transferSize": 1024},
            {"responseStart": 10, "responseEnd": 20},
        )

        self.assertFalse(measurement.succeeded)
        self.assertIsNone(measurement.actual_proto)
        self.assertEqual(measurement.total_ms, 0.0)
        self.assertEqual(measurement.ttfb_ms, 0.0)
        self.assertEqual(measurement.response_code, 200)


if __name__ == "__main__":
    unittest.main()
