"""
sql.py - простой менеджер для SQLite.
Каждый метод сам открывает соединение, выполняет действие и возвращает результат.
Потокобезопасно для использования в Flask.
"""

import sqlite3
from typing import Any, Dict, List, Optional, Tuple, Union


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _escape_identifier(self, name: str) -> str:
        """Экранирует имя таблицы/колонки двойными кавычками."""
        return '"' + name.replace('"', '""') + '"'

    def create_table(self, table_name: str, columns: Dict[str, str]) -> None:
        """Создаёт таблицу, если её нет.
        columns: {'mail': 'VARCHAR(255) NOT NULL UNIQUE', 'password': 'VARCHAR(255) NOT NULL'}
        """
        escaped = self._escape_identifier(table_name)
        cols_def = ', '.join(f'{self._escape_identifier(col)} {typ}' for col, typ in columns.items())
        query = f"CREATE TABLE IF NOT EXISTS {escaped} ({cols_def})"

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(query)
            conn.commit()
        finally:
            conn.close()

    def insert(self, table_name: str, data: Dict[str, Any]) -> int:
        """Вставляет строку, возвращает lastrowid."""
        escaped = self._escape_identifier(table_name)
        columns = ', '.join(self._escape_identifier(k) for k in data.keys())
        placeholders = ', '.join(['?' for _ in data])
        query = f"INSERT INTO {escaped} ({columns}) VALUES ({placeholders})"

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(query, tuple(data.values()))
            conn.commit()
            return cursor.lastrowid
        finally:
            conn.close()

    def select(self,
               table_name: str,
               columns: Union[str, List[str]] = '*',
               where: Optional[str] = None,
               params: Tuple = ()) -> List[sqlite3.Row]:
        """Возвращает список строк (каждая строка — sqlite3.Row, поддерживает доступ по ключу)."""
        escaped = self._escape_identifier(table_name)

        if isinstance(columns, list):
            cols = ', '.join(self._escape_identifier(c) for c in columns)
        else:
            cols = '*'

        query = f"SELECT {cols} FROM {escaped}"
        if where:
            query += f" WHERE {where}"

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(query, params)
            result = cursor.fetchall()
            return result
        finally:
            conn.close()

    def update(self,
               table_name: str,
               data: Dict[str, Any],
               where: str,
               params: Tuple = ()) -> int:
        """Обновляет строки, возвращает количество изменённых строк."""
        escaped = self._escape_identifier(table_name)
        set_clause = ', '.join(f'{self._escape_identifier(k)} = ?' for k in data.keys())
        query = f"UPDATE {escaped} SET {set_clause} WHERE {where}"
        all_params = tuple(data.values()) + params

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(query, all_params)
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def delete(self, table_name: str, where: str, params: Tuple = ()) -> int:
        """Удаляет строки, возвращает количество удалённых."""
        escaped = self._escape_identifier(table_name)
        query = f"DELETE FROM {escaped} WHERE {where}"

        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.execute(query, params)
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def execute_raw(self, query: str, params: Tuple = (), commit: bool = True) -> List[sqlite3.Row]:
        """Выполняет произвольный запрос. Для SELECT возвращает строки, для INSERT/UPDATE/DELETE – пустой список."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            cursor = conn.execute(query, params)
            if commit:
                conn.commit()
            # Пытаемся вернуть результат, если это SELECT
            try:
                return cursor.fetchall()
            except sqlite3.ProgrammingError:
                # Запрос не возвращает строк (INSERT, UPDATE, DELETE)
                return []
        finally:
            conn.close()