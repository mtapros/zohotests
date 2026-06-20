import tempfile
import unittest

from app.database import ForensicsDB
from app.importer import import_expenses


class FakeZohoClient:
    def list_expenses(self, page=1, per_page=200):
        if page > 1:
            return [], False
        return [{"expense_id": "E1"}], False

    def get_expense_detail(self, expense_id):
        return {
            "expense_id": expense_id,
            "date": "2026-06-20",
            "amount": 12.34,
            "vendor_name": "Vendor",
            "description": "Test",
            "attachments": [{"attachment_id": "A1", "file_name": "receipt.pdf", "type": "pdf"}],
        }


class ImporterTests(unittest.TestCase):
    def test_import_populates_expenses_fields_attachments(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = ForensicsDB(tmp.name)
            db.init_schema()

            result = import_expenses(db, FakeZohoClient())

            self.assertEqual(result.expenses_imported, 1)
            self.assertEqual(len(db.list_expenses()), 1)
            self.assertGreater(len(db.get_expense_fields("E1")), 0)
            self.assertEqual(len(db.get_attachments("E1")), 1)


if __name__ == "__main__":
    unittest.main()
