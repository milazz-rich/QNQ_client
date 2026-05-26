from pathlib import Path

from services.db_service import DatabaseService
from services.http_service import HttpsService
from services.measure_service import MeasureService
from services.chart_service import ChartService
from models.measure import Measure
from utils.env import load_env


def _to_bool(value: object, default: bool = False) -> bool:
  if isinstance(value, bool):
    return value
  if value is None:
    return default
  return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _build_chart_data(urls: list[str], measure_service: MeasureService) -> tuple[list[str], list[float]]:
  x = urls
  y = []
  for url in urls:
    measures = measure_service.filter({"url": url})
    average = sum(item.timer for item in measures) / len(measures) if measures else 0
    y.append(average)
  return x, y


def main():
  env = load_env()
  database_service = DatabaseService(db_path=env.get("DB_NAME", "qnq.db"), tables=env.get("DB_TABLES", []))
  database_service.init_tables()
  connection = database_service.getConnection()

  https_service = HttpsService(base_url=str(env.get("BASE_URL", "")))
  measure_service = MeasureService(connection)
  chart_service = ChartService()

  measure_count = int(env.get("MEASURE_COUNT", 1))
  run_measurements = _to_bool(env.get("RUN_MEASUREMENTS", True), default=True)
  use_quic = _to_bool(env.get("USE_QUIC", False), default=False)
  urls = env.get("URLS", [])

  try:
    if run_measurements:
      for url in urls:
        for _ in range(measure_count):
          response_time_ms = https_service.measure(url, use_quic)
          measure_service.insert(Measure(url=url, timer=response_time_ms, quic=use_quic))
          print(f"saved measure url={url} time={response_time_ms}ms quic={use_quic}")

    csv_path = database_service.export_csv(
      "measure",
      str(Path.cwd() / "measure.csv"),
    )
    print(f"csv exported: {csv_path}")

    x, y = _build_chart_data(urls, measure_service)
    chart_service.histogram(x, y)
  finally:
    database_service.close()

if __name__ == "__main__":
  main()
