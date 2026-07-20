"""Esecuzione delle misurazioni HTTP/2 e HTTP/3 tramite il binario curl esterno.

``curl_client`` incapsula l'invocazione del processo e il parsing dei timing;
``runner`` collega le entità del dominio (Target, Scenario, Client, SessionItem)
alla misurazione e produce i ``Result``.
"""

from app.services.measurement.curl_client import CurlMeasurement, build_url, measure
from app.services.measurement.runner import (
    SUPPORTED_CLIENT,
    MeasurementContext,
    measure_once,
    resolve_context,
)

__all__ = [
    "SUPPORTED_CLIENT",
    "CurlMeasurement",
    "MeasurementContext",
    "build_url",
    "measure",
    "measure_once",
    "resolve_context",
]
