from models.measure import Measure


class MeasureService:
  _table = "measure"

  def __init__(self, connection):
    self._connection = connection

  def _cols(self) -> list[str]:
    rows = self._connection.execute(f"PRAGMA table_info({self._table})").fetchall()
    return [row["name"] for row in rows]

  def _data(self, measure: Measure, include_id: bool = True) -> dict:
    table_columns = set(self._cols())
    data = {}
    for key, value in measure.__dict__.items():
      if key in table_columns and (include_id or key != "id"):
        data[key] = value
    return data

  def insert(self, measure: Measure) -> int:
    data = self._data(measure, include_id=False)
    payload = {key: value for key, value in data.items() if value is not None}
    if not payload:
      raise ValueError("measure has no values to insert")

    columns = list(payload.keys())
    placeholders = ", ".join(["?"] * len(columns))
    sql = f"INSERT INTO {self._table} ({', '.join(columns)}) VALUES ({placeholders})"
    cursor = self._connection.execute(sql, tuple(payload[column] for column in columns))
    self._connection.commit()
    return int(cursor.lastrowid)

  def find(self, measure: Measure) -> Measure | None:
    row = self._connection.execute(
      f"SELECT * FROM {self._table} WHERE id = ?",
      (measure.id,),
    ).fetchone()
    if row is None:
      return None
    return Measure.from_row(row)

  def filter(self, partial: dict[str, object]) -> list[Measure]:
    clauses = []
    values = []
    allowed_columns = set(self._cols())

    for key, value in partial.items():
      if key not in allowed_columns:
        continue
      if key == "id" and value == 0:
        continue
      if value is not None:
        clauses.append(f"{key} = ?")
        values.append(value)

    sql = f"SELECT * FROM {self._table}"
    if clauses:
      sql = f"{sql} WHERE {' AND '.join(clauses)}"
    sql = f"{sql} ORDER BY id DESC"

    rows = self._connection.execute(sql, tuple(values)).fetchall()
    return [Measure.from_row(row) for row in rows]

  def update(self, measure: Measure) -> bool:
    if measure.id is None or measure.id <= 0:
      raise ValueError("measure.id must be > 0")

    assignments = []
    values = []
    data = self._data(measure, include_id=False)

    for key, value in data.items():
      if value is not None:
        assignments.append(f"{key} = ?")
        values.append(value)

    if not assignments:
      return False

    values.append(measure.id)
    cursor = self._connection.execute(
      f"UPDATE {self._table} SET {', '.join(assignments)} WHERE id = ?",
      tuple(values),
    )
    self._connection.commit()
    return cursor.rowcount > 0

  def remove(self, measure: Measure) -> bool:
    if measure.id is None or measure.id <= 0:
      raise ValueError("measure.id must be > 0")

    cursor = self._connection.execute(
      f"DELETE FROM {self._table} WHERE id = ?",
      (measure.id,),
    )
    self._connection.commit()
    return cursor.rowcount > 0
