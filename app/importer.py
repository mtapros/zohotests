from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.database import ForensicsDB
from app.flatten import flatten_json, value_to_text, value_type


@dataclass
class ImportResult:
    run_id: int
    expenses_seen: int
    expenses_imported: int
    errors: int
    status: str
    notes: str


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
