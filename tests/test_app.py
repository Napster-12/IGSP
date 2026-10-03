import json
import re

import app as igsp
from conftest import PASSWORD, PROFILE, Client, pdf, submit_application


def test_public_pages_render(client):
    for url in ["/", "/login", "/signup", "/forgot-password", "/admin/login", "/health"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/no-such-page").status_code == 404


def test_signup_requires_email_verification(client):
    client.post("/signup", {
        "email": "New@Example.com", "password": PASSWORD, "confirm_password": PASSWORD,
        "company_name": "Co", "contact_name": "Me", "phone": "1",
    })
    # Not logged in until the code is entered.
    assert client.get("/dashboard").status_code == 302
    email, code = client.app.sent_codes[-1]
    assert email == "new@example.com"
    assert client.post("/verify-email", {"code": "000000" if code != "000000" else "111111"}).status_code == 302
    assert client.get("/dashboard").status_code == 302
    client.post("/verify-email", {"code": code})
    assert client.get("/dashboard").status_code == 200


def test_signup_rejects_weak_password(client):
    resp = client.post("/signup", {
        "email": "a@b.co", "password": "short", "confirm_password": "short",
        "company_name": "Co", "contact_name": "Me", "phone": "1",
    }, follow_redirects=True)
    assert b"at least 8 characters" in resp.data


def test_verification_code_locks_after_too_many_attempts(client):
    client.signup_and_login()
    client.get("/logout")
    client.post("/login", {"email": "supplier@example.com", "password": PASSWORD})
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
        client.post("/login", {"email": "supplier@example.com", "password": "wrong"})
    resp = client.post("/login", {"email": "supplier@example.com", "password": PASSWORD})
    assert b"Too many login attempts" in resp.data


def test_csrf_required(client):
    resp = client.post("/login", {"email": "x@y.z", "password": "p"}, csrf=False)
    assert resp.status_code == 302
    assert client.app.sent_codes == []


def test_full_onboarding_and_admin_review(client):
    client.signup_and_login()
    app_id = submit_application(client)

    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        assert application.status == "pending_review"
        assert application.supplier_category == "Caskets"
        assert len(application.directors) == 1
        docs = igsp.SupplierDocument.query.filter_by(application_id=app_id).all()
        assert len(docs) == 8  # 7 required + company profile
        assert igsp.Notification.query.filter(igsp.Notification.admin_id.isnot(None)).count() == 1

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
        assert {"Application Status Updated", "Account Activated", "Review Comments Added"} <= titles

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
        "categories": "ICT Equipment, Media and Electronic Devices", "category_detail_8": "Laptops",
        # Row 1 was removed in the browser, leaving a gap before row 2.
        "director_initials_2": "K. Nkosi", "director_id_2": "9202204720083",
        "director_role_2": "Shareholder", "director_nationality_2": "South Africa",
        "document_file_7": pdf("declaration-v2.pdf"),
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


def test_edit_validation_failure_does_not_lose_data(client):
    client.signup_and_login()
    app_id = submit_application(client)
    form = {**PROFILE, "company_name": "Acme", "contact_name": "Jane", "phone": "1", "categories": "Caskets",
            "director_nationality_0": ""}
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
    client.post("/onboarding/category", {"categories": "Caskets"})
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

    resp = client.post("/login", {"email": "supplier@example.com", "password": "NewPass1!"})
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
    result = runner.invoke(args=["seed-demo", "--suppliers", "30", "--months", "6", "--requests", "60"])
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
        assert igsp.Product.query.count() > 0 and igsp.ProductRequest.query.count() > 30
        assert igsp.ApplicationEvent.query.filter_by(action="assigned").count() > 0
        doc = igsp.SupplierDocument.query.first()
        assert open(igsp.absolute_upload_path(doc.file_path), "rb").read(5) == b"%PDF-"

    assert runner.invoke(args=["seed-demo"]).exit_code != 0  # refuses to duplicate

    admin = Client(app)
    admin.admin_login()
    for url in ["/admin", "/admin/applications?page=2", "/admin/reports", "/admin/reports/requests?period=all",
                "/admin/reports/suppliers", "/admin/reports/activity", "/admin/catalogue", "/admin/requests"]:
        assert admin.get(url).status_code == 200, url

    result = runner.invoke(args=["seed-demo", "--remove"])
    assert result.exit_code == 0 and "Removed 30" in result.output
    with app.app_context():
        assert igsp.User.query.count() == 0 and igsp.SupplierApplication.query.count() == 0
        assert igsp.ProductRequest.query.count() == 0 and igsp.SupplierDocument.query.count() == 0
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
    client.post("/onboarding/category", {"categories": "Caskets"})

    # Before documents are uploaded: missing docs are flagged and submit is disabled.
    page = client.get("/onboarding/review").data.decode()
    assert "Required — not uploaded" in page and "disabled" in page

    client.post("/onboarding/documents", {f"document_file_{i}": pdf(f"doc{i}.pdf") for i in range(1, 8)},
                content_type="multipart/form-data")
    page = client.get("/onboarding/review").data.decode()
    for text in ["Company Details", "Company Address", "Durban", "KwaZulu-Natal", "Directors &amp; Shareholders",
                 "8001015009087", "Caskets", "doc7.pdf", "Required documents (7/7)"]:
        assert text in page, text
    assert "Required — not uploaded" not in page

    resp = client.post("/onboarding/review")  # no confirmation tick
    assert resp.headers["Location"].endswith("/onboarding/review")
    resp = client.post("/onboarding/review", {"confirm": "1"})
    assert resp.headers["Location"].endswith("/dashboard")


def test_admin_pages_share_layout_and_decline_needs_comment(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = Client(client.app)
    admin.admin_login()
    admin.assign(app_id)

    pages = {"/admin": "Dashboard", "/admin/applications": "Applications", f"/admin/applications/{app_id}": "Applications",
             "/admin/suppliers": "Suppliers", "/admin/reports": "Reports", "/admin/notifications": "Notifications"}
    for url, active in pages.items():
        page = admin.get(url).data.decode()
        assert 'class="ad-shell"' in page, url
        assert re.search(r'is-active"\s+aria-current="page">.*?<span>' + active + '</span>', page, re.S), url

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
    page = client.get("/admin/login").data.decode()
    assert "Admin sign in" in page and "/signup" not in page


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
    page = admin.get("/admin/reports?category=Caskets").data.decode()
    category_select = page.split('name="category"')[1].split("</select>")[0]
    assert 'value="Catering"' in category_select and 'value="Caskets" selected' in category_select

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
            "phone": "0820000000", "categories": "Caskets", "document_file_7": pdf("declaration-signed.pdf")}
    client.post(f"/applications/{app_id}/edit", form, content_type="multipart/form-data")
    admin.post(f"/admin/applications/{app_id}/update",
               {"status": "approved", "documents_status": "complete", "review_comments": "Wrong tax number"})
    admin.get(f"/admin/applications/{app_id}/download-all")

    with client.app.app_context():
        events = igsp.ApplicationEvent.query.filter_by(application_id=app_id).order_by(igsp.ApplicationEvent.id).all()
        assert [e.action for e in events] == [
            "submitted", "assigned", "status_changed", "resubmitted", "status_changed", "account_activated",
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
                                               company_profile="", supplier_category="Caskets", assigned_admin_id=admin.id)
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


PRODUCT = {"name": "Pine casket", "category": "Caskets", "description": "Standard size", "unit": "each",
           "price": "4 500,00", "quantity_available": "5", "lead_time_days": "2", "is_listed": "1"}


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


def test_product_request_lifecycle(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)
    client.post("/inventory/new", {**PRODUCT, "name": "Hidden casket", "is_listed": ""})
    with client.app.app_context():
        product_id = igsp.Product.query.filter_by(name="Pine casket").one().id

    catalogue = admin.get("/admin/catalogue").data.decode()
    assert "Pine casket" in catalogue and "Hidden casket" not in catalogue and "R 4 500.00" in catalogue

    # Can't request more than is in stock.
    resp = admin.post(f"/admin/catalogue/{product_id}/request", {"quantity": "9", "delivery_location": "Durban branch"})
    assert resp.status_code == 400
    admin.post(f"/admin/catalogue/{product_id}/request",
               {"quantity": "3", "delivery_location": "Durban branch", "notes": "For Saturday"})
    with client.app.app_context():
        req = igsp.ProductRequest.query.one()
        req_id, ref = req.id, req.reference
        assert (req.status, str(req.total)) == ("requested", "13500.00")
        supplier = igsp.find_user_by_email("supplier@example.com")
        assert igsp.Notification.query.filter_by(user_id=supplier.id, title=f"New product request {ref}").count() == 1

    assert ref in client.get("/requests").data.decode()
    client.post(f"/requests/{req_id}/respond", {"action": "decline"})  # needs a note
    client.post(f"/requests/{req_id}/respond", {"action": "accept", "note": "Ready Friday"})
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.ProductRequest, req_id).status == "accepted"
        assert igsp.DB.session.get(igsp.Product, product_id).quantity_available == 2  # stock reserved

    admin.post(f"/admin/requests/{req_id}/update", {"action": "complete"})  # not delivered yet
    client.post(f"/requests/{req_id}/respond", {"action": "deliver", "note": "Signed by T. Zulu"})
    admin.post(f"/admin/requests/{req_id}/update", {"action": "complete"})
    with client.app.app_context():
        req = igsp.DB.session.get(igsp.ProductRequest, req_id)
        assert req.status == "completed"
        assert [e.action for e in reversed(req.events)] == ["requested", "accept", "deliver", "complete"]
    assert "Signed by T. Zulu" in admin.get(f"/admin/requests/{req_id}").data.decode()


def test_cancel_returns_stock_and_inactive_suppliers_are_hidden(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)
    with client.app.app_context():
        product_id = igsp.Product.query.one().id
        supplier_id = igsp.find_user_by_email("supplier@example.com").id
    admin.post(f"/admin/catalogue/{product_id}/request", {"quantity": "2", "delivery_location": "HQ"})
    with client.app.app_context():
        req_id = igsp.ProductRequest.query.one().id
    client.post(f"/requests/{req_id}/respond", {"action": "accept"})
    admin.post(f"/admin/requests/{req_id}/update", {"action": "cancel"})  # needs a reason
    admin.post(f"/admin/requests/{req_id}/update", {"action": "cancel", "note": "Event postponed"})
    with client.app.app_context():
        assert igsp.DB.session.get(igsp.ProductRequest, req_id).status == "cancelled"
        assert igsp.DB.session.get(igsp.Product, product_id).quantity_available == 5

    # Other suppliers can't see this supplier's requests.
    other = Client(client.app)
    other.signup_and_login("other@example.com")
    assert other.get(f"/requests/{req_id}").status_code == 302  # inactive → dashboard

    # Deactivating the supplier removes their products from the catalogue and blocks their inventory.
    admin.post(f"/admin/suppliers/{supplier_id}/toggle")
    assert "Pine casket" not in admin.get("/admin/catalogue").data.decode()
    assert admin.get(f"/admin/catalogue/{product_id}/request").headers["Location"].endswith("/admin/catalogue")
    assert client.get("/inventory").headers["Location"].endswith("/dashboard")


def test_quotation_only_after_supplier_accepts(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)
    with client.app.app_context():
        product_id = igsp.Product.query.one().id
    admin.post(f"/admin/catalogue/{product_id}/request", {"quantity": "2", "delivery_location": "Durban branch"})
    with client.app.app_context():
        req_id = igsp.ProductRequest.query.one().id

    # Not accepted yet: no quotation in any format, and the page says so.
    for url in (f"/admin/requests/{req_id}/quotation", f"/admin/requests/{req_id}/quotation.pdf"):
        assert admin.get(url).headers["Location"].endswith(f"/admin/requests/{req_id}")
    page = admin.get(f"/admin/requests/{req_id}").data.decode()
    assert "Quotation available once accepted" in page and "quotation.pdf" not in page

    client.post(f"/requests/{req_id}/respond", {"action": "accept"})
    page = admin.get(f"/admin/requests/{req_id}").data.decode()
    assert f"/admin/requests/{req_id}/quotation.pdf" in page
    assert f"/admin/requests/{req_id}/quotation.pdf" in admin.get("/admin/requests").data.decode()

    html = admin.get(f"/admin/requests/{req_id}/quotation").data.decode()
    assert "QT-00001" in html and "Pine casket" in html and "R 9 000.00" in html
    assert "Acme Caskets (Pty) Ltd" in html and "2020/123456/07" in html and "Approved for payment" in html
    assert "Not VAT registered" in html and "VAT (15%)" not in html

    pdf = admin.get(f"/admin/requests/{req_id}/quotation.pdf")
    assert pdf.status_code == 200 and pdf.mimetype == "application/pdf" and pdf.data[:5] == b"%PDF-"
    assert "attachment" in pdf.headers["Content-Disposition"] and "QT-00001_Acme_Caskets.pdf" in pdf.headers["Content-Disposition"]

    with client.app.app_context():
        application = igsp.DB.session.get(igsp.SupplierApplication, app_id)
        application.vat_number = "4123456789"
        igsp.DB.session.commit()
    html = admin.get(f"/admin/requests/{req_id}/quotation").data.decode()
    assert "VAT (15%)" in html and "R 1 173.91" in html and "R 7 826.09" in html
    assert admin.get(f"/admin/requests/{req_id}/quotation.pdf").data[:5] == b"%PDF-"

    # Still available after delivery and completion; never to suppliers.
    client.post(f"/requests/{req_id}/respond", {"action": "deliver"})
    admin.post(f"/admin/requests/{req_id}/update", {"action": "complete"})
    assert admin.get(f"/admin/requests/{req_id}/quotation.pdf").status_code == 200
    assert client.get(f"/admin/requests/{req_id}/quotation.pdf").status_code == 302
    with client.app.app_context():
        notes = [e.note for e in igsp.ProductRequestEvent.query.filter_by(request_id=req_id, action="quotation")]
        assert notes.count("Quotation downloaded as PDF") == 3 and notes.count("Quotation opened for printing") == 2


def test_additional_reports(client):
    client.signup_and_login()
    app_id = submit_application(client)
    admin = approve_supplier(client, app_id)
    client.post("/inventory/new", PRODUCT)
    with client.app.app_context():
        product_id = igsp.Product.query.one().id
    admin.post(f"/admin/catalogue/{product_id}/request", {"quantity": "2", "delivery_location": "HQ"})
    with client.app.app_context():
        req_id = igsp.ProductRequest.query.one().id
    client.post(f"/requests/{req_id}/respond", {"action": "accept"})

    # The applications report shows the report buttons too.
    page = admin.get("/admin/reports").data.decode()
    assert 'class="ad-report-tabs"' in page and "/admin/reports/requests" in page

    page = admin.get("/admin/reports/requests").data.decode()
    assert "R 9 000.00" in page and "PR-00001" in page and "Committed value" in page
    assert "Product requests</a>" in page and 'aria-current="page">Product requests' in page

    page = admin.get("/admin/reports/suppliers?period=all").data.decode()
    assert "Acme Caskets" in page and "Registrations per month" in page

    page = admin.get("/admin/reports/inventory").data.decode()
    assert "Pine casket" in page and "Live snapshot" in page

    page = admin.get("/admin/reports/activity?period=30d").data.decode()
    assert "Assigned to admin" in page and "Status changed" in page and "Product requested" in page

    csv_data = admin.get("/admin/reports/requests/export?period=all").data.decode("utf-8-sig")
    assert csv_data.splitlines()[0].startswith("Reference,Date,Product") and "9000.00" in csv_data

    assert admin.get("/admin/reports/nonsense").status_code == 404
    assert admin.get("/admin/reports/requests?period=bogus").status_code == 200  # falls back to 12 months
    assert client.get("/admin/reports/requests").status_code == 302  # suppliers can't see reports
