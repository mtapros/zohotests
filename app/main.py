from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import ForensicsDB
from app.importer import import_expenses
from app.zoho_client import build_zoho_client

app = FastAPI(title="Zoho Expense Forensic Inspector")

db = ForensicsDB(settings.db_path)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@app.on_event("startup")
def startup() -> None:
    db.init_schema()


@app.get("/", response_class=HTMLResponse)
def home() -> RedirectResponse:
    return RedirectResponse(url="/expenses", status_code=302)


@app.get("/expenses", response_class=HTMLResponse)
def expenses(request: Request):
    return templates.TemplateResponse(
        "expenses.html",
        {
            "request": request,
            "expenses": db.list_expenses(limit=500),
            "runs": db.recent_import_runs(limit=20),
        },
    )


@app.post("/import")
def run_import(max_pages: Optional[int] = Form(default=None)):
    client = build_zoho_client(
        base_url=settings.zoho_base_url,
        org_id=settings.zoho_org_id,
        access_token=settings.zoho_access_token,
        timeout_seconds=settings.zoho_timeout_seconds,
    )
    import_expenses(db, client, max_pages=max_pages)
    return RedirectResponse(url="/expenses", status_code=303)


@app.get("/expenses/{expense_id}", response_class=HTMLResponse)
def expense_detail(request: Request, expense_id: str):
    expense = db.get_expense(expense_id)
    fields = db.get_expense_fields(expense_id)
    attachments = db.get_attachments(expense_id)
    raw_pretty = "{}"
    if expense:
        raw_pretty = json.dumps(json.loads(expense["raw_json"]), indent=2, ensure_ascii=False)
    return templates.TemplateResponse(
        "expense_detail.html",
        {
            "request": request,
            "expense": expense,
            "fields": fields,
            "attachments": attachments,
            "raw_pretty": raw_pretty,
        },
    )


@app.get("/fields", response_class=HTMLResponse)
def fields(request: Request):
    return templates.TemplateResponse(
        "fields.html",
        {
            "request": request,
            "fields": db.field_catalog(limit=5000),
        },
    )
