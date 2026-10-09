"""No test connects to production MongoDB or Access."""
import importlib
import sys
from unittest.mock import patch
import mongomock
import pytest


@pytest.fixture
def application(monkeypatch):
    monkeypatch.setenv("MONGO_URI", "mongodb://localhost/po_test")
    monkeypatch.setenv("BOOTSTRAP_USERS", "0")
    fake = mongomock.MongoClient()
    with patch("pymongo.MongoClient", return_value=fake):
        sys.modules.pop("app", None)
        module = importlib.import_module("app")
    module.app.config.update(TESTING=True, SECRET_KEY="po-test-secret", INVENTORY_PLATFORM_ENABLED=False)
    return module


@pytest.fixture
def client(application):
    client = application.app.test_client()
    with client.session_transaction() as session:
        session.update(username="admin", role="admin", po_csrf="test-csrf")
    return client


@pytest.fixture
def payload():
    return {
        "supplier": {"name": "Test Supplier", "details": "Address line\nSecond line", "attention": "Purchasing", "fax": "04-0000000"},
        "date": "2026-10-09", "delivery_date": "2026-10-20", "terms": "30 days", "replacement": "",
        "currency": "MYR", "discount_type": "amount", "discount_value": "0",
        "items": [{"description": "PE Container (Phrm/313/Round/UK) 1kg W/Cap & Insert", "product_id": "PE1L104-313-UK", "mssid": "313",
                   "size": "X", "material": "PE", "quantity": "240", "unit_price": "1.6000", "notes": "ID:313Z-S101/16; Bottle and Cap"}],
    }
