import json
import os
import re
from datetime import date, timedelta

import app as igsp
from conftest import DOC_DATES, PASSWORD, PROFILE, Client, pdf, submit_application


def test_public_pages_render(client):
    for url in ["/", "/login", "/signup", "/terms", "/forgot-password", "/admin/login", "/health"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/no-such-page").status_code == 404


def test_signup_requires_email_verification(client):
    client.post("/signup", {
        "email": "New@Example.com", "password": PASSWORD, "confirm_password": PASSWORD,
        "company_name": "Co", "contact_name": "Me", "phone": "1", "accept_terms": "1",
    })
    # Not logged in until the code is entered.
    assert client.get("/dashboard").status_code == 302
    email, code = client.app.sent_codes[-1]
    assert email == "new@example.com"
    assert client.post("/verify-email", {"code": "000000" if code != "000000" else "111111"}).status_code == 302
    assert client.get("/dashboard").status_code == 302
    client.post("/verify-email", {"code": code})
    assert client.get("/dashboard").status_code == 200


def test_signup_requires_terms_acceptance(client):
    resp = client.post("/signup", {
        "email": "new@example.com", "password": PASSWORD, "confirm_password": PASSWORD,
        "company_name": "Co", "contact_name": "Me", "phone": "1",
    }, follow_redirects=True)
    assert b"Please accept our Terms &amp; Conditions." in resp.data
    assert client.app.sent_codes == []
    with client.app.app_context():
        assert igsp.User.query.count() == 0

    client.signup_and_login()
    with client.app.app_context():
        user = igsp.find_user_by_email("supplier@example.com")
        assert user.terms_version == igsp.TERMS_VERSION and user.terms_accepted_at is not None


def test_login_requires_terms_acceptance(client):
    client.signup_and_login()
    client.get("/logout")
    with client.app.app_context():
        user = igsp.find_user_by_email("supplier@example.com")
        user.terms_version, user.terms_accepted_at = None, None
        igsp.DB.session.commit()
    sent = len(client.app.sent_codes)

    resp = client.post("/login", {"email": "supplier@example.com", "password": PASSWORD})
    assert b"Please accept our Terms &amp; Conditions." in resp.data
    assert len(client.app.sent_codes) == sent

    resp = client.post("/login", {"email": "supplier@example.com", "password": PASSWORD, "accept_terms": "1"})
    assert resp.headers["Location"].endswith("/verify-email")
    with client.app.app_context():
        user = igsp.find_user_by_email("supplier@example.com")
        assert user.terms_version == igsp.TERMS_VERSION and user.terms_accepted_at is not None


def test_signup_rejects_weak_password(client):
    resp = client.post("/signup", {
        "email": "a@b.co", "password": "short", "confirm_password": "short",
        "company_name": "Co", "contact_name": "Me", "phone": "1", "accept_terms": "1",
    }, follow_redirects=True)
    assert b"at least 8 characters" in resp.data


def test_verification_code_locks_after_too_many_attempts(client):
    client.signup_and_login()
    client.get("/logout")
    client.post("/login", {"email": "supplier@example.com", "password": PASSWORD, "accept_terms": "1"})
    for _ in range(igsp.VERIFY_CODE_MAX_ATTEMPTS):
        resp = client.post("/verify-email", {"code": "999999x"})
    assert resp.headers["Location"].endswith("/login")
    # The real code no longer works either.
    code = client.app.sent_codes[-1][1]
    assert client.post("/verify-email", {"code": code}).headers["Location"].endswith("/login")


def test_login_rate_limited(client):
    client.signup_and_login()
    client.get("/logout")
    for _ in range(10):
        client.post("/login", {"email": "supplier@example.com", "password": "wrong", "accept_terms": "1"})
    resp = client.post("/login", {"email": "supplier@example.com", "password": PASSWORD, "accept_terms": "1"})
    assert b"Too many login attempts" in resp.data


def test_csrf_required(client):
    resp = client.post("/login", {"email": "x@y.z", "password": "p", "accept_terms": "1"}, csrf=False)
    assert resp.status_code == 302
    assert client.app.sent_codes == []


def test_full_onboarding_and_admin_review(client, monkeypatch):
    emails = []
    monkeypatch.setattr(igsp, "send_notification_email", lambda to, subject, body, **kw: emails.append((subject, kw)))
    client.signup_and_login()
    app_id = submit_application(client)

    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.status == "pending_review"
        assert application.supplier_category == "Caskets and Tombstones"
        assert len(application.directors) == 1
        docs = igsp.SupplierDocument.query.filter_by(application_id=app_id).all()
        assert len(docs) == 8  # 7 required + company profile
        assert igsp.Notification.query.filter(igsp.Notification.admin_id.isnot(None)).count() == 1

    # No certificate until the application is approved.
    assert client.get("/certificate").headers["Location"].endswith("/dashboard")

    # One application per supplier.
    assert client.get("/onboarding/profile").headers["Location"].endswith("/dashboard")
    for url in ["/dashboard", "/apps", "/settings", "/notifications", f"/applications/{app_id}",
                f"/applications/{app_id}/edit"]:
        assert client.get(url).status_code == 200, url

    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)
    for url in ["/admin", "/admin/suppliers", "/admin/applications", f"/admin/applications/{app_id}",
                "/admin/notifications", "/admin/reports", "/admin/reports?date_to=2099-01-01&category=Caskets"]:
        assert admin.get(url).status_code == 200, url

    export = admin.get("/admin/reports/export")
    assert export.status_code == 200 and b"Acme Caskets" in export.data

    zipped = admin.get(f"/admin/applications/{app_id}/download-all")
    assert zipped.status_code == 200 and zipped.data[:2] == b"PK"

    resp = admin.post(f"/admin/applications/{app_id}/update",
                      {"status": "approved", "documents_status": "complete", "review_comments": "All good"})
    assert resp.status_code == 302
    with client.app.app_context():
        user = igsp.find_user_by_email("supplier@example.com")
        assert user.active is True
        titles = {n.title for n in igsp.Notification.query.filter_by(user_id=user.id)}
        # Approval sends one welcome email (linking to the certificate) rather than separate status/activation emails.
        assert {"Welcome to Icebolethu Group", "Review Comments Added"} <= titles
        assert not {"Application Status Updated", "Account Activated"} & titles
        supplier_id, approved_at = user.supplier_id, igsp.DB.session.get(igsp.SupplierApplication, app_id).approved_at
    assert approved_at is not None

    welcome = next(kw for subject, kw in emails if subject == "Welcome to Icebolethu Group")
    assert welcome["action_url"].endswith("/certificate") and welcome["action_label"] == "View your supplier certificate"

    # Approval issues a registration certificate, listed on the supplier's Certificates tab.
    tab = client.get("/certificates").data
    assert b"Supplier registration certificate" in tab and b'href="/certificate"' in tab
    assert b'href="/certificate"' not in client.get("/dashboard").data
    for page in (client.get("/certificate"), admin.get(f"/admin/applications/{app_id}/certificate")):
        assert page.status_code == 200
        assert b"Supplier Registration" in page.data and b"Acme Caskets" in page.data
        assert b"Caskets and Tombstones" in page.data
        assert supplier_id.encode() in page.data
        assert approved_at.strftime("%d %b %Y").encode() in page.data

    # Approved applications are locked for supplier edits.
    assert client.get(f"/applications/{app_id}/edit").status_code == 302

    bad = admin.post(f"/admin/applications/{app_id}/update", {"status": "hacked", "documents_status": "complete"})
    assert bad.status_code == 302
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).status == "approved"


def test_edit_application_keeps_directors_and_resubmits(client):
    client.signup_and_login()
    app_id = submit_application(client)
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        application.status = "returned_for_update"
        igsp.DB.session.commit()

    form = {
        **PROFILE,
        "company_name": "Acme Caskets", "contact_name": "Jane Dube", "phone": "0820000000",
        "email": "attacker@example.com",  # must be ignored
        "categories": "ICT Equipment, Media and Electronic Devices", "category_detail_7": "Laptops",
        # Row 1 was removed in the browser, leaving a gap before row 2.
        "director_initials_2": "K. Nkosi", "director_id_2": "9202204720083",
        "director_role_2": "Shareholder", "director_nationality_2": "South Africa",
        "document_file_7": pdf("declaration-v2.pdf"),
        **DOC_DATES,
    }
    resp = client.post(f"/applications/{app_id}/edit", form, content_type="multipart/form-data")
    assert resp.status_code == 302 and resp.headers["Location"].endswith(f"/applications/{app_id}")

    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.email == "supplier@example.com"
        assert application.status == "pending_review"
        assert [d.initials_surname for d in application.directors] == ["J. Dube", "K. Nkosi"]
        assert application.supplier_category == "ICT Equipment, Media and Electronic Devices"
        assert application.supplier_category_detail == "Laptops"
        declaration = igsp.SupplierDocument.query.filter_by(application_id=app_id, document_type="Declaration Form").all()
        assert len(declaration) == 1 and declaration[0].original_filename == "declaration-v2.pdf"


def test_vat_and_bbbee_need_issue_and_expiry_dates(client):
    from datetime import date, timedelta
    today = date.today()
    client.signup_and_login()
    client.post("/onboarding/profile", {**PROFILE, "document_file_company_profile": pdf("profile.pdf")},
                content_type="multipart/form-data")
    client.post("/onboarding/category", {"categories": "Caskets and Tombstones"})

    page = client.get("/onboarding/documents").data.decode()
    for slot in (3, 5):  # SARS VAT and B-BBEE get date fields; other documents don't
        assert f'name="document_file_{slot}_issue_date"' in page and f'name="document_file_{slot}_expiry_date"' in page
    assert 'name="document_file_1_issue_date"' not in page
    max_expiry = igsp.latest_expiry_date().isoformat()
    assert f'min="1990-01-01" max="{today.isoformat()}"' in page and f'max="{max_expiry}"' in page  # caps the year at 4 digits

    def upload_vat(**dates):
        return client.post("/onboarding/documents", {"document_file_3": pdf("vat.pdf"), **dates},
                           content_type="multipart/form-data", follow_redirects=True).data.decode()

    iso = lambda d: d.isoformat()  # noqa: E731
    cases = [
        ({}, "Please enter the issue date of your SARS VAT certificate."),
        ({"document_file_3_issue_date": iso(today - timedelta(days=10))}, "Please enter the expiry date"),
        ({"document_file_3_issue_date": iso(today + timedelta(days=1)), "document_file_3_expiry_date": iso(today + timedelta(days=9))},
         "can&#39;t be in the future"),
        ({"document_file_3_issue_date": iso(today - timedelta(days=10)), "document_file_3_expiry_date": iso(today - timedelta(days=20))},
         "must be after its issue date"),
        ({"document_file_3_issue_date": iso(today - timedelta(days=400)), "document_file_3_expiry_date": iso(today - timedelta(days=5))},
         "expired on"),
        ({"document_file_3_issue_date": iso(today - timedelta(days=10)), "document_file_3_expiry_date": "202612-01-01"},
         "isn&#39;t a valid date; please use a 4-digit year"),
        ({"document_file_3_issue_date": iso(today - timedelta(days=10)), "document_file_3_expiry_date": "9999-12-31"},
         "can&#39;t be more than 20 years from today"),
        ({"document_file_3_issue_date": "1066-10-14", "document_file_3_expiry_date": iso(today + timedelta(days=30))},
         "can&#39;t be before 1990"),
    ]
    for dates, message in cases:
        assert message in upload_vat(**dates), message
        with client.app.app_context():
            assert igsp.SupplierDocument.query.filter_by(document_type="SARS VAT Certificate").count() == 0  # nothing stored

    issue, expiry = today - timedelta(days=30), today + timedelta(days=20)
    upload_vat(document_file_3_issue_date=iso(issue), document_file_3_expiry_date=iso(expiry))
    with client.app.app_context():
        vat = igsp.SupplierDocument.query.filter_by(document_type="SARS VAT Certificate").one()
        assert (vat.issue_date, vat.expiry_date, vat.expiry_state) == (issue, expiry, "expiring")
    page = client.get("/onboarding/documents").data.decode()
    assert f'value="{iso(issue)}"' in page and f'value="{iso(expiry)}"' in page  # kept and shown again


def test_admin_sees_dates_and_must_give_them_when_replacing(client):
    from datetime import date, timedelta
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)
    page = admin.get(f"/admin/applications/{app_id}").data.decode()
    assert page.count('<span class="ad-doc-dates">Issued ') == 2 and "Upload replacement" in page
    with client.app.app_context():
        vat_id = igsp.SupplierDocument.query.filter_by(document_type="SARS VAT Certificate").one().id
    admin.post(f"/admin/applications/{app_id}/documents/{vat_id}/replace", {"document_file": pdf("vat2.pdf")},
               content_type="multipart/form-data")
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierDocument, vat_id).original_filename == "doc3.pdf"  # refused without dates
    new_expiry = date.today() + timedelta(days=700)
    admin.post(f"/admin/applications/{app_id}/documents/{vat_id}/replace",
               {"document_file": pdf("vat2.pdf"), "document_file_issue_date": date.today().isoformat(),
                "document_file_expiry_date": new_expiry.isoformat()}, content_type="multipart/form-data")
    with client.app.app_context():
        vat = igsp.DB.session.get(igsp.SupplierDocument, vat_id)
        assert (vat.original_filename, vat.expiry_date) == ("vat2.pdf", new_expiry)


def test_certificate_expiry_reminders_deactivation_and_renewal(client):
    from datetime import date, timedelta
    client.signup_and_login()
    app_id = submit_application(client)
    approve_supplier(client, app_id)
    with client.app.app_context():
        vat = igsp.SupplierDocument.query.filter_by(document_type="SARS VAT Certificate").one()
        expiry, vat_id = vat.expiry_date, vat.id
        supplier_id = igsp.find_user_by_email("supplier@example.com").id
        bbbee = igsp.SupplierDocument.query.filter(igsp.SupplierDocument.document_type.like("Valid B-BBEE%")).one()
        bbbee.expiry_date = expiry + timedelta(days=200)  # follow the VAT certificate on its own
        igsp.DB.session.commit()

    def check(days_from_expiry):
        with client.app.test_request_context(base_url="https://portal.example"):
            return igsp.check_certificate_expiry(today=expiry + timedelta(days=days_from_expiry))

    def titles():
        with client.app.app_context():
            return [n.title for n in igsp.Notification.query.filter_by(user_id=supplier_id).order_by(igsp.Notification.id)]

    assert check(-31) == (0, 0)  # 31 days out: nothing yet
    assert check(-30) == (1, 0) and titles().count("Certificate expiring soon") == 1
    assert check(-29) == (0, 0)  # already reminded at 30 days
    assert check(-7) == (1, 0) and titles().count("Certificate expiring soon") == 2
    with client.app.app_context():
        note = igsp.Notification.query.filter_by(user_id=supplier_id, title="Certificate expiring soon").first()
        assert "SARS VAT certificate" in note.message and "will be deactivated" in note.message

    # Expired without a renewal: the account is deactivated, supplier and admins are told, once.
    assert check(1) == (0, 1) and check(2) == (0, 0)
    with client.app.app_context():
        supplier = igsp.DB.session.get(igsp.User, supplier_id)
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert supplier.active is False and application.expiry_suspended is True
        assert igsp.Notification.query.filter(igsp.Notification.admin_id.isnot(None),
                                              igsp.Notification.title.like("Certificate expired%")).count() == 1
        actions = [e.action for e in igsp.ApplicationEvent.query.filter_by(application_id=app_id)]
        assert actions.count("certificate_reminder") == 2 and "certificate_expired" in actions
    assert "Certificate expired – account deactivated" in titles()
    page = client.get("/dashboard").data.decode()
    assert "Certificates</span>" in page and "/certificates" in page

    # Renewing on the Certificates page reactivates the account and restarts the reminders.
    page = client.get("/certificates").data.decode()
    assert "Your supplier account is deactivated" in page
    client.post("/certificates", {}, content_type="multipart/form-data")  # nothing chosen
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.User, supplier_id).active is False
    new_issue, new_expiry = date.today(), date.today() + timedelta(days=400)
    client.post("/certificates", {"document_file_3": pdf("vat-2027.pdf"), "document_file_3_issue_date": new_issue.isoformat(),
                                  "document_file_3_expiry_date": new_expiry.isoformat()}, content_type="multipart/form-data")
    with client.app.app_context():
        vat = igsp.DB.session.get(igsp.SupplierDocument, vat_id)
        assert (vat.original_filename, vat.expiry_date, vat.expiry_notice) == ("vat-2027.pdf", new_expiry, "")
        assert igsp.DB.session.get(igsp.User, supplier_id).active is True
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).expiry_suspended is False
    assert "Account reactivated" in titles()

    result = client.app.test_cli_runner().invoke(args=["check-certificates"])
    assert result.exit_code == 0 and "reminder(s) sent" in result.output


def test_edit_validation_failure_does_not_lose_data(client):
    client.signup_and_login()
    app_id = submit_application(client)
    form = {**PROFILE, "company_name": "Acme", "contact_name": "Jane", "phone": "1", "categories": "Caskets and Tombstones",
            "director_nationality_0": "", **DOC_DATES}
    client.post(f"/applications/{app_id}/edit", form)
    with client.app.app_context():
        assert len(igsp.DB.session.get(igsp.SupplierApplication, app_id).directors) == 1


def test_onboarding_rejects_non_pdf_and_keeps_draft(client):
    client.signup_and_login()
    resp = client.post("/onboarding/profile",
                       {**PROFILE, "document_file_company_profile": pdf("evil.pdf", b"<html>not a pdf")},
                       content_type="multipart/form-data", follow_redirects=True)
    assert b"does not appear to be a valid PDF" in resp.data
    assert b"2020/123456/07" in resp.data  # what the user typed is still there


def test_cannot_skip_documents_step(client):
    client.signup_and_login()
    client.post("/onboarding/profile", {**PROFILE, "website": "https://acme.co.za"})
    client.post("/onboarding/category", {"categories": "Caskets and Tombstones"})
    resp = client.post("/onboarding/review")
    assert resp.headers["Location"].endswith("/onboarding/documents")


def test_uploads_require_owner_or_admin(client):
    client.signup_and_login()
    app_id = submit_application(client)
    with client.app.app_context():
        path = igsp.SupplierDocument.query.filter_by(application_id=app_id).first().file_path

    assert client.get(f"/uploads/{path}").status_code == 200
    assert client.get(f"/static/{path}").status_code == 404
    assert client.get("/uploads/../app.py").status_code == 404

    anonymous = Client(client.app)
    assert anonymous.get(f"/uploads/{path}").status_code == 404

    other = Client(client.app)
    other.signup_and_login("other@example.com")
    assert other.get(f"/uploads/{path}").status_code == 404
    assert other.get(f"/applications/{app_id}").status_code == 404

    admin = Client(client.app)
    admin.admin_login()
    assert admin.get(f"/uploads/{path}").status_code == 200  # any admin may view documents


def test_admin_routes_require_admin(client):
    client.signup_and_login()
    for url in ["/admin", "/admin/reports", "/admin/reports/export", "/db-status"]:
        assert client.get(url).status_code == 302, url


def test_password_reset_flow(client, monkeypatch, caplog):
    client.signup_and_login()
    client.get("/logout")
    with caplog.at_level("WARNING"):
        client.post("/forgot-password", {"email": "supplier@example.com"})
    link = re.search(r"(/reset-password/\S+)", caplog.text).group(1)

    assert client.get(link).status_code == 200
    resp = client.post(link, {"password": "NewPass1!", "confirm_password": "NewPass1!"})
    assert resp.headers["Location"].endswith("/login")
    # Single use.
    assert client.get(link).headers["Location"].endswith("/forgot-password")

    resp = client.post("/login", {"email": "supplier@example.com", "password": "NewPass1!", "accept_terms": "1"})
    assert resp.headers["Location"].endswith("/verify-email")


def test_change_password_and_contact_details(client):
    client.signup_and_login()
    client.post("/settings/password", {"current_password": "nope", "new_password": "NewPass1!", "confirm_password": "NewPass1!"})
    client.post("/settings/contact", {"company_name": "Acme 2", "contact_name": "Jane", "phone": "011", "address": ""})
    with client.app.app_context():
        user = igsp.find_user_by_email("supplier@example.com")
        assert user.check_password(PASSWORD)
        assert user.company_name == "Acme 2"
    client.post("/settings/password", {"current_password": PASSWORD, "new_password": "NewPass1!", "confirm_password": "NewPass1!"})
    with client.app.app_context():
        assert igsp.find_user_by_email("supplier@example.com").check_password("NewPass1!")


def test_cli_create_admin(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin", "--email", "Boss@Icebolethu.co.za", "--name", "Boss", "--password", "Xx123456!"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert igsp.find_admin_by_email("boss@icebolethu.co.za").check_password("Xx123456!")


def test_cli_seed_demo_and_remove(app, monkeypatch, tmp_path):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["seed-demo", "--suppliers", "30", "--months", "6"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        demo = igsp.User.query.filter(igsp.User.email.like("%@demo-supplier.example")).all()
        assert len(demo) == 30
        oldest = min(u.created_at for u in demo)
        assert (igsp.utcnow() - oldest).days <= 6 * 31
        apps = igsp.SupplierApplication.query.all()
        assert len(apps) == 30 and len({a.status for a in apps}) >= 5
        approved = [a for a in apps if a.status == "approved"]
        assert approved and all(igsp.find_user_by_email(a.email).active for a in approved)
        assert all(igsp.sa_id_number_error(d.id_number) is None for a in apps for d in a.directors)
        assert igsp.Product.query.count() > 0 and igsp.ProductRequest.query.count() == 0
        assert igsp.ApplicationEvent.query.filter_by(action="assigned").count() > 0
        doc = igsp.SupplierDocument.query.first()
        assert open(igsp.absolute_upload_path(doc.file_path), "rb").read(5) == b"%PDF-"

    assert runner.invoke(args=["seed-demo"]).exit_code != 0  # refuses to duplicate

    admin = Client(app)
    admin.admin_login()
    for url in ["/admin", "/admin/applications?page=2", "/admin/reports", "/admin/reports/suppliers",
                "/admin/reports/inventory", "/admin/reports/activity", "/admin/catalogue"]:
        assert admin.get(url).status_code == 200, url

    result = runner.invoke(args=["seed-demo", "--remove"])
    assert result.exit_code == 0 and "Removed 30" in result.output
    with app.app_context():
        assert igsp.User.query.count() == 0 and igsp.SupplierApplication.query.count() == 0
        assert igsp.SupplierDocument.query.count() == 0
        assert igsp.Admin.query.count() == 1  # admins are untouched

def test_contact_details_prefilled_from_registration(client):
    client.signup_and_login()
    page = client.get("/onboarding/profile").data.decode()

    def field_value(name):
        match = re.search(r'name="%s"[^>]*value="([^"]*)"' % name, page)
        return match and match.group(1)

    assert field_value("primary_contact_person") == "Jane Dube"
    assert field_value("contact_number") == "0820000000"
    assert field_value("email_address") == "supplier@example.com"
    assert field_value("registered_vendor_name") == "Acme Caskets"


def test_director_id_and_nationality_validation(client):
    client.signup_and_login()

    def attempt(**director):
        form = {**PROFILE, "website": "https://acme.co.za"}
        form.update({f"director_{k}_0": v for k, v in director.items()})
        return client.post("/onboarding/profile", form, follow_redirects=True).data.decode()

    assert "exactly 13 digits" in attempt(id="80010150090")
    assert "checksum failed" in attempt(id="8001015009088")
    assert "valid date of birth" in attempt(id="8013015009087")
    assert "select a nationality" in attempt(nationality="Atlantis")
    assert "passport number must be" in attempt(nationality="Zimbabwe", id="AB1")

    # Valid foreign passport proceeds to the next step.
    resp = client.post("/onboarding/profile", {**PROFILE, "website": "https://acme.co.za",
                                              "director_nationality_0": "Zimbabwe", "director_id_0": "fn 123456"})
    assert resp.headers["Location"].endswith("/onboarding/category")
    with client.c.session_transaction() as sess:
        assert sess["supplier_onboarding"]["directors"][0]["id_number"] == "FN123456"


def test_nationality_dropdown_lists_south_africa_first(client):
    client.signup_and_login()
    page = client.get("/onboarding/profile").data.decode()
    row_template = page.split('id="director-row-template"')[1]
    options = re.findall(r'<option value="([^"]+)"', row_template.split('director_nationality_{INDEX}')[1])
    assert options[0] == "South Africa" and "Zimbabwe" in options and len(options) > 190


def test_legacy_categories_are_merged(client):
    client.signup_and_login()
    app_id = submit_application(client)
    vehicles = "Funeral Vehicles (Hearse / Family Car)"
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        application.supplier_category = "Hearse"
        application.application_categories.clear()
        application.application_categories.append(igsp.SupplierApplicationCategory(category="Hearse", category_detail="2 hearses"))
        application.application_categories.append(igsp.SupplierApplicationCategory(category="Family Car", category_detail="Limo"))
        igsp.DB.session.commit()

        igsp.migrate_legacy_categories()
        igsp.migrate_legacy_categories()  # idempotent

        igsp.DB.session.expire_all()
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.supplier_category == vehicles
        assert [(c.category, c.category_detail) for c in application.application_categories] == [(vehicles, "2 hearses; Limo")]

    # The edit form shows the merged option as selected.
    page = client.get(f"/applications/{app_id}/edit").data.decode()
    assert re.search(r'value="Funeral Vehicles \(Hearse / Family Car\)"[^>]*checked', page)


def test_review_page_shows_all_sections_and_requires_confirmation(client):
    client.signup_and_login()
    client.post("/onboarding/profile", {**PROFILE, "document_file_company_profile": pdf("profile.pdf")},
                content_type="multipart/form-data")
    client.post("/onboarding/category", {"categories": "Caskets and Tombstones"})

    # Before documents are uploaded: missing docs are flagged and submit is disabled.
    page = client.get("/onboarding/review").data.decode()
    assert "Required — not uploaded" in page and "disabled" in page

    client.post("/onboarding/documents", {**{f"document_file_{i}": pdf(f"doc{i}.pdf") for i in range(1, 8)}, **DOC_DATES},
                content_type="multipart/form-data")
    page = client.get("/onboarding/review").data.decode()
    for text in ["Company Details", "Company Address", "Durban", "KwaZulu-Natal", "Directors &amp; Shareholders",
                 "8001015009087", "Caskets and Tombstones", "doc7.pdf", "Required documents (7/7)"]:
        assert text in page, text
    assert "Required — not uploaded" not in page

    assert 'name="accept_terms"' in page and 'href="/application-terms"' in page and 'name="confirm"' not in page
    resp = client.post("/onboarding/review")  # terms not accepted
    assert resp.headers["Location"].endswith("/onboarding/review")
    with client.app.app_context():
        assert igsp.SupplierApplication.query.count() == 0
    resp = client.post("/onboarding/review", {"accept_terms": "1"})
    assert resp.headers["Location"].endswith("/dashboard")
    with client.app.app_context():
        application = igsp.SupplierApplication.query.one()
        assert application.terms_version == igsp.APPLICATION_TERMS_VERSION and application.terms_accepted_at

    terms = client.get("/application-terms").data.decode()
    assert "Supplier Application Terms and Conditions" in terms and "Certificates with expiry dates" in terms


def test_admin_pages_share_layout_and_decline_needs_comment(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)

    pages = {"/admin": "Dashboard", "/admin/applications": "Applications", f"/admin/applications/{app_id}": "Applications",
             "/admin/suppliers": "Suppliers", "/admin/reports": "Reports"}
    for url, active in pages.items():
        page = admin.get(url).data.decode()
        assert 'class="ad-shell"' in page, url
        assert re.search(r'is-active"\s+aria-current="page">.*?<span>' + active + '</span>', page, re.S), url
        assert 'class="ad-bell has-unread"' in page and "<span>Notifications</span>" not in page, url
    assert 'ad-bell has-unread is-active' in admin.get("/admin/notifications").data.decode()

    # First visit shows the new notification as New; afterwards it's read.
    with client.app.app_context():
        admin_id = igsp.find_admin_by_email("admin@icebolethu.co.za").id
        igsp.DB.session.add(igsp.Notification(admin_id=admin_id, title="Fresh", message="hello"))
        igsp.DB.session.commit()
    assert 'class="ad-new">New<' in admin.get("/admin/notifications").data.decode()
    assert 'class="ad-new">New<' not in admin.get("/admin/notifications").data.decode()

    resp = admin.post(f"/admin/applications/{app_id}/update",
                      {"status": "declined", "documents_status": "complete", "review_comments": ""}, follow_redirects=True)
    assert b"Please add a comment" in resp.data
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).status == "pending_review"

    admin.post(f"/admin/applications/{app_id}/update",
               {"status": "returned_for_update", "documents_status": "pending", "review_comments": "Bank letter is too old"})
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.status == "returned_for_update" and application.review_comments == "Bank letter is too old"


def test_admin_login_page_has_no_supplier_signup(client):
    page = client.get("/admin/login/options").data.decode()
    assert "Supplier Administration System" in page and "/signup" not in page


def test_report_filters_only_offer_statuses_and_categories_in_use(client):
    client.signup_and_login()
    app_id = submit_application(client)  # one Caskets application, pending review
    admin = Client(client.app)
    admin.admin_login()

    page = admin.get("/admin/reports").data.decode()
    status_select = page.split('name="status"')[1].split("</select>")[0]
    assert 'value="pending_review"' in status_select
    assert 'value="approved"' not in status_select and 'value="declined"' not in status_select
    data = json.loads(page.split('id="report-data">')[1].split("</script>")[0])
    assert [pair for pair in data["status"] if pair[1] > 0] == [["pending_review", 1]]
    assert data["months"] == [data["months"][0]]  # only the month that has applications

    # Filtering by a category keeps every category available to switch to.
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        igsp.DB.session.add(igsp.SupplierApplication(
            company_name="Other Co", contact_name="X", email="x@x.co", phone="1", company_profile="",
            supplier_category="Catering", status="approved", created_at=application.created_at))
        igsp.DB.session.commit()
    page = admin.get("/admin/reports?category=Caskets+and+Tombstones").data.decode()
    category_select = page.split('name="category"')[1].split("</select>")[0]
    assert 'value="Catering"' in category_select and 'value="Caskets and Tombstones" selected' in category_select

    # A reversed date range is accepted rather than returning nothing.
    page = admin.get("/admin/reports?date_from=2099-01-01&date_to=2000-01-01").data.decode()
    assert 'name="date_from" value="2000-01-01"' in page
    assert "Acme Caskets" in page


def test_application_audit_trail(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)

    # Admin returns it with a comment, then the supplier fixes one field and resubmits.
    admin.post(f"/admin/applications/{app_id}/update",
               {"status": "returned_for_update", "documents_status": "complete", "review_comments": "Wrong tax number"})
    form = {**PROFILE, "tax_number": "9111111111", "company_name": "Acme Caskets", "contact_name": "Jane Dube",
            "phone": "0820000000", "categories": "Caskets and Tombstones", "document_file_7": pdf("declaration-signed.pdf"), **DOC_DATES}
    client.post(f"/applications/{app_id}/edit", form, content_type="multipart/form-data")
    admin.post(f"/admin/applications/{app_id}/update",
               {"status": "approved", "documents_status": "complete", "review_comments": "Wrong tax number"})
    admin.get(f"/admin/applications/{app_id}/download-all")

    with client.app.app_context():
        events = igsp.ApplicationEvent.query.filter_by(application_id=app_id).order_by(igsp.ApplicationEvent.id).all()
        assert [e.action for e in events] == [
            "submitted", "assigned", "status_changed", "resubmitted", "status_changed", "account_activated",
            "registration_certificate_issued",
            "documents_downloaded"]
        submitted, returned, resubmitted, approved = events[0], events[2], events[3], events[4]
        assert submitted.actor_type == "supplier" and submitted.actor_email == "supplier@example.com"
        assert (returned.actor_type, returned.from_status, returned.to_status) == ("admin", "pending_review", "returned_for_update")
        assert {"field": "Review comments", "old": "", "new": "Wrong tax number"} in returned.changes
        assert (resubmitted.from_status, resubmitted.to_status) == ("returned_for_update", "pending_review")
        changed = {c["field"]: (c["old"], c["new"]) for c in resubmitted.changes}
        assert changed["Tax Number"] == ("9000000000", "9111111111")
        assert changed["Document: Declaration Form"][1] == "declaration-signed.pdf"
        assert "Review comments" not in {c["field"] for c in approved.changes}  # unchanged comment isn't logged again

    page = admin.get(f"/admin/applications/{app_id}").data.decode()
    assert f'href="/admin/applications/{app_id}/history"' in page  # History is a button on the application page
    history = admin.get(f"/admin/applications/{app_id}/history").data.decode()
    assert 'id="history"' in history and "9111111111" in history and "Application resubmitted" in history

    export = admin.get(f"/admin/applications/{app_id}/history.csv")
    assert export.status_code == 200 and b"Tax Number,9000000000,9111111111" in export.data

    # Suppliers can't see the audit trail export.
    assert client.get(f"/admin/applications/{app_id}/history.csv").status_code == 302



def test_admins_can_view_but_only_assignee_can_work(client):
    client.signup_and_login()
    app_id = submit_application(client)
    with client.app.app_context():
        other = igsp.Admin(email="thandi@icebolethu.co.za", name="Thandi Zulu")
        other.set_password(PASSWORD)
        igsp.DB.session.add(other)
        igsp.DB.session.commit()
        doc = igsp.SupplierDocument.query.filter_by(application_id=app_id).first()
        doc_path, doc_id = doc.file_path, doc.id

    first, second = Client(client.app), Client(client.app)
    first.admin_login()
    second.post("/admin/login", {"email": "thandi@icebolethu.co.za", "password": PASSWORD})

    def status():
        with client.app.app_context():
            return igsp.DB.session.get(igsp.SupplierApplication, app_id).status

    # Unassigned: everyone can view everything, nobody can work on it, and Assign to me is on the page.
    page = first.get(f"/admin/applications/{app_id}").data.decode()
    assert "8001015009087" in page and "Assign to me" in page and 'id="reviewForm"' not in page
    assert first.get(f"/uploads/{doc_path}").status_code == 200
    assert first.get(f"/admin/applications/{app_id}/history").status_code == 200
    first.post(f"/admin/applications/{app_id}/update", {"status": "approved", "documents_status": "complete"})
    assert status() == "pending_review"

    # Assigned to the first admin: they can work on it.
    first.assign(app_id)
    page = first.get(f"/admin/applications/{app_id}").data.decode()
    assert "Assigned to you" in page and 'id="reviewForm"' in page and "Replace</label>" in page

    # The second admin can still view (read-only) but cannot assign, decide, replace or release.
    page = second.get(f"/admin/applications/{app_id}").data.decode()
    assert "Assigned to Admin" in page and "8001015009087" in page
    assert 'id="reviewForm"' not in page and "Replace</label>" not in page and ">Assign to me<" not in page
    resp = second.assign(app_id)
    assert b"already assigned" in second.get(f"/admin/applications/{app_id}").data or resp.status_code == 302
    second.post(f"/admin/applications/{app_id}/update", {"status": "declined", "documents_status": "complete", "review_comments": "x"})
    second.post(f"/admin/applications/{app_id}/documents/{doc_id}/replace", {"document_file": pdf("x.pdf")}, content_type="multipart/form-data")
    second.post(f"/admin/applications/{app_id}/release")
    assert second.get(f"/uploads/{doc_path}").status_code == 200
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.assigned_admin.email == "admin@icebolethu.co.za" and application.status == "pending_review"
        assert igsp.DB.session.get(igsp.SupplierDocument, doc_id).original_filename != "x.pdf"
        views = igsp.ApplicationEvent.query.filter_by(application_id=app_id, action="document_viewed").all()
        assert {v.actor_email for v in views} == {"admin@icebolethu.co.za", "thandi@icebolethu.co.za"}
        assert igsp.ApplicationEvent.query.filter_by(application_id=app_id, action="assigned").count() == 1

    # Filters, then release frees it up for the second admin.
    assert "Acme Caskets" in first.get("/admin/applications?assigned=me").data.decode()
    assert "Acme Caskets" not in second.get("/admin/applications?assigned=me").data.decode()
    first.post(f"/admin/applications/{app_id}/release")
    assert "Acme Caskets" in second.get("/admin/applications?assigned=unassigned").data.decode()
    second.assign(app_id)
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).assigned_admin.email == "thandi@icebolethu.co.za"


def test_cli_release_application(app):
    with app.app_context():
        admin = igsp.find_admin_by_email("admin@icebolethu.co.za")
        application = igsp.SupplierApplication(company_name="Co", contact_name="X", email="x@x.co", phone="1",
                                               company_profile="", supplier_category="Caskets and Tombstones", assigned_admin_id=admin.id)
        igsp.DB.session.add(application)
        igsp.DB.session.commit()
        app_id = application.id
    result = app.test_cli_runner().invoke(args=["release-application", str(app_id), "--reason", "Admin left the company"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).assigned_admin_id is None
        event = igsp.ApplicationEvent.query.filter_by(application_id=app_id, action="unassigned").one()
        assert event.actor_type == "system" and {"field": "Reason", "old": "", "new": "Admin left the company"} in event.changes


def approve_supplier(client, app_id):
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)
    admin.post(f"/admin/applications/{app_id}/update", {"status": "approved", "documents_status": "complete"})
    return admin


PRODUCT = {"name": "Pine casket", "category": "Caskets and Tombstones", "description": "Standard size", "unit": "each",
           "price": "4 500,00", "quantity_available": "5", "lead_time_days": "2", "is_listed": "1"}


def test_notification_bell_on_every_supplier_page(client):
    client.signup_and_login()
    page = client.get("/dashboard").data.decode()
    assert 'class="ad-bell"' in page and '<span class="ad-bell-count is-zero" aria-hidden="true">0</span>' in page
    submit_application(client)
    for url in ("/dashboard", "/settings", "/apps"):
        page = client.get(url).data.decode()
        assert 'class="ad-bell has-unread"' in page and '<span class="ad-bell-count" aria-hidden="true">1</span>' in page, url
        assert "<span>Notifications</span>" not in page, url  # replaced the sidebar item
    client.get("/notifications")  # opening the list marks them read
    assert 'ad-bell-count is-zero" aria-hidden="true">0<' in client.get("/dashboard").data.decode()


def test_on_file_decision(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)
    page = admin.get(f"/admin/applications/{app_id}").data.decode()
    assert 'value="on_file"' in page and "Accept &amp; keep on file" in page

    # No comment needed; documents are marked complete; the supplier gets the on-file message.
    admin.post(f"/admin/applications/{app_id}/update", {"status": "on_file", "documents_status": "pending"})
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        supplier = igsp.find_user_by_email("supplier@example.com")
        assert (application.status, application.documents_status, supplier.active) == ("on_file", "complete", False)
        note = igsp.Notification.query.filter_by(user_id=supplier.id, title="Application Received – On File").one()
        assert "keep your details on file and contact you when we need your services" in note.message
        assert igsp.ApplicationEvent.query.filter_by(application_id=app_id, to_status="on_file").count() == 1

    page = client.get("/dashboard").data.decode()
    assert "ad-badge--on_file" in page and "We will keep your details on file" in page
    assert client.get("/inventory").headers["Location"].endswith("/dashboard")  # no account, so no inventory
    assert client.get(f"/applications/{app_id}/edit").status_code == 302  # no longer editable

    page = admin.get("/admin/applications?status=on_file").data.decode()
    assert "sl-card--on_file is-active" in page and "Acme Caskets" in page

    # Moving an approved supplier to on file deactivates the account.
    admin.post(f"/admin/applications/{app_id}/update", {"status": "approved", "documents_status": "complete"})
    admin.post(f"/admin/applications/{app_id}/update", {"status": "on_file", "documents_status": "complete"})
    with client.app.app_context():
        assert igsp.find_user_by_email("supplier@example.com").active is False


def test_inventory_only_for_active_suppliers(client):
    client.signup_and_login()
    app_id = submit_application(client)
    # Pending application: no inventory, no menu item.
    assert client.get("/inventory").headers["Location"].endswith("/dashboard")
    assert client.post("/inventory/new", PRODUCT).headers["Location"].endswith("/dashboard")
    assert ">Inventory<" not in client.get("/dashboard").data.decode()
    with client.app.app_context():
        assert igsp.Product.query.count() == 0

    approve_supplier(client, app_id)
    assert ">Inventory<" in client.get("/dashboard").data.decode()
    assert client.get("/inventory").status_code == 200
    client.post("/inventory/new", PRODUCT)
    with client.app.app_context():
        product = igsp.Product.query.one()
        assert (product.name, str(product.price), product.quantity_available) == ("Pine casket", "4500.00", 5)

    bad = client.post("/inventory/new", {**PRODUCT, "price": "lots"})
    assert bad.status_code == 400 and b"valid price" in bad.data


def test_catalogue_shows_listed_products_and_supplier_contact(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)
    client.post("/inventory/new", {**PRODUCT, "name": "Hidden casket", "is_listed": ""})
    with client.app.app_context():
        supplier_id = igsp.find_user_by_email("supplier@example.com").id

    catalogue = admin.get("/admin/catalogue").data.decode()
    assert "Pine casket" in catalogue and "Hidden casket" not in catalogue and "R 4 500.00" in catalogue
    assert "mailto:supplier@example.com" in catalogue  # admins order outside the portal
    assert "Durban, KwaZulu-Natal" in catalogue  # supplier location from the approved application

    # Location filter: only provinces that have listed products are offered.
    assert '<option value="KwaZulu-Natal"' in catalogue and '<option value="Gauteng"' not in catalogue
    assert "Pine casket" in admin.get("/admin/catalogue?province=KwaZulu-Natal").data.decode()
    assert "Pine casket" not in admin.get("/admin/catalogue?province=Gauteng").data.decode()
    assert "/request" not in catalogue and "Requests" not in admin.get("/admin").data.decode()
    assert "Requests" not in client.get("/inventory").data.decode()

    # Request pages are gone for both sides.
    for url in ("/requests", "/requests/1", "/admin/requests", "/admin/requests/1", "/admin/catalogue/1/request",
                "/admin/requests/1/quotation.pdf", "/admin/reports/requests"):
        assert (client if not url.startswith("/admin") else admin).get(url).status_code == 404, url

    # Deactivating the supplier removes their products from the catalogue and blocks their inventory.
    admin.post(f"/admin/suppliers/{supplier_id}/toggle")
    assert "Pine casket" not in admin.get("/admin/catalogue").data.decode()
    assert client.get("/inventory").headers["Location"].endswith("/dashboard")


def test_additional_reports(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)

    # The applications report shows the report buttons too.
    page = admin.get("/admin/reports").data.decode()
    assert 'class="ad-report-tabs"' in page and "/admin/reports/suppliers" in page
    assert "/admin/reports/requests" not in page

    page = admin.get("/admin/reports/suppliers?period=all").data.decode()
    assert "Acme Caskets" in page and "Registrations per month" in page
    assert 'aria-current="page">Suppliers' in page

    page = admin.get("/admin/reports/inventory").data.decode()
    assert "Pine casket" in page and "Live snapshot" in page

    page = admin.get("/admin/reports/activity?period=30d").data.decode()
    assert "Assigned to admin" in page and "Status changed" in page

    csv_data = admin.get("/admin/reports/inventory/export").data.decode("utf-8-sig")
    assert csv_data.splitlines()[0].startswith("Product,Category,Supplier") and "4500.00" in csv_data

    assert admin.get("/admin/reports/nonsense").status_code == 404
    assert admin.get("/admin/reports/suppliers?period=bogus").status_code == 200  # falls back to 12 months
    assert client.get("/admin/reports/suppliers").status_code == 302  # suppliers can't see reports


def test_admin_login_is_two_steps(client):
    landing = client.get("/admin/login").data.decode()
    assert "/admin/login/options" in landing and 'name="password"' not in landing
    options = client.get("/admin/login/options").data.decode()
    assert 'name="password"' in options and 'action="/admin/login"' in options
    assert "Continue with Microsoft (Azure AD)" in options  # shown even when SSO isn't configured


def test_admin_login_error_shows_sign_in_options(client):
    page = client.post("/admin/login", {"email": "admin@icebolethu.co.za", "password": "wrong"}).data.decode()
    assert "Invalid email or password." in page and 'name="password"' in page


def test_admin_logout_returns_to_admin_login(client):
    client.admin_login()
    resp = client.get("/admin/logout")
    assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/login")
    assert client.get("/admin").status_code == 302  # signed out


def test_caskets_and_tombstones_are_one_category(client):
    client.signup_and_login()
    client.post("/onboarding/profile", {**PROFILE, "document_file_company_profile": pdf("profile.pdf")})
    page = client.get("/onboarding/category").data.decode()
    assert 'value="Caskets and Tombstones"' in page
    assert 'value="Caskets"' not in page and 'value="Tombstones"' not in page
    # An old category name still posted by a stale form is mapped onto the merged category.
    client.post("/onboarding/category", {"categories": "Tombstones"})
    with client.c.session_transaction() as sess:
        assert sess["supplier_onboarding"]["supplier_category"] == "Caskets and Tombstones"


def test_application_slicer_cards_count_and_filter(client):
    client.admin_login()
    with client.app.app_context():
        admin = igsp.Admin.query.first()
        for name, status, mine in [("Alpha", "pending_review", True), ("Bravo", "pending_review", False),
                                   ("Charlie", "approved", False), ("Delta", "declined", True)]:
            igsp.DB.session.add(igsp.SupplierApplication(
                company_name=name, contact_name="X", email=f"{name}@x.co", phone="1", company_profile="",
                supplier_category="Catering", status=status, assigned_admin_id=admin.id if mine else None))
        igsp.DB.session.commit()

    page = client.get("/admin/applications").data.decode()
    assert '<span class="sl-value">4</span>\n                    <span class="sl-label">All applications</span>' in page
    assert '<span class="sl-value">2</span>\n                    <span class="sl-label">Pending Review</span>' in page

    # Clicking the Pending Review card lists only those applications.
    page = client.get("/admin/applications?status=pending_review").data.decode()
    assert "Alpha" in page and "Bravo" in page and "Charlie" not in page and "Delta" not in page
    assert 'sl-card--pending_review is-active' in page

    # Combining with "Assigned to me" narrows further, and the status counts follow the assignment card.
    page = client.get("/admin/applications?status=pending_review&assigned=me").data.decode()
    assert "Alpha" in page and "Bravo" not in page
    assert '<span class="sl-value">2</span>\n                    <span class="sl-label">All applications</span>' in page


def test_supplier_slicer_cards_count_and_filter(client):
    client.admin_login()
    with client.app.app_context():
        for name, active, status in [("Alpha", True, "approved"), ("Bravo", False, "pending_review"),
                                     ("Charlie", False, None), ("Delta", False, "declined")]:
            email = f"{name.lower()}@x.co"
            user = igsp.User(supplier_id=f"IGSP{name[:4].upper()}", name=name, email=email, company_name=name,
                             contact_name=name, phone="1", active=active, email_verified=True)
            user.set_password(PASSWORD)
            igsp.DB.session.add(user)
            if status:
                igsp.DB.session.add(igsp.SupplierApplication(
                    company_name=name, contact_name=name, email=email.upper(), phone="1", company_profile="",
                    supplier_category="Catering", status=status))
        igsp.DB.session.commit()

    page = client.get("/admin/suppliers").data.decode()
    assert '<span class="sl-value">4</span>\n                    <span class="sl-label">All suppliers</span>' in page
    assert '<span class="sl-value">1</span>\n                    <span class="sl-label">Not submitted</span>' in page

    # Inactive suppliers only; the application cards then count inactive suppliers only.
    page = client.get("/admin/suppliers?account=inactive").data.decode()
    assert "Bravo" in page and "Charlie" in page and "Delta" in page and "Alpha" not in page
    assert '<span class="sl-value">0</span>\n                    <span class="sl-label">Approved</span>' in page

    page = client.get("/admin/suppliers?account=inactive&application=none").data.decode()
    assert "Charlie" in page and "Bravo" not in page and "Delta" not in page


def add_admin(app, email, name, role):
    with app.app_context():
        admin = igsp.Admin(email=email, name=name, role=role)
        admin.set_password(PASSWORD)
        igsp.DB.session.add(admin)
        igsp.DB.session.commit()
        return admin.id


def login_as(app, email):
    c = Client(app)
    resp = c.post("/admin/login", {"email": email, "password": PASSWORD})
    return c, resp


def test_applications_admin_only_sees_applications_and_suppliers(client):
    client.signup_and_login()
    app_id = submit_application(client)
    with client.app.app_context():
        doc_path = igsp.SupplierDocument.query.filter_by(application_id=app_id).first().file_path
    add_admin(client.app, "apps@icebolethu.co.za", "Apps Admin", "applications")
    apps, resp = login_as(client.app, "apps@icebolethu.co.za")
    assert resp.headers["Location"].endswith("/admin/applications")

    page = apps.get("/admin/applications").data.decode()
    assert "Procurement Officer" in page and 'href="/admin/reports"' not in page and 'href="/admin"' not in page
    assert apps.get("/admin/suppliers").status_code == 200
    assert apps.get(f"/admin/applications/{app_id}").status_code == 200
    assert apps.get(f"/uploads/{doc_path}").status_code == 200
    for url in ["/admin/reports", "/admin/reports/export", "/admin/catalogue"]:
        resp = apps.get(url)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/applications"), url
    assert apps.get("/admin").headers["Location"].endswith("/admin/applications")


def test_reports_admin_only_sees_reports(client):
    client.signup_and_login()
    app_id = submit_application(client)
    with client.app.app_context():
        doc_path = igsp.SupplierDocument.query.filter_by(application_id=app_id).first().file_path
    add_admin(client.app, "reports@icebolethu.co.za", "Reports Admin", "reports")
    rep, resp = login_as(client.app, "reports@icebolethu.co.za")
    assert resp.headers["Location"].endswith("/admin/reports")

    page = rep.get("/admin/reports").data.decode()
    assert "Procurement Manager" in page and 'href="/admin/applications"' not in page and 'href="/admin/suppliers"' not in page
    assert rep.get("/admin/reports/export").status_code == 200
    for url in ["/admin/applications", f"/admin/applications/{app_id}", "/admin/suppliers", "/admin/catalogue", "/admin"]:
        resp = rep.get(url)
        assert resp.status_code == 302 and resp.headers["Location"].endswith("/admin/reports"), url
    assert rep.post(f"/admin/applications/{app_id}/assign").status_code == 302
    assert rep.get(f"/uploads/{doc_path}").status_code == 404
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).assigned_admin_id is None


def test_super_admin_assigns_applications_to_applications_admin(client):
    client.signup_and_login()
    app_id = submit_application(client)
    apps_id = add_admin(client.app, "apps@icebolethu.co.za", "Apps Admin", "applications")
    other_id = add_admin(client.app, "apps2@icebolethu.co.za", "Second Apps", "applications")
    reports_id = add_admin(client.app, "reports@icebolethu.co.za", "Reports Admin", "reports")

    boss = Client(client.app)
    boss.admin_login()
    page = boss.get(f"/admin/applications/{app_id}").data.decode()
    assert 'name="admin_id"' in page and "Apps Admin" in page and "Reports Admin" not in page

    def assignee():
        with client.app.app_context():
            return igsp.DB.session.get(igsp.SupplierApplication, app_id).assigned_admin_id

    # Only applications admins can be chosen.
    boss.post(f"/admin/applications/{app_id}/assign-to", {"admin_id": reports_id})
    assert assignee() is None

    boss.post(f"/admin/applications/{app_id}/assign-to", {"admin_id": apps_id})
    assert assignee() == apps_id
    with client.app.app_context():
        assert igsp.Notification.query.filter_by(admin_id=apps_id, related_application_id=app_id).count() >= 1

    # A super admin can reassign even when someone else holds it.
    boss.post(f"/admin/applications/{app_id}/assign-to", {"admin_id": other_id})
    assert assignee() == other_id

    # Applications admins can't hand applications to others.
    apps, _ = login_as(client.app, "apps@icebolethu.co.za")
    apps.post(f"/admin/applications/{app_id}/assign-to", {"admin_id": apps_id})
    assert assignee() == other_id
    assert 'name="admin_id"' not in apps.get(f"/admin/applications/{app_id}").data.decode()


def test_cli_admin_roles(app):
    runner = app.test_cli_runner()
    result = runner.invoke(args=["create-admin", "--email", "r@icebolethu.co.za", "--name", "R", "--role", "reports", "--password", "Xx123456!"])
    assert result.exit_code == 0, result.output
    result = runner.invoke(args=["set-admin-role", "r@icebolethu.co.za", "applications"])
    assert result.exit_code == 0, result.output
    with app.app_context():
        assert igsp.find_admin_by_email("r@icebolethu.co.za").role == "applications"
        assert igsp.find_admin_by_email("admin@icebolethu.co.za").role == "super"


def test_supplier_report_custom_date_range(client):
    client.signup_and_login()
    client.signup_and_login("old@example.com")
    with client.app.app_context():
        old = igsp.find_user_by_email("old@example.com")
        old.company_name = "Old Timers"
        old.created_at = igsp.utcnow() - timedelta(days=400)
        igsp.DB.session.commit()
    admin = Client(client.app)
    admin.admin_login()

    recent_from = (date.today() - timedelta(days=7)).isoformat()
    page = admin.get(f"/admin/reports/suppliers?date_from={recent_from}").data.decode()
    assert "Acme Caskets" in page and "Old Timers" not in page and 'name="date_from"' in page

    span_from = (date.today() - timedelta(days=410)).isoformat()
    span_to = (date.today() - timedelta(days=390)).isoformat()
    page = admin.get(f"/admin/reports/suppliers?date_from={span_from}&date_to={span_to}").data.decode()
    assert "Old Timers" in page and "Acme Caskets" not in page
    assert f"date_from={span_from}" in page  # carried into the export and report tabs

    resp = admin.get(f"/admin/reports/suppliers/export?date_from={span_from}&date_to={span_to}")
    body = resp.data.decode("utf-8-sig")
    assert "Old Timers" in body and "Acme Caskets" not in body
    assert f"{span_from}_to_{span_to}" in resp.headers["Content-Disposition"]

    # A reversed range is swapped rather than returning nothing.
    page = admin.get(f"/admin/reports/suppliers?date_from={span_to}&date_to={span_from}").data.decode()
    assert "Old Timers" in page



def test_director_on_another_supplier_is_flagged_not_blocked(client):
    # J. Dube (8001015009087) is director of the first supplier.
    client.signup_and_login("first@example.com")
    first_id = submit_application(client)
    with client.app.app_context():
        igsp.DB.session.get(igsp.SupplierApplication, first_id).company_name = "Bongi Funerals"
        igsp.DB.session.commit()

    # A different company lists the same person: the application still goes through, with no error shown.
    second = Client(client.app)
    second.signup_and_login("second@example.com")
    second_id = submit_application(second)
    assert "already linked" not in second.get("/dashboard").data.decode()

    # Admins are told once, naming both suppliers.
    with client.app.app_context():
        admin_id = igsp.find_admin_by_email("admin@icebolethu.co.za").id
        notes = igsp.Notification.query.filter(igsp.Notification.admin_id == admin_id,
                                               igsp.Notification.title.like("Director match%")).all()
        assert len(notes) == 1 and notes[0].related_application_id == second_id
        assert "8001015009087" in notes[0].message and "Bongi Funerals" in notes[0].message

    # Saving the application again doesn't repeat the notification.
    with client.app.app_context():
        igsp.DB.session.get(igsp.SupplierApplication, second_id).status = "returned_for_update"
        igsp.DB.session.commit()
    second.post(f"/applications/{second_id}/edit", {**PROFILE, "company_name": "Acme Caskets", "contact_name": "Jane Dube",
                                                    "phone": "0820000000", "categories": "Caskets and Tombstones",
                                                    **DOC_DATES}, content_type="multipart/form-data")
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, second_id).status == "pending_review"
        assert igsp.Notification.query.filter(igsp.Notification.admin_id == admin_id,
                                              igsp.Notification.title.like("Director match%")).count() == 1

    # The first supplier is not flagged by the later duplicate.
    with client.app.app_context():
        assert igsp.director_matches([igsp.DB.session.get(igsp.SupplierApplication, first_id)]) == {}


def test_admin_sees_duplicates_that_predate_the_rule(client):
    client.signup_and_login("first@example.com")
    first_id = submit_application(client)
    second = Client(client.app)
    second.signup_and_login("second@example.com")
    with client.app.app_context():  # simulate one that got in before the check existed
        user = igsp.find_user_by_email("second@example.com")
        app_ = igsp.SupplierApplication(company_name="Second Co", contact_name="X", email=user.email, phone="1", supplier_category="Caskets and Tombstones", company_profile="",
                                        status="pending_review")
        app_.directors.append(igsp.SupplierDirector(initials_surname="J. Dube", id_number="8001015009087",
                                                    role="Shareholder", nationality="South Africa"))
        igsp.DB.session.add(app_)
        igsp.DB.session.commit()
        second_id = app_.id
    admin = Client(client.app)
    admin.admin_login()
    page = admin.get(f"/admin/applications/{second_id}").data.decode()
    assert "Already linked to another supplier" in page and f"/admin/applications/{first_id}" in page
    assert "Director match" in admin.get("/admin/applications").data.decode()


def test_flagged_application_can_only_be_declined(client):
    client.signup_and_login("first@example.com")
    first_id = submit_application(client)
    with client.app.app_context():
        igsp.DB.session.get(igsp.SupplierApplication, first_id).company_name = "Bongi Funerals"
        user = igsp.User(email="second@example.com", company_name="Second Co", contact_name="X", phone="1")
        user.set_password(PASSWORD)
        app_ = igsp.SupplierApplication(company_name="Second Co", contact_name="X", email=user.email, phone="1",
                                        supplier_category="Caskets and Tombstones", company_profile="", status="pending_review")
        app_.directors.append(igsp.SupplierDirector(initials_surname="J. Dube", id_number="8001015009087",
                                                    role="Shareholder", nationality="South Africa"))
        igsp.DB.session.add_all([user, app_])
        igsp.DB.session.commit()
        second_id = app_.id

    admin = Client(client.app)
    admin.admin_login()
    admin.assign(second_id)
    page = admin.get(f"/admin/applications/{second_id}").data.decode()
    assert "This application can only be declined" in page and "J. Dube" in page and "Bongi Funerals" in page
    for value in ["approved", "on_file", "under_review", "returned_for_update"]:
        button = re.search(rf'<button[^>]*value="{value}"[^>]*>', page).group(0)
        assert "disabled" in button, value
    assert "disabled" not in re.search(r'<button[^>]*value="declined"[^>]*>', page).group(0)
    # Decline comment is pre-filled for the supplier, naming the person but not the other company.
    comment = re.search(r'<textarea name="review_comments"[^>]*>(.*?)</textarea>', page, re.S).group(1)
    assert "J. Dube" in comment and "Bongi" not in comment

    def status():
        with client.app.app_context():
            return igsp.DB.session.get(igsp.SupplierApplication, second_id).status

    admin.post(f"/admin/applications/{second_id}/update", {"status": "approved", "documents_status": "complete"})
    assert status() == "pending_review"
    admin.post(f"/admin/applications/{second_id}/update", {"status": "declined", "documents_status": "complete",
                                                          "review_comments": "Director already linked."})
    assert status() == "declined"


def test_supplier_account_card_only_after_onboarding(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)
    assert "Supplier account" not in admin.get(f"/admin/applications/{app_id}").data.decode()
    admin.post(f"/admin/applications/{app_id}/update", {"status": "approved", "documents_status": "complete"})
    assert "Supplier account" in admin.get(f"/admin/applications/{app_id}").data.decode()


def test_ict_admin_sees_everything_but_the_dashboard(client):
    client.signup_and_login()
    app_id = submit_application(client)
    add_admin(client.app, "ict@icebolethu.co.za", "ICT Admin", "ict")
    ict, resp = login_as(client.app, "ict@icebolethu.co.za")
    assert resp.headers["Location"].endswith("/admin/applications")

    page = ict.get("/admin/applications").data.decode()
    assert "ICT Admin" in page and 'href="/admin/ict/audit"' in page and 'href="/admin/ict/emails"' in page
    assert 'href="/admin"' not in page
    assert ict.get("/admin").headers["Location"].endswith("/admin/applications")
    for url in ["/admin/suppliers", "/admin/catalogue", "/admin/reports", f"/admin/applications/{app_id}",
                "/admin/ict/audit", "/admin/ict/audit?view=accounts", "/admin/ict/emails", "/admin/ict/emails/application_received"]:
        assert ict.get(url).status_code == 200, url
    # Like a Team Lead, ICT can hand applications to officers.
    officer_id = add_admin(client.app, "officer@icebolethu.co.za", "Officer", "applications")
    ict.post(f"/admin/applications/{app_id}/assign-to", {"admin_id": officer_id})
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id).assigned_admin_id == officer_id
        # ICT isn't part of the review workflow, so it gets no application notifications.
        ict_id = igsp.find_admin_by_email("ict@icebolethu.co.za").id
        assert igsp.Notification.query.filter_by(admin_id=ict_id).count() == 0

    # Nobody else can open the ICT pages or use the ICT actions.
    team_lead = Client(client.app)
    team_lead.admin_login()
    for url in ["/admin/ict/audit", "/admin/ict/emails", "/admin/ict/emails/application_received"]:
        assert team_lead.get(url).status_code == 302, url
    assert 'href="/admin/ict/audit"' not in team_lead.get("/admin").data.decode()
    assert "Delete application" not in team_lead.get(f"/admin/applications/{app_id}").data.decode()
    team_lead.post(f"/admin/ict/applications/{app_id}/delete", {"confirm_name": "Acme Caskets"})
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id) is not None


def test_user_audit_shows_everything_an_admin_did_and_no_suppliers(client):
    client.signup_and_login()
    client.get("/logout")
    client.post("/login", {"email": "supplier@example.com", "password": "wrong", "accept_terms": "1"})
    client.signup_and_login("second@example.com")
    app_id = submit_application(client)
    Client(client.app).post("/admin/login", {"email": "admin@icebolethu.co.za", "password": "wrong"})

    # The Team Lead signs in, works on the application and exports a report.
    lead = Client(client.app)
    lead.admin_login()
    lead.assign(app_id)
    with client.app.app_context():
        doc_path = igsp.SupplierDocument.query.filter_by(application_id=app_id).first().file_path
        lead_id = igsp.find_admin_by_email("admin@icebolethu.co.za").id
    lead.get(f"/uploads/{doc_path}")
    lead.post(f"/admin/applications/{app_id}/update", {"status": "under_review", "documents_status": "complete"})
    lead.get("/admin/reports/export")
    add_admin(client.app, "ict@icebolethu.co.za", "ICT Admin", "ict")
    ict, _ = login_as(client.app, "ict@icebolethu.co.za")

    # Supplier activity isn't recorded in the user audit at all.
    with client.app.app_context():
        assert igsp.AuditLog.query.filter(igsp.AuditLog.actor_type != "admin").count() == 0

    def rows(page):
        return re.findall(r'<td class="ad-nowrap">([A-Za-z][^<]+)</td>', page)

    lead_page = ict.get(f"/admin/ict/audit?admin={lead_id}").data.decode()
    assert rows(lead_page) == ["Report exported", "Status changed", "Document viewed", "Assigned to admin",
                               "Admin signed in", "Admin sign-in failed"]
    assert "Acme Caskets" in lead_page and f"#{app_id}" in lead_page and "supplier@example.com" not in lead_page
    everyone = rows(ict.get("/admin/ict/audit").data.decode())
    assert everyone[0] == "Admin signed in" and "Report exported" in everyone and not any("Supplier" in r for r in everyone)
    assert rows(ict.get("/admin/ict/audit?action=document_viewed").data.decode()) == ["Document viewed"]

    csv_body = ict.get(f"/admin/ict/audit/export?admin={lead_id}").data.decode("utf-8-sig")
    assert "Status changed" in csv_body and f"Acme Caskets (#{app_id})" in csv_body
    accounts = ict.get("/admin/ict/audit?view=accounts").data.decode()
    assert f"/admin/ict/audit?admin={lead_id}" in accounts and "All activity" in accounts


def test_ict_edits_email_templates(client):
    add_admin(client.app, "ict@icebolethu.co.za", "ICT Admin", "ict")
    ict, _ = login_as(client.app, "ict@icebolethu.co.za")
    url = "/admin/ict/emails/application_received"

    bad = ict.post(url, {"subject": "Hi", "body": "Thanks {contact_person}"})
    assert bad.status_code == 200 and "{contact_person}" in bad.data.decode()
    ict.post(url, {"subject": "We got it, {company_name}", "body": "Thanks {company_name}, we will be in touch."})
    assert "Edited" in ict.get("/admin/ict/emails").data.decode()

    client.signup_and_login()
    submit_application(client)
    with client.app.app_context():
        note = igsp.Notification.query.filter(igsp.Notification.user_id.isnot(None)).one()
        assert (note.title, note.message) == ("We got it, Acme Caskets", "Thanks Acme Caskets, we will be in touch.")
        assert igsp.AuditLog.query.filter_by(action="email_template_updated").count() == 1

    ict.post(f"{url}/reset")
    with client.app.app_context():
        assert igsp.email_text("application_received", company_name="X")[0] == "Application Received"


def test_ict_deletes_directors_and_applications(client):
    client.signup_and_login()
    app_id = submit_application(client)
    add_admin(client.app, "ict@icebolethu.co.za", "ICT Admin", "ict")
    ict, _ = login_as(client.app, "ict@icebolethu.co.za")
    with client.app.app_context():
        director_id = igsp.DB.session.get(igsp.SupplierApplication, app_id).directors[0].id
        file_paths = [d.file_path for d in igsp.SupplierDocument.query.filter_by(application_id=app_id)]

    ict.post(f"/admin/ict/applications/{app_id}/directors/{director_id}/delete")
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.directors == []
        assert igsp.ApplicationEvent.query.filter_by(application_id=app_id, action="director_removed").count() == 1
        assert "8001015009087" in igsp.AuditLog.query.filter_by(action="director_deleted").one().detail

    # The company name must be typed to confirm.
    ict.post(f"/admin/ict/applications/{app_id}/delete", {"confirm_name": "Wrong name"})
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id) is not None

    resp = ict.post(f"/admin/ict/applications/{app_id}/delete", {"confirm_name": "acme caskets"})
    assert resp.headers["Location"].endswith("/admin/applications")
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.SupplierApplication, app_id) is None
        assert igsp.SupplierDocument.query.filter_by(application_id=app_id).count() == 0
        assert igsp.ApplicationEvent.query.filter_by(application_id=app_id).count() == 0
        assert "Acme Caskets" in igsp.AuditLog.query.filter_by(action="application_deleted").one().detail
        for path in file_paths:
            assert not igsp.absolute_upload_path(path) or not os.path.isfile(igsp.absolute_upload_path(path))
    # The supplier can still sign in and apply again.
    assert client.get("/onboarding/profile").status_code == 200



def ask(client, url, message):
    resp = client.c.post(url, json={"message": message}, headers={"X-CSRFToken": client.token()})
    return resp.status_code, (resp.get_json() or {}).get("reply", "")


def test_cebelihle_answers_suppliers_from_their_own_records(client):
    other = Client(client.app)
    other.signup_and_login("other@example.com")
    submit_application(other)
    with client.app.app_context():
        igsp.SupplierApplication.query.one().company_name = "Bongi Funerals"
        igsp.DB.session.commit()

    client.signup_and_login()
    assert "Ask Cebelihle" in client.get("/dashboard").data.decode()
    status, reply = ask(client, "/chat", "What's my application status?")
    assert status == 200 and "haven't submitted" in reply and "/onboarding/profile" in reply
    assert "7 required document" in ask(client, "/chat", "which documents are missing?")[1]

    app_id = submit_application(client)
    reply = ask(client, "/chat", "what is the status of my application")[1]
    assert "Acme Caskets" in reply and "Pending Review" in reply and "Bongi" not in reply
    assert "All 7 required documents are uploaded" in ask(client, "/chat", "any documents missing?")[1]
    assert "SARS VAT certificate" in ask(client, "/chat", "when does my VAT certificate expire")[1]
    assert "031 208 9102" in ask(client, "/chat", "I want to change my bank details")[1]
    assert "031 208 9102" in ask(client, "/chat", "Why was I declined?")[1]
    assert "can't share information about other suppliers" in ask(client, "/chat", "how many other suppliers are there")[1]
    assert "PDF" in ask(client, "/chat", "Can I upload a jpg photo?")[1]
    assert "didn't quite get that" in ask(client, "/chat", "what's the weather in Durban")[1]

    # Returned with comments: the supplier sees the comments and where to fix them.
    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        application.status, application.review_comments = "returned_for_update", "Upload a newer bank letter."
        igsp.DB.session.commit()
    reply = ask(client, "/chat", "any update on my application?")[1]
    assert "Returned For Update" in reply and "newer bank letter" in reply and f"/applications/{app_id}/edit" in reply

    assert ask(client, "/chat", "x" * 501)[0] == 400
    assert ask(client, "/admin/chat", "application 1")[0] == 302  # suppliers can't use the admin chat


def test_cebelihle_admin_answers_follow_the_role(client):
    client.signup_and_login()
    app_id = submit_application(client)
    add_admin(client.app, "officer@icebolethu.co.za", "Officer", "applications")
    add_admin(client.app, "manager@icebolethu.co.za", "Manager", "reports")
    officer, _ = login_as(client.app, "officer@icebolethu.co.za")
    manager, _ = login_as(client.app, "manager@icebolethu.co.za")
    assert "Ask Cebelihle" in officer.get("/admin/applications").data.decode()

    reply = ask(officer, "/admin/chat", f"tell me about application {app_id}")[1]
    assert "Acme Caskets" in reply and "Pending Review" in reply and f"/admin/applications/{app_id}" in reply
    assert "awaiting review" in ask(officer, "/admin/chat", "How many applications are waiting?")[1]
    assert "Assign to me" in ask(officer, "/admin/chat", "how do I assign an application")[1]
    assert "can only be declined" in ask(officer, "/admin/chat", "what is a director match")[1]

    # A Procurement Manager (reports only) gets no application data.
    assert "doesn't have access" in ask(manager, "/admin/chat", "How many applications are waiting?")[1]
    assert "Acme" not in ask(manager, "/admin/chat", f"application {app_id}")[1]
    assert "/admin/reports" in ask(manager, "/admin/chat", "how do I export a report")[1]


def test_cebelihle_answers_frequently_asked_questions(client):
    client.signup_and_login()
    faqs = {
        "Is it free to register?": "no fee",
        "I don't have a B-BBEE certificate": "sworn affidavit",
        "Can I save and continue later?": "signing out clears them",
        "Who can see my products?": "Only Icebolethu Group's procurement staff",
        "What is my supplier ID?": "IGSP",
        "I want to withdraw my application": "031 208 9102",
        "Can I choose more than one category?": "one supplier category",
    }
    for question, expected in faqs.items():
        assert expected in ask(client, "/chat", question)[1], question

    add_admin(client.app, "officer@icebolethu.co.za", "Officer", "applications")
    officer, _ = login_as(client.app, "officer@icebolethu.co.za")
    assert "/forgot-password" in ask(officer, "/admin/chat", "A supplier forgot their password")[1]
    assert "Return to supplier" in ask(officer, "/admin/chat", "How do I let a supplier edit an approved application?")[1]
    assert "doesn't have access" in ask(officer, "/admin/chat", "How do I place an order?")[1]  # no catalogue access


def test_cebelihle_greets_back(client):
    client.signup_and_login()
    # The chat opens empty: Cebelihle only speaks once the user has written something.
    assert "Hi, I'm Cebelihle" not in client.get("/dashboard").data.decode()
    for greeting in ["hi", "Hi!", "hii", "hie", "hello", "helo", "hey", "good day", "sawubona", "howzit", "how are you?"]:
        status, reply = ask(client, "/chat", greeting)
        assert status == 200 and reply.startswith("Hi, I'm Cebelihle, how can I help you?"), greeting
    # A page still running the previous widget sends the whole conversation; the last message is answered.
    resp = client.c.post("/chat", json={"messages": [{"role": "user", "content": "hi"}]}, headers={"X-CSRFToken": client.token()})
    assert resp.status_code == 200 and resp.get_json()["reply"].startswith("Hi, I'm Cebelihle")
    page = client.get("/dashboard").data.decode()
    assert re.search(r'/static/js/chatbot\.js\?v=\d+', page) and re.search(r'/static/css/icebolethu\.css\?v=\d+', page)


def test_cebelihle_more_knowledge(client):
    client.signup_and_login()
    reply = ask(client, "/chat", "Do you need caterers?")[1]
    assert "**Catering**" in reply and "/onboarding/category" in reply
    assert "**Caskets and Tombstones**" in ask(client, "/chat", "I sell coffins")[1]
    assert "one PDF" in ask(client, "/chat", "Can I upload multiple files?")[1]
    assert "Declaration of Interest" in ask(client, "/chat", "My family member works at Icebolethu")[1]
    assert "13-digit" in ask(client, "/chat", "My ID number is not accepted")[1]
    app_id = submit_application(client)
    assert f"/applications/{app_id}" in ask(client, "/chat", "Where can I view my application?")[1]

    add_admin(client.app, "officer@icebolethu.co.za", "Officer", "applications")
    officer, _ = login_as(client.app, "officer@icebolethu.co.za")
    assert f"#{app_id}" in ask(officer, "/admin/chat", "List unassigned applications")[1]
    assert "no open applications assigned to you" in ask(officer, "/admin/chat", "Which applications are assigned to me?")[1]
    officer.assign(app_id)
    assert f"Acme Caskets (#{app_id})" in ask(officer, "/admin/chat", "Which applications are assigned to me?")[1]
    assert "1** applications submitted" in ask(officer, "/admin/chat", "How many were approved this month?")[1]


def test_seed_audit_creates_and_removes_dummy_entries(client):
    client.admin_login()  # one real admin sign-in entry
    runner = client.app.test_cli_runner()
    result = runner.invoke(args=["seed-audit", "--days", "10"])
    assert result.exit_code == 0 and "dummy audit entries" in result.output
    with client.app.app_context():
        dummy = igsp.AuditLog.query.filter(igsp.AuditLog.ip_address.like("198.51.100.%"))
        assert dummy.count() > 0
        assert {e.actor_email for e in dummy.filter_by(action="admin_login")} <= {"admin@icebolethu.co.za"}
    assert runner.invoke(args=["seed-audit"]).exit_code != 0  # won't add a second batch
    assert runner.invoke(args=["seed-audit", "--remove"]).exit_code == 0
    with client.app.app_context():
        assert igsp.AuditLog.query.filter(igsp.AuditLog.ip_address.like("198.51.100.%")).count() == 0
        assert igsp.AuditLog.query.count() == 1  # the real admin sign-in is untouched
