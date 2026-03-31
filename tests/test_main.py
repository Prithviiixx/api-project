"""
tests/test_main.py
==================
Unit tests for the Smart Billing & Invoice Management API.

APIs tested:
  - API #1 (invoice CRUD):   POST, GET, DELETE /api/invoices/
  - API #4 (tax/analytics):  POST /api/tax-calculator/
                             POST /api/invoice-summary/
                             GET  /api/analytics/

API #2 (friend's PDF service) is NOT tested here because it makes
an external HTTP call. API #3 (Frankfurter) is a public API
consumed directly by the frontend JS.
"""

import pytest
from fastapi.testclient import TestClient
import main


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def clear_db():
    """Wipe the in-memory store before and after every test."""
    main.invoices_db.clear()
    yield
    main.invoices_db.clear()


@pytest.fixture()
def client():
    return TestClient(main.app)


@pytest.fixture()
def sample_invoice_payload():
    return {
        "customer_name": "Alice Smith",
        "customer_email": "alice@example.com",
        "items": [
            {"description": "Laptop", "quantity": 1, "unit_price": 1000.0},
            {"description": "Mouse",  "quantity": 2, "unit_price": 25.0},
        ],
        "currency": "USD",
        "notes": "Net 30",
    }


# ── API #1: Create Invoice ─────────────────────────────────────────────────────

class TestCreateInvoice:
    def test_creates_successfully(self, client, sample_invoice_payload):
        r = client.post("/api/invoices/", json=sample_invoice_payload)
        assert r.status_code == 200
        data = r.json()
        assert "invoice" in data
        assert data["invoice"]["customer_name"] == "Alice Smith"
        assert data["invoice"]["invoice_id"]    # non-empty
        assert data["invoice"]["status"] in ("pending", "processed")

    def test_invoice_id_is_8_chars(self, client, sample_invoice_payload):
        r = client.post("/api/invoices/", json=sample_invoice_payload)
        assert len(r.json()["invoice"]["invoice_id"]) == 8

    def test_missing_customer_name_returns_422(self, client):
        r = client.post("/api/invoices/", json={
            "customer_email": "x@x.com",
            "items": [{"description": "X", "quantity": 1, "unit_price": 1.0}],
        })
        assert r.status_code == 422

    def test_empty_items_returns_422(self, client):
        r = client.post("/api/invoices/", json={
            "customer_name": "Bob", "customer_email": "b@b.com", "items": []
        })
        assert r.status_code == 422

    def test_zero_quantity_returns_422(self, client):
        r = client.post("/api/invoices/", json={
            "customer_name": "Bob", "customer_email": "b@b.com",
            "items": [{"description": "X", "quantity": 0, "unit_price": 10.0}],
        })
        assert r.status_code == 422


# ── API #1: List Invoices ──────────────────────────────────────────────────────

class TestListInvoices:
    def test_empty_list(self, client):
        r = client.get("/api/invoices/")
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 0
        assert data["invoices"] == []

    def test_returns_created_invoices(self, client, sample_invoice_payload):
        client.post("/api/invoices/", json=sample_invoice_payload)
        client.post("/api/invoices/", json=sample_invoice_payload)
        r = client.get("/api/invoices/")
        assert r.json()["total"] == 2


# ── API #1: Get Invoice ────────────────────────────────────────────────────────

class TestGetInvoice:
    def test_get_existing(self, client, sample_invoice_payload):
        inv_id = client.post("/api/invoices/", json=sample_invoice_payload).json()["invoice"]["invoice_id"]
        r = client.get(f"/api/invoices/{inv_id}")
        assert r.status_code == 200
        assert r.json()["invoice_id"] == inv_id

    def test_get_case_insensitive(self, client, sample_invoice_payload):
        inv_id = client.post("/api/invoices/", json=sample_invoice_payload).json()["invoice"]["invoice_id"]
        r = client.get(f"/api/invoices/{inv_id.lower()}")
        assert r.status_code == 200

    def test_get_nonexistent_returns_404(self, client):
        r = client.get("/api/invoices/XXXXXXXX")
        assert r.status_code == 404


# ── API #1: Delete Invoice ─────────────────────────────────────────────────────

class TestDeleteInvoice:
    def test_delete_existing(self, client, sample_invoice_payload):
        inv_id = client.post("/api/invoices/", json=sample_invoice_payload).json()["invoice"]["invoice_id"]
        r = client.delete(f"/api/invoices/{inv_id}")
        assert r.status_code == 200
        assert client.get(f"/api/invoices/{inv_id}").status_code == 404

    def test_delete_nonexistent_returns_404(self, client):
        r = client.delete("/api/invoices/XXXXXXXX")
        assert r.status_code == 404


# ── API #4: Tax Calculator ─────────────────────────────────────────────────────

class TestTaxCalculator:
    def test_basic_calculation(self, client):
        r = client.post("/api/tax-calculator/", json={
            "items": [{"description": "Laptop", "quantity": 1, "unit_price": 1000.0}],
            "tax_rate": 23.0,
        })
        assert r.status_code == 200
        d = r.json()
        assert d["subtotal"]   == 1000.0
        assert d["tax_amount"] == 230.0
        assert d["total"]      == 1230.0

    def test_multiple_items(self, client):
        r = client.post("/api/tax-calculator/", json={
            "items": [
                {"description": "A", "quantity": 2, "unit_price": 50.0},
                {"description": "B", "quantity": 1, "unit_price": 100.0},
            ],
            "tax_rate": 10.0,
        })
        d = r.json()
        assert d["subtotal"]   == 200.0
        assert d["tax_amount"] == 20.0
        assert d["total"]      == 220.0

    def test_zero_tax_rate(self, client):
        r = client.post("/api/tax-calculator/", json={
            "items": [{"description": "X", "quantity": 1, "unit_price": 500.0}],
            "tax_rate": 0.0,
        })
        assert r.json()["total"] == 500.0

    def test_invalid_tax_rate_over_100(self, client):
        r = client.post("/api/tax-calculator/", json={
            "items": [{"description": "X", "quantity": 1, "unit_price": 100.0}],
            "tax_rate": 150.0,
        })
        assert r.status_code == 422


# ── API #4: Invoice Summary ────────────────────────────────────────────────────

class TestInvoiceSummary:
    def _create(self, client):
        return client.post("/api/invoices/", json={
            "customer_name": "Bob Jones",
            "customer_email": "bob@example.com",
            "items": [{"description": "Widget", "quantity": 5, "unit_price": 20.0}],
        }).json()["invoice"]["invoice_id"]

    def test_no_discount(self, client):
        inv_id = self._create(client)
        r = client.post("/api/invoice-summary/", json={"invoice_id": inv_id, "discount_percentage": 0})
        d = r.json()
        assert d["subtotal"]        == 100.0
        assert d["discount_amount"] == 0.0
        assert d["tax_23pct"]       == 23.0
        assert d["total_due"]       == 123.0

    def test_with_discount(self, client):
        inv_id = self._create(client)
        r = client.post("/api/invoice-summary/", json={"invoice_id": inv_id, "discount_percentage": 10.0})
        d = r.json()
        assert d["discount_amount"] == 10.0
        assert d["taxable_amount"]  == 90.0
        assert round(d["tax_23pct"], 2) == 20.7

    def test_nonexistent_invoice_returns_404(self, client):
        r = client.post("/api/invoice-summary/", json={"invoice_id": "XXXXXXXX"})
        assert r.status_code == 404


# ── API #4: Analytics ─────────────────────────────────────────────────────────

class TestAnalytics:
    def test_empty_analytics(self, client):
        r = client.get("/api/analytics/")
        assert r.status_code == 200
        d = r.json()
        assert d["total_invoices"] == 0
        assert d["total_revenue"]  == 0

    def test_populated_analytics(self, client, sample_invoice_payload):
        client.post("/api/invoices/", json=sample_invoice_payload)
        r = client.get("/api/analytics/")
        d = r.json()
        assert d["total_invoices"] == 1
        # subtotal = 1000 + 50 = 1050; tax 23% = 241.5; total = 1291.5
        assert d["total_revenue"]  == pytest.approx(1291.5, abs=0.01)
