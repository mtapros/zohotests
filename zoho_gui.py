from __future__ import annotations

import json
import os
import sqlite3
import threading
import tkinter as tk
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple


APP_TITLE = "Zoho Expense Forensic Inspector"
DEFAULT_DB_PATH = os.getenv("FORENSICS_DB_PATH", "forensics.db")
DEFAULT_BASE_URL = os.getenv("ZOHO_BOOKS_BASE_URL", "https://www.zohoapis.com/books/v3")
DEFAULT_ORG_ID = os.getenv("ZOHO_BOOKS_ORG_ID", "")
DEFAULT_ACCESS_TOKEN = os.getenv("ZOHO_BOOKS_ACCESS_TOKEN", "")
DEFAULT_TIMEOUT_SECONDS = int(os.getenv("ZOHO_BOOKS_TIMEOUT_SECONDS", "30"))
DEFAULT_SAMPLE_FILE = os.getenv("ZOHO_BOOKS_SAMPLE_FILE", "")


@dataclass
class ImportResult:
    run_id: int
    expenses_seen: int
    expenses_imported: int
    errors: int
    status: str
    notes: str


@dataclass
class Settings:
    db_path: str = DEFAULT_DB_PATH
    zoho_base_url: str = DEFAULT_BASE_URL
    zoho_org_id: str = DEFAULT_ORG_ID
    zoho_access_token: str = DEFAULT_ACCESS_TOKEN
    zoho_timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    sample_file: str = DEFAULT_SAMPLE_FILE


settings = Settings()


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

    def list_expenses(self, limit: int = 500) -> List[sqlite3.Row]:
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

    def field_catalog(self, limit: int = 5000) -> List[sqlite3.Row]:
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

    def recent_import_runs(self, limit: int = 20) -> List[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM import_runs ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()


def flatten_json(data: Any, prefix: str = "") -> Dict[str, Any]:
    flat: Dict[str, Any] = {}

    if isinstance(data, dict):
        if not data and prefix:
            flat[prefix] = data
        for key, value in data.items():
            path = f"{prefix}.{key}" if prefix else key
            flat.update(flatten_json(value, path))
    elif isinstance(data, list):
        if not data and prefix:
            flat[prefix] = data
        for idx, value in enumerate(data):
            path = f"{prefix}[{idx}]" if prefix else f"[{idx}]"
            flat.update(flatten_json(value, path))
    else:
        flat[prefix] = data

    return flat


def value_to_text(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


class ZohoBooksClient:
    def __init__(self, *, base_url: str, org_id: str, access_token: str, timeout_seconds: int = 30):
        self.base_url = base_url.rstrip("/")
        self.org_id = org_id
        self.access_token = access_token
        self.timeout_seconds = timeout_seconds

    def _request_json(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self.org_id:
            raise RuntimeError("ZOHO_BOOKS_ORG_ID is required for live Zoho import")
        if not self.access_token:
            raise RuntimeError("ZOHO_BOOKS_ACCESS_TOKEN is required for live Zoho import")

        merged = dict(params)
        merged["organization_id"] = self.org_id
        query = urllib.parse.urlencode(merged)
        url = f"{self.base_url}{path}?{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Zoho-oauthtoken {self.access_token}",
                "Accept": "application/json",
            },
        )

        with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)

    def list_expenses(self, page: int = 1, per_page: int = 200) -> Tuple[List[Dict[str, Any]], bool]:
        payload = self._request_json("/expenses", {"page": page, "per_page": per_page})
        expenses = payload.get("expenses", [])
        page_context = payload.get("page_context", {})
        has_more = bool(page_context.get("has_more_page"))
        return expenses, has_more

    def get_expense_detail(self, expense_id: str) -> Dict[str, Any]:
        payload = self._request_json(f"/expenses/{expense_id}", {})
        return payload.get("expense", payload)


class SampleZohoBooksClient:
    def __init__(self, sample_path: str):
        self.sample_path = sample_path

    def _load(self) -> Dict[str, Any]:
        with Path(self.sample_path).open("r", encoding="utf-8") as f:
            return json.load(f)

    def list_expenses(self, page: int = 1, per_page: int = 200) -> Tuple[List[Dict[str, Any]], bool]:
        payload = self._load()
        expenses = payload.get("expenses", [])
        start = (page - 1) * per_page
        end = start + per_page
        chunk = expenses[start:end]
        has_more = end < len(expenses)
        return chunk, has_more

    def get_expense_detail(self, expense_id: str) -> Dict[str, Any]:
        payload = self._load()
        for expense in payload.get("expenses", []):
            if str(expense.get("expense_id")) == str(expense_id):
                return expense
        raise RuntimeError(f"Sample expense not found: {expense_id}")


def build_zoho_client(current_settings: Settings):
    if current_settings.sample_file.strip():
        return SampleZohoBooksClient(current_settings.sample_file.strip())
    return ZohoBooksClient(
        base_url=current_settings.zoho_base_url,
        org_id=current_settings.zoho_org_id,
        access_token=current_settings.zoho_access_token,
        timeout_seconds=current_settings.zoho_timeout_seconds,
    )


def extract_attachments(expense_detail: Dict[str, Any]) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for key in ("attachments", "attachment", "documents", "document", "receipts", "receipt"):
        value = expense_detail.get(key)
        if isinstance(value, list):
            candidates.extend([v for v in value if isinstance(v, dict)])
        elif isinstance(value, dict):
            candidates.append(value)
    return candidates


def expense_identifier(expense_summary: Dict[str, Any]) -> str:
    return str(
        expense_summary.get("expense_id")
        or expense_summary.get("id")
        or expense_summary.get("expense_number")
    )


def import_expenses(db: ForensicsDB, zoho_client: Any, max_pages: Optional[int] = None) -> ImportResult:
    run_id = db.create_import_run(notes="Zoho expense import started")

    page = 1
    expenses_seen = 0
    expenses_imported = 0
    errors = 0

    try:
        while True:
            summaries, has_more = zoho_client.list_expenses(page=page)
            if not summaries:
                break

            for summary in summaries:
                expense_id = expense_identifier(summary)
                if not expense_id or expense_id == "None":
                    errors += 1
                    continue

                expenses_seen += 1
                try:
                    detail = zoho_client.get_expense_detail(expense_id)
                    db.upsert_expense(run_id=run_id, expense_id=expense_id, detail=detail)

                    flat = flatten_json(detail)
                    db.replace_fields(
                        expense_id,
                        [
                            {
                                "field_path": path,
                                "field_type": value_type(value),
                                "field_value_text": value_to_text(value),
                                "field_value_json": json.dumps(value, ensure_ascii=False),
                            }
                            for path, value in sorted(flat.items())
                        ],
                    )
                    db.replace_attachments(expense_id, extract_attachments(detail))
                    expenses_imported += 1
                except Exception:
                    errors += 1

            if max_pages and page >= max_pages:
                break
            if not has_more:
                break
            page += 1

        status = "success" if errors == 0 else "partial_success"
        notes = f"Imported {expenses_imported}/{expenses_seen} expenses"
        db.complete_import_run(
            run_id,
            status=status,
            expenses_seen=expenses_seen,
            expenses_imported=expenses_imported,
            errors=errors,
            notes=notes,
        )
        return ImportResult(
            run_id=run_id,
            expenses_seen=expenses_seen,
            expenses_imported=expenses_imported,
            errors=errors,
            status=status,
            notes=notes,
        )
    except Exception as exc:
        notes = f"Import failed: {exc}"
        db.complete_import_run(
            run_id,
            status="failed",
            expenses_seen=expenses_seen,
            expenses_imported=expenses_imported,
            errors=errors + 1,
            notes=notes,
        )
        raise


class ForensicsApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("1400x900")

        self.settings = Settings()
        self.db = ForensicsDB(self.settings.db_path)
        self.db.init_schema()

        self.selected_expense_id: Optional[str] = None

        self.status_var = tk.StringVar(value="Ready")
        self.db_path_var = tk.StringVar(value=self.settings.db_path)
        self.base_url_var = tk.StringVar(value=self.settings.zoho_base_url)
        self.org_id_var = tk.StringVar(value=self.settings.zoho_org_id)
        self.access_token_var = tk.StringVar(value=self.settings.zoho_access_token)
        self.timeout_var = tk.StringVar(value=str(self.settings.zoho_timeout_seconds))
        self.sample_file_var = tk.StringVar(value=self.settings.sample_file)
        self.max_pages_var = tk.StringVar(value="")
        self.search_var = tk.StringVar(value="")

        self._build_ui()
        self.refresh_all()

    def _build_ui(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)

        top = ttk.Frame(self.root, padding=10)
        top.grid(row=0, column=0, sticky="nsew")
        for i in range(6):
            top.columnconfigure(i, weight=1)

        ttk.Label(top, text="SQLite DB").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.db_path_var).grid(row=0, column=1, columnspan=4, sticky="ew", padx=(0, 6))
        ttk.Button(top, text="Browse DB", command=self.choose_db).grid(row=0, column=5, sticky="ew")

        ttk.Label(top, text="Sample JSON").grid(row=1, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.sample_file_var).grid(row=1, column=1, columnspan=4, sticky="ew", padx=(0, 6), pady=(6, 0))
        ttk.Button(top, text="Browse JSON", command=self.choose_sample).grid(row=1, column=5, sticky="ew", pady=(6, 0))

        ttk.Label(top, text="Zoho Base URL").grid(row=2, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.base_url_var).grid(row=2, column=1, sticky="ew", padx=(0, 6), pady=(6, 0))
        ttk.Label(top, text="Org ID").grid(row=2, column=2, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.org_id_var).grid(row=2, column=3, sticky="ew", padx=(0, 6), pady=(6, 0))
        ttk.Label(top, text="Timeout").grid(row=2, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.timeout_var).grid(row=2, column=5, sticky="ew", pady=(6, 0))

        ttk.Label(top, text="Access Token").grid(row=3, column=0, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.access_token_var, show="*").grid(row=3, column=1, columnspan=3, sticky="ew", padx=(0, 6), pady=(6, 0))
        ttk.Label(top, text="Max Pages").grid(row=3, column=4, sticky="w", pady=(6, 0))
        ttk.Entry(top, textvariable=self.max_pages_var).grid(row=3, column=5, sticky="ew", pady=(6, 0))

        buttons = ttk.Frame(top)
        buttons.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="Apply Settings", command=self.apply_settings).pack(side="left")
        ttk.Button(buttons, text="Run Import", command=self.run_import).pack(side="left", padx=6)
        ttk.Button(buttons, text="Refresh", command=self.refresh_all).pack(side="left")

        notebook = ttk.Notebook(self.root)
        notebook.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.expenses_tab = ttk.Frame(notebook, padding=8)
        self.fields_tab = ttk.Frame(notebook, padding=8)
        self.runs_tab = ttk.Frame(notebook, padding=8)
        notebook.add(self.expenses_tab, text="Expenses")
        notebook.add(self.fields_tab, text="Fields")
        notebook.add(self.runs_tab, text="Import Runs")

        self._build_expenses_tab()
        self._build_fields_tab()
        self._build_runs_tab()

        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief="sunken", padding=6)
        status_bar.grid(row=2, column=0, sticky="ew")

    def _build_expenses_tab(self) -> None:
        self.expenses_tab.columnconfigure(0, weight=3)
        self.expenses_tab.columnconfigure(1, weight=4)
        self.expenses_tab.rowconfigure(1, weight=1)

        search_frame = ttk.Frame(self.expenses_tab)
        search_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        search_frame.columnconfigure(1, weight=1)
        ttk.Label(search_frame, text="Search").grid(row=0, column=0, sticky="w")
        search_entry = ttk.Entry(search_frame, textvariable=self.search_var)
        search_entry.grid(row=0, column=1, sticky="ew", padx=(6, 6))
        search_entry.bind("<KeyRelease>", lambda _event: self.refresh_expenses())

        columns = ("expense_id", "date", "amount", "vendor", "reference", "imported_at")
        self.expense_tree = ttk.Treeview(self.expenses_tab, columns=columns, show="headings", height=18)
        headings = {
            "expense_id": "Expense ID",
            "date": "Date",
            "amount": "Amount",
            "vendor": "Vendor",
            "reference": "Reference",
            "imported_at": "Imported At",
        }
        for key, label in headings.items():
            self.expense_tree.heading(key, text=label)
        self.expense_tree.column("expense_id", width=120, anchor="w")
        self.expense_tree.column("date", width=100, anchor="w")
        self.expense_tree.column("amount", width=90, anchor="e")
        self.expense_tree.column("vendor", width=180, anchor="w")
        self.expense_tree.column("reference", width=140, anchor="w")
        self.expense_tree.column("imported_at", width=180, anchor="w")
        self.expense_tree.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.expense_tree.bind("<<TreeviewSelect>>", self.on_expense_select)

        left_scroll = ttk.Scrollbar(self.expenses_tab, orient="vertical", command=self.expense_tree.yview)
        self.expense_tree.configure(yscrollcommand=left_scroll.set)
        left_scroll.grid(row=1, column=0, sticky="nse")

        detail_pane = ttk.Panedwindow(self.expenses_tab, orient="vertical")
        detail_pane.grid(row=1, column=1, sticky="nsew")

        fields_frame = ttk.Labelframe(detail_pane, text="Flattened Fields")
        attachments_frame = ttk.Labelframe(detail_pane, text="Attachments")
        raw_frame = ttk.Labelframe(detail_pane, text="Raw JSON")
        detail_pane.add(fields_frame, weight=3)
        detail_pane.add(attachments_frame, weight=1)
        detail_pane.add(raw_frame, weight=3)

        fields_frame.columnconfigure(0, weight=1)
        fields_frame.rowconfigure(0, weight=1)
        self.fields_tree = ttk.Treeview(fields_frame, columns=("path", "type", "value"), show="headings")
        self.fields_tree.heading("path", text="Field Path")
        self.fields_tree.heading("type", text="Type")
        self.fields_tree.heading("value", text="Value")
        self.fields_tree.column("path", width=260, anchor="w")
        self.fields_tree.column("type", width=90, anchor="w")
        self.fields_tree.column("value", width=420, anchor="w")
        self.fields_tree.grid(row=0, column=0, sticky="nsew")
        fields_scroll = ttk.Scrollbar(fields_frame, orient="vertical", command=self.fields_tree.yview)
        self.fields_tree.configure(yscrollcommand=fields_scroll.set)
        fields_scroll.grid(row=0, column=1, sticky="ns")

        attachments_frame.columnconfigure(0, weight=1)
        attachments_frame.rowconfigure(0, weight=1)
        self.attachments_tree = ttk.Treeview(attachments_frame, columns=("id", "name", "type"), show="headings")
        self.attachments_tree.heading("id", text="Attachment ID")
        self.attachments_tree.heading("name", text="File Name")
        self.attachments_tree.heading("type", text="Type")
        self.attachments_tree.column("id", width=140, anchor="w")
        self.attachments_tree.column("name", width=280, anchor="w")
        self.attachments_tree.column("type", width=120, anchor="w")
        self.attachments_tree.grid(row=0, column=0, sticky="nsew")
        attach_scroll = ttk.Scrollbar(attachments_frame, orient="vertical", command=self.attachments_tree.yview)
        self.attachments_tree.configure(yscrollcommand=attach_scroll.set)
        attach_scroll.grid(row=0, column=1, sticky="ns")

        raw_frame.columnconfigure(0, weight=1)
        raw_frame.rowconfigure(0, weight=1)
        self.raw_text = tk.Text(raw_frame, wrap="none")
        self.raw_text.grid(row=0, column=0, sticky="nsew")
        raw_y = ttk.Scrollbar(raw_frame, orient="vertical", command=self.raw_text.yview)
        raw_x = ttk.Scrollbar(raw_frame, orient="horizontal", command=self.raw_text.xview)
        self.raw_text.configure(yscrollcommand=raw_y.set, xscrollcommand=raw_x.set)
        raw_y.grid(row=0, column=1, sticky="ns")
        raw_x.grid(row=1, column=0, sticky="ew")

    def _build_fields_tab(self) -> None:
        self.fields_tab.columnconfigure(0, weight=1)
        self.fields_tab.rowconfigure(0, weight=1)
        self.catalog_tree = ttk.Treeview(self.fields_tab, columns=("path", "count", "type", "sample"), show="headings")
        self.catalog_tree.heading("path", text="Field Path")
        self.catalog_tree.heading("count", text="Expense Count")
        self.catalog_tree.heading("type", text="Type")
        self.catalog_tree.heading("sample", text="Sample Value")
        self.catalog_tree.column("path", width=320, anchor="w")
        self.catalog_tree.column("count", width=110, anchor="e")
        self.catalog_tree.column("type", width=100, anchor="w")
        self.catalog_tree.column("sample", width=700, anchor="w")
        self.catalog_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.fields_tab, orient="vertical", command=self.catalog_tree.yview)
        self.catalog_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

    def _build_runs_tab(self) -> None:
        self.runs_tab.columnconfigure(0, weight=1)
        self.runs_tab.rowconfigure(0, weight=1)
        self.runs_tree = ttk.Treeview(
            self.runs_tab,
            columns=("id", "started", "completed", "status", "seen", "imported", "errors", "notes"),
            show="headings",
        )
        labels = {
            "id": "Run ID",
            "started": "Started",
            "completed": "Completed",
            "status": "Status",
            "seen": "Seen",
            "imported": "Imported",
            "errors": "Errors",
            "notes": "Notes",
        }
        for key, label in labels.items():
            self.runs_tree.heading(key, text=label)
        self.runs_tree.column("id", width=70, anchor="e")
        self.runs_tree.column("started", width=180, anchor="w")
        self.runs_tree.column("completed", width=180, anchor="w")
        self.runs_tree.column("status", width=120, anchor="w")
        self.runs_tree.column("seen", width=80, anchor="e")
        self.runs_tree.column("imported", width=90, anchor="e")
        self.runs_tree.column("errors", width=70, anchor="e")
        self.runs_tree.column("notes", width=600, anchor="w")
        self.runs_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(self.runs_tab, orient="vertical", command=self.runs_tree.yview)
        self.runs_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")

    def choose_db(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Choose SQLite database",
            defaultextension=".db",
            filetypes=[("SQLite DB", "*.db *.sqlite *.sqlite3"), ("All Files", "*.*")],
        )
        if path:
            self.db_path_var.set(path)

    def choose_sample(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose sample Zoho JSON file",
            filetypes=[("JSON Files", "*.json"), ("All Files", "*.*")],
        )
        if path:
            self.sample_file_var.set(path)

    def apply_settings(self) -> None:
        try:
            timeout = int(self.timeout_var.get().strip() or "30")
        except ValueError:
            messagebox.showerror("Invalid timeout", "Timeout must be a whole number.")
            return

        self.settings = Settings(
            db_path=self.db_path_var.get().strip() or DEFAULT_DB_PATH,
            zoho_base_url=self.base_url_var.get().strip() or DEFAULT_BASE_URL,
            zoho_org_id=self.org_id_var.get().strip(),
            zoho_access_token=self.access_token_var.get().strip(),
            zoho_timeout_seconds=timeout,
            sample_file=self.sample_file_var.get().strip(),
        )
        self.db = ForensicsDB(self.settings.db_path)
        self.db.init_schema()
        self.status_var.set(f"Using database: {self.settings.db_path}")
        self.refresh_all()

    def parse_max_pages(self) -> Optional[int]:
        raw = self.max_pages_var.get().strip()
        if not raw:
            return None
        value = int(raw)
        if value <= 0:
            raise ValueError("Max pages must be greater than zero.")
        return value

    def run_import(self) -> None:
        try:
            self.apply_settings()
            max_pages = self.parse_max_pages()
        except ValueError as exc:
            messagebox.showerror("Invalid max pages", str(exc))
            return

        self.status_var.set("Import running...")

        def worker() -> None:
            try:
                client = build_zoho_client(self.settings)
                result = import_expenses(self.db, client, max_pages=max_pages)
                self.root.after(0, lambda: self.on_import_success(result))
            except Exception as exc:
                self.root.after(0, lambda: self.on_import_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def on_import_success(self, result: ImportResult) -> None:
        self.status_var.set(
            f"Import run {result.run_id} finished: {result.status}, "
            f"seen={result.expenses_seen}, imported={result.expenses_imported}, errors={result.errors}"
        )
        self.refresh_all()
        messagebox.showinfo(
            "Import complete",
            f"Run {result.run_id}\nStatus: {result.status}\nSeen: {result.expenses_seen}\n"
            f"Imported: {result.expenses_imported}\nErrors: {result.errors}",
        )

    def on_import_error(self, exc: Exception) -> None:
        self.status_var.set(f"Import failed: {exc}")
        self.refresh_all()
        messagebox.showerror("Import failed", str(exc))

    def refresh_all(self) -> None:
        self.refresh_expenses()
        self.refresh_catalog()
        self.refresh_runs()
        if self.selected_expense_id:
            self.load_expense_detail(self.selected_expense_id)

    def refresh_expenses(self) -> None:
        for item in self.expense_tree.get_children():
            self.expense_tree.delete(item)

        search = self.search_var.get().strip().lower()
        rows = self.db.list_expenses(limit=500)
        for row in rows:
            haystack = " ".join(
                str(row[key] or "")
                for key in ("zoho_expense_id", "date", "vendor_name", "description", "reference_number", "imported_at")
            ).lower()
            if search and search not in haystack:
                continue
            self.expense_tree.insert(
                "",
                "end",
                iid=str(row["zoho_expense_id"]),
                values=(
                    row["zoho_expense_id"],
                    row["date"] or "",
                    row["amount"] if row["amount"] is not None else "",
                    row["vendor_name"] or "",
                    row["reference_number"] or "",
                    row["imported_at"] or "",
                ),
            )

    def refresh_catalog(self) -> None:
        for item in self.catalog_tree.get_children():
            self.catalog_tree.delete(item)
        for row in self.db.field_catalog(limit=5000):
            self.catalog_tree.insert(
                "",
                "end",
                values=(
                    row["field_path"],
                    row["expense_count"],
                    row["field_type"] or "",
                    row["sample_value"] or "",
                ),
            )

    def refresh_runs(self) -> None:
        for item in self.runs_tree.get_children():
            self.runs_tree.delete(item)
        for row in self.db.recent_import_runs(limit=20):
            self.runs_tree.insert(
                "",
                "end",
                values=(
                    row["id"],
                    row["started_at"] or "",
                    row["completed_at"] or "",
                    row["status"] or "",
                    row["expenses_seen"],
                    row["expenses_imported"],
                    row["errors"],
                    row["notes"] or "",
                ),
            )

    def on_expense_select(self, _event=None) -> None:
        selection = self.expense_tree.selection()
        if not selection:
            return
        expense_id = selection[0]
        self.selected_expense_id = expense_id
        self.load_expense_detail(expense_id)

    def load_expense_detail(self, expense_id: str) -> None:
        expense = self.db.get_expense(expense_id)
        fields = self.db.get_expense_fields(expense_id)
        attachments = self.db.get_attachments(expense_id)

        for item in self.fields_tree.get_children():
            self.fields_tree.delete(item)
        for row in fields:
            self.fields_tree.insert(
                "",
                "end",
                values=(row["field_path"], row["field_type"], row["field_value_text"] or ""),
            )

        for item in self.attachments_tree.get_children():
            self.attachments_tree.delete(item)
        for row in attachments:
            self.attachments_tree.insert(
                "",
                "end",
                values=(row["attachment_id"] or "", row["file_name"] or "", row["attachment_type"] or ""),
            )

        self.raw_text.delete("1.0", tk.END)
        if expense:
            try:
                pretty = json.dumps(json.loads(expense["raw_json"]), indent=2, ensure_ascii=False)
            except Exception:
                pretty = expense["raw_json"]
            self.raw_text.insert("1.0", pretty)


def main() -> None:
    root = tk.Tk()
    app = ForensicsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
