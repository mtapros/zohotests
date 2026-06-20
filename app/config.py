import os
from dataclasses import dataclass


@dataclass
class Settings:
    db_path: str = os.getenv("FORENSICS_DB_PATH", "forensics.db")
    zoho_base_url: str = os.getenv("ZOHO_BOOKS_BASE_URL", "https://www.zohoapis.com/books/v3")
    zoho_org_id: str = os.getenv("ZOHO_BOOKS_ORG_ID", "")
    zoho_access_token: str = os.getenv("ZOHO_BOOKS_ACCESS_TOKEN", "")
    zoho_timeout_seconds: int = int(os.getenv("ZOHO_BOOKS_TIMEOUT_SECONDS", "30"))


settings = Settings()
