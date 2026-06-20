from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ForensicsDB:
    def __init__(self, db_path: str):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS import_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    expenses_seen INTEGER NOT NULL DEFAULT 0,
                    expenses_imported INTEGER NOT NULL DEFAULT 0,
                    errors INTEGER NOT NULL DEFAULT 0,
                    notes TEXT
                );

                CREATE TABLE IF NOT EXISTS expenses (
                    zoho_expense_id TEXT PRIMARY KEY,
                    date TEXT,
                    amount REAL,
                    vendor_name TEXT,
                    vendor_id TEXT,
                    description TEXT,
                    reference_number TEXT,
                    raw_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL,
                    last_run_id INTEGER,
                    FOREIGN KEY(last_run_id) REFERENCES import_runs(id)
                );

                CREATE TABLE IF NOT EXISTS expense_fields (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zoho_expense_id TEXT NOT NULL,
                    field_path TEXT NOT NULL,
                    field_type TEXT NOT NULL,
                    field_value_text TEXT,
                    field_value_json TEXT,
                    present_on_record INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY(zoho_expense_id) REFERENCES expenses(zoho_expense_id) ON DELETE CASCADE,
                    UNIQUE(zoho_expense_id, field_path)
                );

                CREATE INDEX IF NOT EXISTS idx_expense_fields_path ON expense_fields(field_path);

                CREATE TABLE IF NOT EXISTS attachments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zoho_expense_id TEXT NOT NULL,
                    attachment_id TEXT,
                    file_name TEXT,
                    attachment_type TEXT,
                    metadata_json TEXT,
                    FOREIGN KEY(zoho_expense_id) REFERENCES expenses(zoho_expense_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_attachments_expense ON attachments(zoho_expense_id);
                """
            )

    def create_import_run(self, notes: Optional[str] = None) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO import_runs (started_at, status, notes) VALUES (?, ?, ?)",
                (utc_now_iso(), "running", notes),
            )
            return int(cur.lastrowid)

    def complete_import_run(
        self,
        run_id: int,
        *,
        status: str,
        expenses_seen: int,
        expenses_imported: int,
        errors: int,
        notes: Optional[str] = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE import_runs
                SET completed_at = ?, status = ?, expenses_seen = ?, expenses_imported = ?, errors = ?, notes = ?
                WHERE id = ?
                """,
                (utc_now_iso(), status, expenses_seen, expenses_imported, errors, notes, run_id),
            )

    def upsert_expense(self, *, run_id: int, expense_id: str, detail: Dict[str, Any]) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO expenses (
                    zoho_expense_id, date, amount, vendor_name, vendor_id, description,
                    reference_number, raw_json, imported_at, last_run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(zoho_expense_id) DO UPDATE SET
                    date=excluded.date,
                    amount=excluded.amount,
                    vendor_name=excluded.vendor_name,
                    vendor_id=excluded.vendor_id,
                    description=excluded.description,
                    reference_number=excluded.reference_number,
                    raw_json=excluded.raw_json,
                    imported_at=excluded.imported_at,
                    last_run_id=excluded.last_run_id
                """,
                (
                    expense_id,
                    detail.get("date"),
                    detail.get("amount"),
                    detail.get("vendor_name") or detail.get("vendor") or detail.get("payee"),
                    detail.get("vendor_id"),
                    detail.get("description"),
                    detail.get("reference_number"),
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utc_now_iso(),
                    run_id,
                ),
            )

    def replace_fields(self, expense_id: str, fields: Iterable[Dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM expense_fields WHERE zoho_expense_id = ?", (expense_id,))
            conn.executemany(
                """
                INSERT INTO expense_fields (
                    zoho_expense_id, field_path, field_type, field_value_text, field_value_json, present_on_record
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                [
                    (
                        expense_id,
                        item["field_path"],
                        item["field_type"],
                        item.get("field_value_text"),
                        item.get("field_value_json"),
                    )
                    for item in fields
                ],
            )

    def replace_attachments(self, expense_id: str, attachments: List[Dict[str, Any]]) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM attachments WHERE zoho_expense_id = ?", (expense_id,))
            conn.executemany(
                """
                INSERT INTO attachments (
                    zoho_expense_id, attachment_id, file_name, attachment_type, metadata_json
                ) VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        expense_id,
                        att.get("attachment_id") or att.get("document_id") or att.get("id"),
                        att.get("file_name") or att.get("name"),
                        att.get("attachment_type") or att.get("type") or "unknown",
                        json.dumps(att, ensure_ascii=False, sort_keys=True),
                    )
                    for att in attachments
                ],
            )

    def list_expenses(self, limit: int = 100) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT zoho_expense_id, date, amount, vendor_name, description, reference_number, imported_at
                FROM expenses
                ORDER BY date DESC, zoho_expense_id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def get_expense(self, expense_id: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM expenses WHERE zoho_expense_id = ?",
                (expense_id,),
            ).fetchone()

    def get_expense_fields(self, expense_id: str) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT field_path, field_type, field_value_text, field_value_json
                FROM expense_fields
                WHERE zoho_expense_id = ?
                ORDER BY field_path
                """,
                (expense_id,),
            ).fetchall()

    def get_attachments(self, expense_id: str) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT attachment_id, file_name, attachment_type, metadata_json
                FROM attachments
                WHERE zoho_expense_id = ?
                ORDER BY id
                """,
                (expense_id,),
            ).fetchall()

    def field_catalog(self, limit: int = 1000) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                """
                SELECT
                    field_path,
                    COUNT(DISTINCT zoho_expense_id) AS expense_count,
                    MIN(field_type) AS field_type,
                    MIN(field_value_text) AS sample_value
                FROM expense_fields
                GROUP BY field_path
                ORDER BY expense_count DESC, field_path ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()

    def recent_import_runs(self, limit: int = 10) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM import_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
