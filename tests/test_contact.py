"""Backend tests for TrustBridge Counsel iteration 3:
- POST /api/contact (consultation request) happy + validation paths
- GET /api/contact persistence + sort + no _id leak
- /api/status regression (POST + GET)
"""
import os
import uuid
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://executive-counsel-3.preview.emergentagent.com").rstrip("/")
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ----- Contact (POST) -----
class TestContactPost:
    def test_valid_payload_returns_200_and_persists(self, session):
        unique = f"TEST_{uuid.uuid4().hex[:8]}"
        payload = {
            "name": f"TEST User {unique}",
            "email": f"test_{unique}@example.com",
            "phone": "+1 555 123 4567",
            "subject": "Couples / Relationship",
            "message": f"This is a test consultation message {unique}.",
        }
        r = session.post(f"{API}/contact", json=payload, timeout=20)
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        body = r.json()
        # Stored fields match
        assert body["name"] == payload["name"]
        assert body["email"] == payload["email"].lower()
        assert body["phone"] == payload["phone"]
        assert body["subject"] == payload["subject"]
        assert body["message"] == payload["message"]
        # Server generated
        assert "id" in body and isinstance(body["id"], str) and len(body["id"]) > 0
        assert "timestamp" in body and body["timestamp"]
        # Webhook env var not set -> forwarded_to_sheet must be False
        assert body["forwarded_to_sheet"] is False
        # No raw mongo _id leaked
        assert "_id" not in body

        # Verify persistence via GET
        g = session.get(f"{API}/contact", timeout=20)
        assert g.status_code == 200
        rows = g.json()
        assert any(row["id"] == body["id"] for row in rows), "POSTed record not found in GET response"

    def test_missing_phone_returns_422(self, session):
        payload = {
            "name": "TEST NoPhone",
            "email": "TEST_nophone@example.com",
            "subject": "Other",
            "message": "Phone missing should fail validation.",
        }
        r = session.post(f"{API}/contact", json=payload, timeout=15)
        assert r.status_code == 422, f"Expected 422 for missing phone, got {r.status_code}: {r.text}"

    def test_invalid_email_returns_422(self, session):
        payload = {
            "name": "TEST BadEmail",
            "email": "not-an-email",
            "phone": "+1 555 000 0000",
            "subject": "Other",
            "message": "Invalid email should fail.",
        }
        r = session.post(f"{API}/contact", json=payload, timeout=15)
        assert r.status_code == 422, f"Expected 422 for invalid email, got {r.status_code}: {r.text}"

    def test_empty_message_returns_422(self, session):
        payload = {
            "name": "TEST EmptyMsg",
            "email": "TEST_emptymsg@example.com",
            "phone": "+1 555 000 0000",
            "subject": "Other",
            "message": "",
        }
        r = session.post(f"{API}/contact", json=payload, timeout=15)
        assert r.status_code == 422, f"Expected 422 for empty message, got {r.status_code}: {r.text}"


# ----- Contact (GET) -----
class TestContactGet:
    def test_get_returns_list_sorted_desc_no_objectid(self, session):
        # Insert two records to validate sort
        ids = []
        for i in range(2):
            unique = f"TEST_sort_{uuid.uuid4().hex[:8]}_{i}"
            payload = {
                "name": f"TEST Sort {unique}",
                "email": f"test_sort_{unique}@example.com",
                "phone": "+1 555 222 0000",
                "subject": "Other",
                "message": f"sort test {i}",
            }
            r = session.post(f"{API}/contact", json=payload, timeout=15)
            assert r.status_code == 200
            ids.append(r.json()["id"])

        g = session.get(f"{API}/contact", timeout=15)
        assert g.status_code == 200
        rows = g.json()
        assert isinstance(rows, list)
        # Verify no _id leakage on any row
        for row in rows:
            assert "_id" not in row
            assert "timestamp" in row and row["timestamp"]
            assert "forwarded_to_sheet" in row
        # Sort desc by timestamp
        timestamps = [row["timestamp"] for row in rows]
        assert timestamps == sorted(timestamps, reverse=True), "Records are not sorted desc by timestamp"


# ----- Regression: /api/status -----
class TestStatusRegression:
    def test_status_post_and_get(self, session):
        client_name = f"TEST_status_{uuid.uuid4().hex[:8]}"
        r = session.post(f"{API}/status", json={"client_name": client_name}, timeout=15)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["client_name"] == client_name
        assert "id" in body and "timestamp" in body
        assert "_id" not in body

        g = session.get(f"{API}/status", timeout=15)
        assert g.status_code == 200
        rows = g.json()
        assert any(row["client_name"] == client_name for row in rows)
        for row in rows:
            assert "_id" not in row

    def test_root_endpoint(self, session):
        r = session.get(f"{API}/", timeout=15)
        assert r.status_code == 200
        assert r.json() == {"message": "Hello World"}
