from __future__ import annotations

import argparse

from app.config import settings
from app.database import ForensicsDB
from app.importer import import_expenses
from app.zoho_client import build_zoho_client


def main() -> None:
    parser = argparse.ArgumentParser(description="Zoho expense forensic importer")
    parser.add_argument("command", choices=["import"], help="Command to run")
    parser.add_argument("--max-pages", type=int, default=None, help="Max pages to import")
    parser.add_argument("--db", default=settings.db_path, help="SQLite DB path")
    args = parser.parse_args()

    if args.command == "import":
        db = ForensicsDB(args.db)
        db.init_schema()
        client = build_zoho_client(
            base_url=settings.zoho_base_url,
            org_id=settings.zoho_org_id,
            access_token=settings.zoho_access_token,
            timeout_seconds=settings.zoho_timeout_seconds,
        )
        result = import_expenses(db, client, max_pages=args.max_pages)
        print(
            f"Import run {result.run_id}: status={result.status}, "
            f"seen={result.expenses_seen}, imported={result.expenses_imported}, errors={result.errors}"
        )


if __name__ == "__main__":
    main()
