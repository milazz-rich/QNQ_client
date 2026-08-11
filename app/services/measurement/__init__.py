"""Esecuzione delle misurazioni HTTP/2 e HTTP/3.

Due motori di misura, che espongono lo stesso contratto ``measure(url,
protocol, timeout_ms) -> Measurement``: ``curl_client`` invoca il binario
curl come processo esterno, ``chrome_client`` guida Chromium headless via
Playwright e ne legge i timing dal Chrome DevTools Protocol.

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
