"""Работа с базой данных SQLite: аккаунт Яндекс Диска и история задач выгрузки."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import DB_PATH


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, db_path: Optional[str] = None):
        self.db_path = str(db_path or DB_PATH)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def init_db(self) -> None:
        with self._get_conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    token TEXT NOT NULL,
                    login TEXT DEFAULT '',
                    total_bytes INTEGER DEFAULT 0,
                    used_bytes INTEGER DEFAULT 0,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    dest TEXT NOT NULL,
                    items TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    total_files INTEGER DEFAULT 0,
                    total_bytes INTEGER DEFAULT 0,
                    done_files INTEGER DEFAULT 0,
                    done_bytes INTEGER DEFAULT 0,
                    failed_files INTEGER DEFAULT 0,
                    error TEXT DEFAULT '',
                    keep_parts BOOLEAN DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    path TEXT NOT NULL,
                    error TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_task_errors_task_id ON task_errors(task_id);
                CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
                """
            )
            try:
                conn.execute("ALTER TABLE tasks ADD COLUMN keep_parts BOOLEAN DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    # --- Account ---

    def get_account(self) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id = 1").fetchone()
            if not row:
                return None
            return dict(row)

    def save_account(self, token: str, login: str, total_bytes: int, used_bytes: int) -> Dict[str, Any]:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO accounts (id, token, login, total_bytes, used_bytes, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    token = excluded.token,
                    login = excluded.login,
                    total_bytes = excluded.total_bytes,
                    used_bytes = excluded.used_bytes,
                    updated_at = excluded.updated_at
                """,
                (token, login, total_bytes, used_bytes, now),
            )
        return {
            "token": token,
            "login": login,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "updated_at": now,
        }

    def delete_account(self) -> None:
        with self._get_conn() as conn:
            conn.execute("DELETE FROM accounts WHERE id = 1")

    # --- Tasks ---

    def create_task(self, task_id: str, dest: str, items: List[Dict[str, Any]], keep_parts: bool = False) -> Dict[str, Any]:
        now = _utc_now_iso()
        items_json = json.dumps(items, ensure_ascii=False)
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, dest, items, status,
                    total_files, total_bytes, done_files, done_bytes, failed_files,
                    error, keep_parts, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', 0, 0, 0, 0, 0, '', ?, ?, ?)
                """,
                (task_id, dest, items_json, 1 if keep_parts else 0, now, now),
            )
        return {
            "id": task_id,
            "dest": dest,
            "items": items,
            "status": "pending",
            "total_files": 0,
            "total_bytes": 0,
            "done_files": 0,
            "done_bytes": 0,
            "failed_files": 0,
            "error": "",
            "keep_parts": bool(keep_parts),
            "created_at": now,
            "updated_at": now,
        }

    def update_task_scan(self, task_id: str, total_files: int, total_bytes: int, status: str = "running") -> None:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    total_files = ?,
                    total_bytes = ?,
                    status = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (total_files, total_bytes, status, now, task_id),
            )

    def update_task_progress(self, task_id: str, done_files: int, done_bytes: int, failed_files: int) -> None:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    done_files = ?,
                    done_bytes = ?,
                    failed_files = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (done_files, done_bytes, failed_files, now, task_id),
            )

    def update_task_status(self, task_id: str, status: str, error: str = "") -> None:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, error, now, task_id),
            )

    def finalize_task(
        self,
        task_id: str,
        status: str,
        done_files: int,
        done_bytes: int,
        failed_files: int,
        error: str = "",
    ) -> None:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                UPDATE tasks SET
                    status = ?,
                    done_files = ?,
                    done_bytes = ?,
                    failed_files = ?,
                    error = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (status, done_files, done_bytes, failed_files, error, now, task_id),
            )

    def record_task_error(self, task_id: str, path: str, error: str) -> None:
        now = _utc_now_iso()
        with self._get_conn() as conn:
            conn.execute(
                """
                INSERT INTO task_errors (task_id, path, error, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (task_id, path, str(error)[:300], now),
            )

    def get_task_errors(self, task_id: str, limit: int = 50) -> List[Dict[str, str]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT path, error FROM task_errors
                WHERE task_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (task_id, limit),
            ).fetchall()
            return [{"path": r["path"], "error": r["error"]} for r in rows]

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            data = dict(row)
            data["items"] = json.loads(data["items"]) if data.get("items") else []
            data["errors"] = self.get_task_errors(task_id)
            data["keep_parts"] = bool(data.get("keep_parts", 0))
            return data

    def list_tasks(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self._get_conn() as conn:
            rows = conn.execute(
                """
                SELECT * FROM tasks
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result = []
            for r in rows:
                data = dict(r)
                data["items"] = json.loads(data["items"]) if data.get("items") else []
                data["errors"] = self.get_task_errors(data["id"])
                data["keep_parts"] = bool(data.get("keep_parts", 0))
                result.append(data)
            return result

    def delete_task(self, task_id: str) -> bool:
        with self._get_conn() as conn:
            cur = conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.execute("DELETE FROM task_errors WHERE task_id = ?", (task_id,))
            return cur.rowcount > 0

    def get_last_dest(self) -> Optional[str]:
        """Возвращает последнюю использованную папку назначения из истории задач."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT dest FROM tasks ORDER BY created_at DESC LIMIT 1").fetchone()
            if row and row["dest"]:
                return str(row["dest"])
            return None


# Глобальный инстанс БД
db = Database()
