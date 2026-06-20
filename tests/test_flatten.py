import unittest

from app.flatten import flatten_json


class FlattenJsonTests(unittest.TestCase):
    def test_nested_objects_and_lists(self):
        data = {
            "vendor_id": "123",
            "line_items": [{"account_id": "A1", "amount": 10.5}],
            "custom_fields": [{"value": "x"}],
        }

        flat = flatten_json(data)

        self.assertEqual(flat["vendor_id"], "123")
        self.assertEqual(flat["line_items[0].account_id"], "A1")
        self.assertEqual(flat["line_items[0].amount"], 10.5)
        self.assertEqual(flat["custom_fields[0].value"], "x")


if __name__ == "__main__":
    unittest.main()
