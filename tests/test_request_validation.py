
"""Phase 10 Prompt 5 - Request validation & payload protection tests."""
from __future__ import annotations
import math, os, sys, tempfile, threading, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
_fd, _db_path = tempfile.mkstemp(prefix="oggy_req_val_", suffix=".db")
os.close(_fd)
import config; config.DB_PATH = _db_path
import database.db as db; db.DB_PATH = _db_path; db._tls = threading.local(); db.init_db()

from reseller_api.services.request_validator import (
    ValidationError, MAX_CLIENT_ORDER_ID_LEN, MAX_EVENTS_ARRAY_LEN,
    MAX_PAGE_SIZE, MAX_WEBHOOK_URL_LEN, MAX_SEARCH_LEN, DEFAULT_PAGE_SIZE,
    validate_events_array, validate_page_size, validate_sort_field,
    validate_string_length, reject_unexpected_fields,
)
from reseller_api.middleware.payload_guard import _HARD_MAX_BYTES, _scan_nan_inf, _PayloadError

_RAISE_ON_NAN = (Exception,)  # catches both ApiError and _PayloadError


class TestStringLength(unittest.TestCase):
    def test_valid(self): self.assertEqual(validate_string_length("abc", "f", 100), "abc")
    def test_at_max(self):
        v = "x" * MAX_CLIENT_ORDER_ID_LEN
        self.assertEqual(validate_string_length(v, "f", MAX_CLIENT_ORDER_ID_LEN), v)
    def test_too_long(self):
        with self.assertRaises(ValidationError):
            validate_string_length("x"*(MAX_CLIENT_ORDER_ID_LEN+1), "f", MAX_CLIENT_ORDER_ID_LEN, True)
    def test_webhook_too_long(self):
        with self.assertRaises(ValidationError):
            validate_string_length("x"*(MAX_WEBHOOK_URL_LEN+1), "url", MAX_WEBHOOK_URL_LEN, True)
    def test_search_too_long(self):
        with self.assertRaises(ValidationError):
            validate_string_length("q"*(MAX_SEARCH_LEN+1), "q", MAX_SEARCH_LEN)
    def test_non_string(self):
        with self.assertRaises(ValidationError): validate_string_length(123, "f", 100)
    def test_none_optional(self): self.assertIsNone(validate_string_length(None, "f", 100))
    def test_none_required(self):
        with self.assertRaises(ValidationError): validate_string_length(None, "f", 100, True)
    def test_empty_required(self):
        with self.assertRaises(ValidationError): validate_string_length("", "f", 100, True)


class TestPageSize(unittest.TestCase):
    def test_default_none(self): self.assertEqual(validate_page_size(None), DEFAULT_PAGE_SIZE)
    def test_valid(self): self.assertEqual(validate_page_size(10), 10)
    def test_max(self): self.assertEqual(validate_page_size(MAX_PAGE_SIZE), MAX_PAGE_SIZE)
    def test_above_max(self):
        with self.assertRaises(ValidationError): validate_page_size(MAX_PAGE_SIZE+1)
    def test_zero(self):
        with self.assertRaises(ValidationError): validate_page_size(0)
    def test_negative(self):
        with self.assertRaises(ValidationError): validate_page_size(-1)
    def test_huge(self):
        with self.assertRaises(ValidationError): validate_page_size(999999)
    def test_non_int(self):
        with self.assertRaises(ValidationError): validate_page_size("all")


class TestSortField(unittest.TestCase):
    def test_valid(self): self.assertIsNotNone(validate_sort_field("created_at"))
    def test_invalid(self):
        with self.assertRaises(ValidationError): validate_sort_field("secret; DROP--")
    def test_none(self): self.assertIsNone(validate_sort_field(None))
    def test_non_string(self):
        with self.assertRaises(ValidationError): validate_sort_field(123)
    def test_arbitrary_db_field(self):
        with self.assertRaises(ValidationError): validate_sort_field("wallet_balance")


class TestEventsArray(unittest.TestCase):
    def test_valid(self): self.assertEqual(len(validate_events_array(["order.created"])), 1)
    def test_at_max(self): self.assertEqual(len(validate_events_array(["e"]*MAX_EVENTS_ARRAY_LEN)), MAX_EVENTS_ARRAY_LEN)
    def test_above_max(self):
        with self.assertRaises(ValidationError): validate_events_array(["e"]*(MAX_EVENTS_ARRAY_LEN+1))
    def test_none(self): self.assertEqual(validate_events_array(None), [])
    def test_non_list(self):
        with self.assertRaises(ValidationError): validate_events_array("order.created")


class TestUnexpectedFields(unittest.TestCase):
    def test_valid(self): reject_unexpected_fields({"product_id": "P-001"}, {"product_id", "quantity"})
    def test_unexpected(self):
        with self.assertRaises(ValidationError) as c:
            reject_unexpected_fields({"product_id": "P-001", "__admin": True}, {"product_id"})
        self.assertIn("__admin", c.exception.message)
    def test_non_dict(self): reject_unexpected_fields("not-a-dict", {"a"})


class TestNanInfScanner(unittest.TestCase):
    def test_clean_dict(self): _scan_nan_inf({"quantity": 5, "price": 10.5})
    def test_clean_nested(self): _scan_nan_inf({"items": [{"value": 1.5}]})
    def test_clean_int(self): _scan_nan_inf({"n": 42})
    def test_nan_raises(self):
        with self.assertRaises(_RAISE_ON_NAN): _scan_nan_inf({"v": math.nan})
    def test_inf_raises(self):
        with self.assertRaises(_RAISE_ON_NAN): _scan_nan_inf({"v": math.inf})
    def test_neg_inf_raises(self):
        with self.assertRaises(_RAISE_ON_NAN): _scan_nan_inf({"v": -math.inf})
    def test_nested_nan_raises(self):
        with self.assertRaises(_RAISE_ON_NAN): _scan_nan_inf({"a": {"b": math.nan}})


class TestBodySizeConstant(unittest.TestCase):
    def test_is_512kb(self): self.assertEqual(_HARD_MAX_BYTES, 512*1024)
    def test_reasonable(self):
        self.assertGreater(_HARD_MAX_BYTES, 64*1024)
        self.assertLess(_HARD_MAX_BYTES, 10*1024*1024)


if __name__ == "__main__":
    unittest.main()
