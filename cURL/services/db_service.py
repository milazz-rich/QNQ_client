import csv
import sqlite3
from pathlib import Path


class DatabaseService:
  def __init__(self, db_path: str = "qnq.db", tables: list[str] = []):
    self.db_path = str(Path(db_path))
    self._sql_dir = Path(__file__).parent.parent / "sql"
    self.tables = tables
    self._connection = sqlite3.connect(self.db_path)
    self._connection.row_factory = sqlite3.Row

  def _read_sql(self, file_name: str) -> str:
    file_path = self._sql_dir / file_name
    return file_path.read_text(encoding="utf-8")

  def getConnection(self) -> sqlite3.Connection:
    return self._connection

  def export_csv(self, table_name: str, output_path: str) -> str:
    file_path = Path(output_path)
    cursor = self._connection.execute(f"SELECT * FROM {table_name} ORDER BY url ASC")
    columns = [description[0] for description in cursor.description or []]
    rows = cursor.fetchall()

    with file_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
      writer = csv.writer(csv_file, delimiter=";")
      if columns:
        writer.writerow(columns)
      for row in rows:
        writer.writerow([row[column] for column in columns])

    return str(file_path)
    
  def init_tables(self) -> None:
    for table in self.tables:
      create_table_sql = self._read_sql(f"{table}.sql")
      self._connection.executescript(create_table_sql)
    self._connection.commit()

  def close(self) -> None:
    self._connection.close()
