# Zoho Expense Forensic Inspector

Small local/offline FastAPI app for importing Zoho Books expenses into SQLite and inspecting them for forensic reverse-mapping.

## What it does

- Imports expenses from Zoho Books with pagination + per-expense detail fetch.
- Stores full raw JSON for each expense.
- Flattens nested fields into searchable field paths (`line_items[0].account_id`, etc).
- Captures optional attachment metadata if present.
- Provides a minimal web UI for expense browsing, detail inspection, and field cataloging.

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Configuration

Set environment variables:

- `FORENSICS_DB_PATH` (optional, default `forensics.db`)
- `ZOHO_BOOKS_BASE_URL` (optional, default `https://www.zohoapis.com/books/v3`)
- `ZOHO_BOOKS_ORG_ID` (required for live Zoho imports)
- `ZOHO_BOOKS_ACCESS_TOKEN` (required for live Zoho imports)
- `ZOHO_BOOKS_TIMEOUT_SECONDS` (optional, default `30`)

Optional offline mode:

- `ZOHO_BOOKS_SAMPLE_FILE=/absolute/path/to/sample_expenses.json`

When `ZOHO_BOOKS_SAMPLE_FILE` is set, imports run from the local JSON instead of live API calls.

## Run the app

```bash
uvicorn app.main:app --reload
```

Open:
- `http://127.0.0.1:8000/expenses`
- `http://127.0.0.1:8000/fields`

## Trigger import

### UI
- On `/expenses`, use **Run Import**.

### CLI

```bash
python -m app.cli import
# optional
python -m app.cli import --max-pages 2 --db /absolute/path/forensics.db
```

## Pages

- `/expenses`: imported expense list + import run history + import button.
- `/expenses/{id}`: expense summary, flattened fields, raw JSON, attachment metadata.
- `/fields`: catalog of discovered field paths with counts and sample values.

## SQLite schema

- `expenses`: summary columns + raw JSON + import metadata
- `expense_fields`: flattened per-expense field paths and values
- `attachments`: attachment metadata snapshots
- `import_runs`: run timing, status, counts, and notes

## Limitations / next steps

- Zoho auth token refresh flow is not implemented in v1.
- Endpoint details are intentionally isolated in `app/zoho_client.py` for easy wiring updates.
- Attachment binary download/storage is not yet implemented (metadata only).
- UI is intentionally minimal and optimized for quick forensic browsing.
