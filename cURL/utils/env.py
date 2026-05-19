from pathlib import Path
from typing import Any


def _parse_scalar(value: str) -> Any:
  raw = value.strip()
  if raw == "":
    return ""

  try:
    return int(raw)
  except ValueError:
    pass

  try:
    return float(raw)
  except ValueError:
    return raw


def _parse_value(value: str) -> Any:
  if "," in value:
    return [_parse_scalar(item) for item in value.split(",") if item.strip()]
  return _parse_scalar(value)


def load_env() -> dict[str, Any]:
  env_path = Path(__file__).parent.parent.parent / ".env"
  if not env_path.exists():
    return {}

  env = {}
  for raw_line in env_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
      continue
    key, value = line.split("=", 1)
    env[key.strip()] = _parse_value(value)

  return env
