"""Test unitari del client Chrome production."""

import unittest

from app.services.measurement import chrome_client


class ChromeClientTests(unittest.TestCase):
    """Copre la selezione del documento main-frame finale per contenuti HTTrack."""

    def test_internal_httrack_navigation_is_allowed(self) -> None:
        self.assertTrue(
            chrome_client._is_allowed_internal_navigation(
                "https://example.test:8444/content/discord/",
                "https://example.test:8444/content/discord/discord.com/index.html",
            )
        )

    def test_external_navigation_is_rejected(self) -> None:
        self.assertFalse(
            chrome_client._is_allowed_internal_navigation(
                "https://example.test:8444/content/discord/",
                "https://discord.com/",
            )
        )

    def test_navigation_outside_scenario_prefix_is_rejected(self) -> None:
        self.assertFalse(
            chrome_client._is_allowed_internal_navigation(
                "https://example.test:8444/content/discord/",
                "https://example.test:8444/content/wikipedia/index.html",
            )
        )

    def test_final_document_response_uses_last_internal_document(self) -> None:
        responses = [
            {
                "requestId": "initial",
                "response": {"url": "https://example.test/content/discord/"},
            },
            {
                "requestId": "final",
                "response": {
                    "url": "https://example.test/content/discord/discord.com/index.html"
                },
            },
        ]

        selected = chrome_client._final_document_response(
            responses,
            "https://example.test/content/discord/",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["requestId"], "final")

    def test_final_document_response_ignores_external_documents(self) -> None:
        responses = [
            {
                "requestId": "initial",
                "response": {"url": "https://example.test/content/discord/"},
            },
            {
                "requestId": "external",
                "response": {"url": "https://discord.com/"},
            },
        ]

        selected = chrome_client._final_document_response(
            responses,
            "https://example.test/content/discord/",
        )

        self.assertIsNotNone(selected)
        self.assertEqual(selected["requestId"], "initial")


if __name__ == "__main__":
    unittest.main()
