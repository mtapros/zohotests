from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Tuple


class ZohoBooksClient:
    """Thin Zoho Books API client with clear extension points for endpoint/auth details."""

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


class SampleZohoBooksClient(ZohoBooksClient):
    """Offline fallback client for local testing and UI walkthroughs."""

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


def build_zoho_client(base_url: str, org_id: str, access_token: str, timeout_seconds: int = 30):
    sample_path = os.getenv("ZOHO_BOOKS_SAMPLE_FILE", "").strip()
    if sample_path:
        return SampleZohoBooksClient(sample_path)
    return ZohoBooksClient(
        base_url=base_url,
        org_id=org_id,
        access_token=access_token,
        timeout_seconds=timeout_seconds,
    )
