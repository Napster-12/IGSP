import io
import os
import re
import sys
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="igsp-test-")
os.environ["DATABASE_URL"] = f"sqlite:///{os.path.join(_tmp, 'test.db')}"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["SMTP_USER"] = ""
os.environ["SMTP_PASSWORD"] = ""
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as igsp  # noqa: E402

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PASSWORD = "Secret123!"


@pytest.fixture()
def app(monkeypatch, tmp_path):
    static_dir = tmp_path / "static"
    (static_dir / "uploads").mkdir(parents=True)
    monkeypatch.setattr(igsp, "STATIC_DIR", str(static_dir))
    monkeypatch.setattr(igsp, "UPLOAD_ROOT", str(static_dir / "uploads"))

    sent_codes = []
    monkeypatch.setattr(igsp, "send_verification_email", lambda email, code: sent_codes.append((email, code)) or True)
    igsp.app.config.update(TESTING=True)
    igsp.app.sent_codes = sent_codes
    igsp.login_limiter.hits.clear()
    igsp.reset_limiter.hits.clear()

    with igsp.app.app_context():
        igsp.DB.drop_all()
        igsp.DB.create_all()
        admin = igsp.Admin(email="admin@icebolethu.co.za", name="Admin")
        admin.set_password(PASSWORD)
        igsp.DB.session.add(admin)
        igsp.DB.session.commit()
    yield igsp.app


class Client:
    """Test client wrapper that carries the CSRF token like a browser form would."""

    def __init__(self, flask_app):
        self.app = flask_app
        self.c = flask_app.test_client()

    def token(self):
        self.c.get("/login")  # any page that renders a form seeds the token
        with self.c.session_transaction() as sess:
            return sess["_csrf_token"]

    def get(self, url, **kw):
        return self.c.get(url, **kw)

    def post(self, url, data=None, csrf=True, **kw):
        data = dict(data or {})
        if csrf:
            data["csrf_token"] = self.token()
        return self.c.post(url, data=data, **kw)

    def signup_and_login(self, email="supplier@example.com"):
        self.post("/signup", {
            "email": email, "password": PASSWORD, "confirm_password": PASSWORD,
            "company_name": "Acme Caskets", "contact_name": "Jane Dube", "phone": "0820000000",
        })
        code = self.app.sent_codes[-1][1]
        resp = self.post("/verify-email", {"code": code})
        assert resp.status_code == 302 and "/dashboard" in resp.headers["Location"]

    def assign(self, application_id, reason=None):
        data = {"reason": reason} if reason else {}
        return self.post(f"/admin/applications/{application_id}/assign", data)

    def admin_login(self):
        resp = self.post("/admin/login", {"email": "admin@icebolethu.co.za", "password": PASSWORD})
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin")


@pytest.fixture()
def client(app):
    return Client(app)


def pdf(name="doc.pdf", content=PDF):
    return (io.BytesIO(content), name)


PROFILE = {
    "registered_vendor_name": "Acme Caskets (Pty) Ltd",
    "business_registration_number": "2020/123456/07",
    "tax_number": "9000000000",
    "physical_address": "1 Main Rd",
    "city": "Durban",
    "province": "KwaZulu-Natal",
    "postal_code": "4001",
    "website": "",
    "primary_contact_person": "Jane Dube",
    "contact_person_role": "Director",
    "contact_number": "0820000000",
    "email_address": "jane@acme.co.za",
    "director_initials_0": "J. Dube",
    "director_id_0": "8001015009087",
    "director_role_0": "Director",
    "director_nationality_0": "South Africa",
}


def submit_application(client):
    resp = client.post("/onboarding/profile", {**PROFILE, "document_file_company_profile": pdf("profile.pdf")},
                       content_type="multipart/form-data")
    assert resp.headers["Location"].endswith("/onboarding/category"), resp.headers["Location"]
    resp = client.post("/onboarding/category", {"categories": "Caskets"})
    assert resp.headers["Location"].endswith("/onboarding/documents")
    files = {f"document_file_{i}": pdf(f"doc{i}.pdf") for i in range(1, 8)}
    resp = client.post("/onboarding/documents", files, content_type="multipart/form-data")
    assert resp.headers["Location"].endswith("/onboarding/review")
    resp = client.post("/onboarding/review", {"confirm": "1"})
    assert resp.headers["Location"].endswith("/dashboard")
    with client.app.app_context():
        return igsp.SupplierApplication.query.one().id
