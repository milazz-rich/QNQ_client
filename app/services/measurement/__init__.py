"""Esecuzione delle misurazioni HTTP/2 e HTTP/3.

Tre motori di misura, che espongono lo stesso contratto ``measure(url,
protocol, timeout_ms) -> Measurement``: ``curl_client`` invoca il binario
curl come processo esterno, ``chrome_client`` guida Chromium headless via
Playwright e ne legge i timing dal Chrome DevTools Protocol, ``firefox_client``
guida Firefox headless via Playwright e ne legge i timing dal Resource Timing
(§5.2, §5.6, §5.7 di AGENTS.md).

``navigation`` raccoglie le regole di catena main-frame condivise fra i due
motori browser, che devono restare identiche perché il confronto fra motori
abbia significato.

``runner`` collega le entità del dominio (Target, Scenario, Client,
SessionItem) al motore corrispondente — la mappa ``MEASUREMENT_BACKENDS`` è il
punto di estensione — e produce i ``Result``.
"""

from app.services.measurement.curl_client import Measurement, build_url, measure
from app.services.measurement.runner import (
    MEASUREMENT_BACKENDS,
    MeasurementContext,
    measure_once,
    resolve_context,
)

__all__ = [
    "MEASUREMENT_BACKENDS",
    "Measurement",
    "MeasurementContext",
    "build_url",
    "measure",
    "measure_once",
    "resolve_context",
]
