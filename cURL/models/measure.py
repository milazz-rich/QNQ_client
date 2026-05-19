from typing import Any


class Measure:
  def __init__(self, url: str, timer: float, id: int = 0):
    self.id = id
    self.url = str(url)
    self.timer = float(timer)

  @classmethod
  def from_dict(cls, data: dict[str, Any]) -> "Measure":
    if "url" not in data or "timer" not in data:
      raise ValueError("'url' and 'timer' are required")
    return cls(id=data.get("id"), url=data["url"], timer=data["timer"])

  @classmethod
  def from_row(cls, row: Any) -> "Measure":
    return cls(id=row["id"], url=row["url"], timer=row["timer"])

  def to_dict(self) -> dict[str, Any]:
    return {
      "id": self.id,
      "url": self.url,
      "timer": self.timer,
    }
