"""Test unitari del client Firefox production."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen

from app.models.common import Protocol
from app.services.measurement import firefox_client, navigation


class FakePage:
    """Pagina fake che espone solo le API usate dal retry Navigation Timing."""

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = outcomes
        self.evaluate_calls = 0
        self.goto_calls = 0
        self.wait_calls = 0

    async def evaluate(self, script: str) -> object:
        self.evaluate_calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def wait_for_load_state(self, state: str, timeout: int) -> None:
        self.wait_calls += 1


class FakeTracker:
    """Tracker fake con conteggi controllati di navigazione main-frame."""

    def __init__(
        self,
        counts: list[int] | None = None,
        urls: list[str] | None = None,
    ) -> None:
        self.counts = counts or [1]
        self.urls = urls or ["https://example.test/content/page/"]

    def count_since(self, mark: firefox_client.MainFrameNavigationMark) -> int:
        if len(self.counts) > 1:
            return self.counts.pop(0)
        return self.counts[0]

    def urls_since(self, mark: firefox_client.MainFrameNavigationMark) -> list[str]:
        return self.urls


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

    def test_navigation_timing_evaluate_succeeds_first_try(self) -> None:
        page = FakePage([{"nextHopProtocol": "h3", "transferSize": 512}])
        tracker = FakeTracker([1])
        mark = firefox_client.MainFrameNavigationMark(0, 0)

        entry = asyncio.run(
            firefox_client._read_navigation_entry(
                page,
                tracker,
                mark,
                "https://example.test/content/page/",
            )
        )

        self.assertEqual(entry["nextHopProtocol"], "h3")
        self.assertEqual(page.evaluate_calls, 1)
        self.assertEqual(page.goto_calls, 0)

    def test_navigation_timing_retries_context_destroyed_only(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        page = FakePage(
            [
                Exception(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                ),
                {"nextHopProtocol": "h3", "transferSize": 512},
            ]
        )
        tracker = FakeTracker([1])
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            entry = asyncio.run(
                firefox_client._read_navigation_entry(
                    page,
                    tracker,
                    mark,
                    "https://example.test/content/page/",
                )
            )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet

        self.assertEqual(entry["nextHopProtocol"], "h3")
        self.assertEqual(page.evaluate_calls, 2)
        self.assertEqual(page.goto_calls, 0)

    def test_navigation_timing_does_not_retry_other_evaluate_errors(self) -> None:
        page = FakePage([RuntimeError("boom")])
        tracker = FakeTracker([1])
        mark = firefox_client.MainFrameNavigationMark(0, 0)

        with self.assertRaises(RuntimeError):
            asyncio.run(
                firefox_client._read_navigation_entry(
                    page,
                    tracker,
                    mark,
                    "https://example.test/content/page/",
                )
            )

        self.assertEqual(page.evaluate_calls, 1)

    def test_navigation_timing_respects_max_retry_count(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        error = Exception(
            "Page.evaluate: Execution context was destroyed, "
            "most likely because of a navigation"
        )
        page = FakePage([error, error, error, error])
        tracker = FakeTracker([1])
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            with self.assertRaises(firefox_client.NavigationTimingError):
                asyncio.run(
                    firefox_client._read_navigation_entry(
                        page,
                        tracker,
                        mark,
                        "https://example.test/content/page/",
                    )
                )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet

        self.assertEqual(page.evaluate_calls, firefox_client._NAVIGATION_TIMING_RETRIES)

    def test_internal_httrack_navigation_is_accepted(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        page = FakePage([{"nextHopProtocol": "h3", "transferSize": 512}])
        tracker = FakeTracker(
            [2],
            [
                "https://example.test/content/discord/",
                "https://example.test/content/discord/discord.com/index.html",
            ],
        )
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            entry = asyncio.run(
                firefox_client._read_navigation_entry(
                    page,
                    tracker,
                    mark,
                    "https://example.test/content/discord/",
                )
            )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet

        self.assertEqual(entry["nextHopProtocol"], "h3")
        self.assertEqual(page.evaluate_calls, 1)
        self.assertEqual(page.goto_calls, 0)

    def test_external_main_frame_navigation_fails(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        page = FakePage([{"nextHopProtocol": "h3", "transferSize": 512}])
        tracker = FakeTracker(
            [2],
            [
                "https://example.test/content/discord/",
                "https://discord.com/",
            ],
        )
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            with self.assertRaises(firefox_client.ExternalMainFrameNavigationError):
                asyncio.run(
                    firefox_client._read_navigation_entry(
                        page,
                        tracker,
                        mark,
                        "https://example.test/content/discord/",
                    )
                )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet

        self.assertEqual(page.evaluate_calls, 0)
        self.assertEqual(page.goto_calls, 0)

    def test_navigation_timing_recovers_after_internal_navigation_context_destroyed(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        page = FakePage(
            [
                Exception(
                    "Page.evaluate: Execution context was destroyed, "
                    "most likely because of a navigation"
                ),
                {"nextHopProtocol": "h3", "transferSize": 512},
            ]
        )
        tracker = FakeTracker(
            [2],
            [
                "https://example.test/content/discord/",
                "https://example.test/content/discord/discord.com/index.html",
            ],
        )
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            entry = asyncio.run(
                firefox_client._read_navigation_entry(
                    page,
                    tracker,
                    mark,
                    "https://example.test/content/discord/",
                )
            )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet

        self.assertEqual(entry["nextHopProtocol"], "h3")
        self.assertEqual(page.evaluate_calls, 2)

    def test_entry_must_match_final_response_url(self) -> None:
        record = firefox_client.MainFrameNavigationRecord(
            url="https://example.test/content/discord/discord.com/index.html",
            status=200,
            timing={"responseStart": 1, "responseEnd": 2},
        )

        self.assertTrue(
            firefox_client._entry_matches_navigation(
                {"name": "https://example.test/content/discord/discord.com/index.html"},
                record,
            )
        )
        self.assertFalse(
            firefox_client._entry_matches_navigation(
                {"name": "https://example.test/content/discord/"},
                record,
            )
        )

    def test_internal_navigation_path_must_remain_under_scenario_prefix(self) -> None:
        self.assertTrue(
            firefox_client._is_allowed_internal_navigation(
                "https://example.test:8444/content/discord/",
                "https://example.test:8444/content/discord/discord.com/index.html",
            )
        )
        self.assertFalse(
            firefox_client._is_allowed_internal_navigation(
                "https://example.test:8444/content/discord/",
                "https://example.test:8444/content/wikipedia/index.html",
            )
        )

    def test_main_frame_stability_timeout_fails(self) -> None:
        original_quiet = navigation.MAIN_FRAME_QUIET_MS
        original_timeout = navigation.MAIN_FRAME_SETTLE_TIMEOUT_MS
        navigation.MAIN_FRAME_QUIET_MS = 0
        navigation.MAIN_FRAME_SETTLE_TIMEOUT_MS = 0
        page = FakePage([])
        tracker = FakeTracker(
            [1, 2],
            [
                "https://example.test/content/discord/",
                "https://example.test/content/discord/discord.com/index.html",
            ],
        )
        mark = firefox_client.MainFrameNavigationMark(0, 0)
        try:
            with self.assertRaises(firefox_client.MainFrameStabilityTimeoutError):
                asyncio.run(
                    firefox_client._wait_for_main_frame_quiet(
                        page,
                        tracker,
                        mark,
                        "https://example.test/content/discord/",
                    )
                )
        finally:
            navigation.MAIN_FRAME_QUIET_MS = original_quiet
            navigation.MAIN_FRAME_SETTLE_TIMEOUT_MS = original_timeout

    def test_measurement_uses_navigation_timing_entry_for_final_document(self) -> None:
        measurement = firefox_client._to_measurement(
            Protocol.HTTP3,
            200,
            {
                "nextHopProtocol": "h3",
                "responseStart": 11.0,
                "responseEnd": 24.0,
                "transferSize": 1024,
            },
            {"responseStart": 12.5, "responseEnd": 25.0},
        )

        self.assertTrue(measurement.succeeded)
        self.assertEqual(measurement.ttfb_ms, 11.0)
        self.assertEqual(measurement.total_ms, 24.0)

    def test_measurement_falls_back_to_encoded_body_size_when_transfer_size_is_zero(self) -> None:
        measurement = firefox_client._to_measurement(
            Protocol.HTTP3,
            200,
            {
                "nextHopProtocol": "h3",
                "responseStart": 11.0,
                "responseEnd": 24.0,
                "transferSize": 0,
                "encodedBodySize": 2048,
            },
            {"responseStart": 11.0, "responseEnd": 24.0},
        )

        self.assertTrue(measurement.succeeded)
        self.assertEqual(measurement.kb, 2.0)

    def test_measurement_falls_back_to_final_response_content_length(self) -> None:
        measurement = firefox_client._to_measurement(
            Protocol.HTTP3,
            200,
            {
                "nextHopProtocol": "h3",
                "responseStart": 11.0,
                "responseEnd": 24.0,
                "transferSize": 0,
                "encodedBodySize": 0,
            },
            {"responseStart": 11.0, "responseEnd": 24.0},
            byte_size_hint=4096,
        )

        self.assertTrue(measurement.succeeded)
        self.assertEqual(measurement.kb, 4.0)


if __name__ == "__main__":
    unittest.main()
