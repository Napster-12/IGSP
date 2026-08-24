import os
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime
from urllib.parse import quote_plus
import base64
import io

from flask import Flask, flash, jsonify, redirect, render_template, request, session, send_file, url_for
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import text
from werkzeug.security import check_password_hash, generate_password_hash
from authlib.integrations.flask_client import OAuth
import mimetypes
import pyotp
import qrcode

app = Flask(__name__)
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "igsp-dev-key")

oauth = OAuth(app)
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "")
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "")
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common")
MICROSOFT_AUTHORITY = f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}/v2.0"
MICROSOFT_SCOPE = ["openid", "profile", "email", "User.Read"]

if MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET:
    oauth.register(
        name="microsoft",
        client_id=MICROSOFT_CLIENT_ID,
        client_secret=MICROSOFT_CLIENT_SECRET,
        authority=MICROSOFT_AUTHORITY,
        access_token_url=f"{MICROSOFT_AUTHORITY}/oauth2/v2.0/token",
        authorize_url=f"{MICROSOFT_AUTHORITY}/oauth2/v2.0/authorize",
        userinfo_endpoint="https://graph.microsoft.com/oidc/userinfo",
        client_kwargs={"scope": " ".join(MICROSOFT_SCOPE)},
    )

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "@@Codnell12")
MYSQL_DB = os.getenv("MYSQL_DB", "igsp")

app.config["SQLALCHEMY_DATABASE_URI"] = (
    f"mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD)}@"
    f"{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
)
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

DB = SQLAlchemy(app)


SUPPLIER_CATEGORIES = [
    {"value": "Catering", "label": "Catering", "specify": False, "group": "SERVICES"},
    {"value": "Body Storage and other Burial Services", "label": "Body Storage and other Burial Services", "specify": False, "group": "SERVICES"},
    {"value": "Consulting", "label": "Consulting", "specify": False, "group": "SERVICES"},
    {"value": "Other - Services", "label": "Other, please specify:", "specify": True, "group": "SERVICES"},
    {"value": "Caskets", "label": "Caskets", "specify": False, "group": "GOODS"},
    {"value": "Livestock", "label": "Livestock", "specify": False, "group": "GOODS"},
    {"value": "Flowers, crosses and plaques", "label": "Flowers, crosses and plaques", "specify": False, "group": "GOODS"},
    {"value": "ICT Equipment", "label": "ICT Equipment, please specify:", "specify": True, "group": "GOODS"},
    {"value": "Tents", "label": "Tents", "specify": False, "group": "Burials related rentals and Purchases"},
    {"value": "Draping and Décor", "label": "Draping and Décor", "specify": False, "group": "Burials related rentals and Purchases"},
    {"value": "Family Car", "label": "Family Car, please specify make:", "specify": True, "group": "Burials related rentals and Purchases"},
    {"value": "Hearse", "label": "Hearse, please specify make:", "specify": True, "group": "Burials related rentals and Purchases"},
    {"value": "Lowering Device", "label": "Lowering Device", "specify": False, "group": "Burials related rentals and Purchases"},
    {"value": "Cold-Room", "label": "Cold-Room", "specify": False, "group": "Burials related rentals and Purchases"},
    {"value": "Media/Electronic Devices", "label": "Media/Electronic Devices, please specify:", "specify": True, "group": "Burials related rentals and Purchases"},
    {"value": "Mobile toilet", "label": "Mobile toilet", "specify": False, "group": "Burials related rentals and Purchases"},
    {"value": "Other - Rentals", "label": "Other (Please Specify)", "specify": True, "group": "Burials related rentals and Purchases"},
]

LEGACY_CATEGORY_MAP = {
    "Catering services": "Catering",
    "Other": "Other - Services",
}


def normalize_category_value(value):
    if value in LEGACY_CATEGORY_MAP:
        return LEGACY_CATEGORY_MAP[value]
    return value


def get_specify_categories():
    """Return set of category values that require a detail/specification."""
    return {cat["value"] for cat in SUPPLIER_CATEGORIES if cat.get("specify")}


def get_category_sections():
    """Group SUPPLIER_CATEGORIES into sections for checkbox rendering."""
    sections = []
    current = {"name": "", "items": []}
    for i, cat in enumerate(SUPPLIER_CATEGORIES):
        group = cat.get("group", "")
        if group != current["name"]:
            if current["items"]:
                sections.append(current)
            current = {"name": group, "items": []}
        item = dict(cat)
        item["index"] = i
        current["items"].append(item)
    if current["items"]:
        sections.append(current)
    return sections


class SupplierApplicationCategory(DB.Model):
    __tablename__ = "supplier_application_categories"

    id = DB.Column(DB.Integer, primary_key=True)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=False)
    category = DB.Column(DB.String(120), nullable=False)
    category_detail = DB.Column(DB.String(255), default="")


class SupplierApplication(DB.Model):
    __tablename__ = "supplier_applications"

    id = DB.Column(DB.Integer, primary_key=True)
    company_name = DB.Column(DB.String(150), nullable=False)
    contact_name = DB.Column(DB.String(120), nullable=False)
    email = DB.Column(DB.String(120), nullable=False)
    phone = DB.Column(DB.String(50), nullable=False)
    supplier_category = DB.Column(DB.String(120), nullable=False)
    supplier_category_detail = DB.Column(DB.String(255), default="")
    company_profile = DB.Column(DB.Text, nullable=False)
    documents_status = DB.Column(DB.String(50), default="pending")
    status = DB.Column(DB.String(50), default="submitted")
    review_comments = DB.Column(DB.Text, default="")
    created_at = DB.Column(DB.DateTime, default=datetime.utcnow)
    updated_at = DB.Column(DB.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    registered_vendor_name = DB.Column(DB.String(150), default="")
    trading_name = DB.Column(DB.String(150), default="")
    business_registration_number = DB.Column(DB.String(120), default="")
    vat_number = DB.Column(DB.String(50), default="")
    tax_number = DB.Column(DB.String(50), default="")
    physical_address = DB.Column(DB.String(255), default="")
    city = DB.Column(DB.String(100), default="")
    province = DB.Column(DB.String(100), default="")
    postal_code = DB.Column(DB.String(20), default="")
    website = DB.Column(DB.String(255), default="")
    primary_contact_person = DB.Column(DB.String(120), default="")
    contact_person_role = DB.Column(DB.String(120), default="")
    contact_number = DB.Column(DB.String(50), default="")
    email_address = DB.Column(DB.String(120), default="")

    application_categories = DB.relationship(
        "SupplierApplicationCategory",
        backref="application",
        cascade="all, delete-orphan",
        lazy=True,
    )

    directors = DB.relationship(
        "SupplierDirector",
        backref="application",
        cascade="all, delete-orphan",
        lazy=True,
    )


class SupplierDocument(DB.Model):
    __tablename__ = "supplier_documents"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=False)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=True)
    document_type = DB.Column(DB.String(120), nullable=False)
    file_path = DB.Column(DB.String(255), nullable=False)
    original_filename = DB.Column(DB.String(255), nullable=False)
    uploaded_at = DB.Column(DB.DateTime, default=datetime.utcnow)


class SupplierDirector(DB.Model):
    __tablename__ = "supplier_directors"

    id = DB.Column(DB.Integer, primary_key=True)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=False)
    initials_surname = DB.Column(DB.String(120), nullable=False)
    id_number = DB.Column(DB.String(50), nullable=True)
    role = DB.Column(DB.String(120), nullable=False)
    nationality = DB.Column(DB.String(80), nullable=False)


class Notification(DB.Model):
    __tablename__ = "notifications"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=True)
    admin_id = DB.Column(DB.Integer, DB.ForeignKey("admins.id"), nullable=True)
    title = DB.Column(DB.String(150), nullable=False)
    message = DB.Column(DB.Text, nullable=False)
    is_read = DB.Column(DB.Boolean, nullable=False, default=False)
    related_application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=True)
    created_at = DB.Column(DB.DateTime, default=datetime.utcnow)

    user = DB.relationship("User", backref=DB.backref("notifications", lazy=True))
    admin = DB.relationship("Admin", backref=DB.backref("notifications", lazy=True))
    application = DB.relationship("SupplierApplication", backref=DB.backref("notifications", lazy=True))


class User(DB.Model):
    __tablename__ = "users"

    id = DB.Column(DB.Integer, primary_key=True)
    supplier_id = DB.Column(DB.String(20), unique=True, nullable=True)
    name = DB.Column("name", DB.String(100), nullable=False, default="")
    email = DB.Column(DB.String(120), unique=True, nullable=False)
    password_hash = DB.Column(DB.String(256), nullable=False)
    company_name = DB.Column(DB.String(150), nullable=False)
    contact_name = DB.Column(DB.String(120), nullable=False)
    phone = DB.Column(DB.String(50), nullable=False)
    address = DB.Column(DB.String(255), nullable=False, default="")
    active = DB.Column(DB.Boolean, nullable=False, default=False)
    created_at = DB.Column(DB.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Admin(DB.Model):
    __tablename__ = "admins"

    id = DB.Column(DB.Integer, primary_key=True)
    email = DB.Column(DB.String(120), unique=True, nullable=False)
    password_hash = DB.Column(DB.String(256), nullable=False)
    name = DB.Column(DB.String(100), nullable=False, default="")
    created_at = DB.Column(DB.DateTime, default=datetime.utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class User2FA(DB.Model):
    __tablename__ = "user_2fa"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=False, unique=True)
    totp_secret = DB.Column(DB.String(64), nullable=False)
    confirmed = DB.Column(DB.Boolean, nullable=False, default=False)
    created_at = DB.Column(DB.DateTime, default=datetime.utcnow)

    user = DB.relationship("User", backref=DB.backref("two_fa", uselist=False))


@app.route("/")
def home():
    user = get_current_user()
    return render_template("index.html", user=user)


def get_onboarding_draft():
    draft = session.get("supplier_onboarding", {})
    user = get_current_user()
    if not draft or (user and draft.get("email") != user.email):
        draft = {
            "company_name": user.company_name if user else "",
            "contact_name": user.contact_name if user else "",
            "email": user.email if user else "",
            "phone": user.phone if user else "",
            "address": user.address if user else "",
            "supplier_category": "",
            "supplier_categories": [],
            "supplier_category_detail": "",
            "company_profile": "",
            "documents_status": "pending",
            "registered_vendor_name": "",
            "trading_name": "",
            "business_registration_number": "",
            "vat_number": "",
            "tax_number": "",
            "physical_address": "",
            "city": "",
            "province": "",
            "postal_code": "",
            "website": "",
            "primary_contact_person": "",
            "contact_person_role": "",
            "contact_number": "",
            "email_address": "",
            "directors": [],
        }
    session["supplier_onboarding"] = draft
    return draft


@app.route("/onboarding/profile", methods=["GET", "POST"])
def onboarding_profile():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    draft = get_onboarding_draft()

    if request.method == "POST":
        draft["registered_vendor_name"] = request.form.get("registered_vendor_name", "").strip()
        draft["trading_name"] = request.form.get("trading_name", "").strip()
        draft["business_registration_number"] = request.form.get("business_registration_number", "").strip()
        draft["vat_number"] = request.form.get("vat_number", "").strip()
        draft["tax_number"] = request.form.get("tax_number", "").strip()
        draft["physical_address"] = request.form.get("physical_address", "").strip()
        draft["city"] = request.form.get("city", "").strip()
        draft["province"] = request.form.get("province", "").strip()
        draft["postal_code"] = request.form.get("postal_code", "").strip()
        draft["website"] = request.form.get("website", "").strip()
        draft["primary_contact_person"] = request.form.get("primary_contact_person", "").strip()
        draft["contact_person_role"] = request.form.get("contact_person_role", "").strip()
        draft["contact_number"] = request.form.get("contact_number", "").strip()
        draft["email_address"] = request.form.get("email_address", "").strip()

        directors = []
        idx = 0
        while True:
            name = request.form.get(f"director_initials_{idx}", "").strip()
            id_number = request.form.get(f"director_id_{idx}", "").strip()
            role = request.form.get(f"director_role_{idx}", "").strip()
            nationality = request.form.get(f"director_nationality_{idx}", "").strip()
            if not any([name, id_number, role, nationality]):
                break
            directors.append({
                "initials_surname": name,
                "id_number": id_number,
                "role": role,
                "nationality": nationality,
            })
            idx += 1
        draft["directors"] = directors

        required = [
            draft["company_name"], draft["contact_name"], draft["email"], draft["phone"],
            draft["registered_vendor_name"],
            draft["business_registration_number"], draft["tax_number"],
            draft["physical_address"], draft["city"], draft["province"], draft["postal_code"],
            draft["primary_contact_person"], draft["contact_person_role"], draft["contact_number"], draft["email_address"],
        ]
        if not all(required):
            flash("Please complete all required company profile fields.", "error")
            return redirect(url_for("onboarding_profile"))

        if not draft.get("directors"):
            flash("Please provide at least one director or shareholder.", "error")
            return redirect(url_for("onboarding_profile"))

        company_profile_file = request.files.get("document_file_company_profile")
        existing_profile_doc = SupplierDocument.query.filter_by(user_id=user.id, document_type="Company Profile").first()
        has_company_profile = existing_profile_doc or (company_profile_file and company_profile_file.filename)
        has_website = draft.get("website", "").strip()
        if not has_company_profile and not has_website:
            flash("Please upload your Company Profile or provide a company website.", "error")
            return redirect(url_for("onboarding_profile"))

        if company_profile_file and company_profile_file.filename:
            if not company_profile_file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are allowed.", "error")
                return redirect(url_for("onboarding_profile"))
            upload_dir = os.path.join("static", "uploads", str(user.id))
            os.makedirs(upload_dir, exist_ok=True)

            filename = f"{int(datetime.utcnow().timestamp())}_{company_profile_file.filename}"
            relative_path = f"uploads/{user.id}/{filename}"
            absolute_path = os.path.join("static", "uploads", str(user.id), filename)
            company_profile_file.save(absolute_path)

            if existing_profile_doc:
                old_path = os.path.join("static", existing_profile_doc.file_path.replace("/", os.sep))
                if os.path.exists(old_path):
                    os.remove(old_path)
                existing_profile_doc.file_path = relative_path
                existing_profile_doc.original_filename = company_profile_file.filename
            else:
                document = SupplierDocument(
                    user_id=user.id,
                    document_type="Company Profile",
                    file_path=relative_path,
                    original_filename=company_profile_file.filename,
                )
                DB.session.add(document)
            DB.session.commit()

        session["supplier_onboarding"] = draft
        return redirect(url_for("onboarding_category"))

    return render_template("supplier_profile.html", user=user, draft=draft, unread_notifications=Notification.query.filter_by(user_id=user.id, is_read=False).count(), company_profile_doc=SupplierDocument.query.filter_by(user_id=user.id, document_type="Company Profile").first())


@app.route("/onboarding/category", methods=["GET", "POST"])
def onboarding_category():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    draft = get_onboarding_draft()

    if request.method == "POST":
        selected_categories = request.form.getlist("categories")
        draft["supplier_categories"] = selected_categories

        category_details = {}
        for i, cat in enumerate(SUPPLIER_CATEGORIES):
            if cat.get("specify") and cat["value"] in selected_categories:
                detail = request.form.get(f"category_detail_{i}", "").strip()
                if not detail:
                    flash(f"Please provide details for '{cat['label']}'.", "error")
                    return redirect(url_for("onboarding_category"))
                category_details[cat["value"]] = detail

        draft["supplier_category_details"] = category_details
        draft["supplier_category"] = selected_categories[0] if selected_categories else ""

        if not selected_categories:
            flash("Please select at least one supplier category.", "error")
            return redirect(url_for("onboarding_category"))

        session["supplier_onboarding"] = draft
        return redirect(url_for("onboarding_documents"))

    return render_template("supplier_category.html", user=user, draft=draft, unread_notifications=Notification.query.filter_by(user_id=user.id, is_read=False).count())


@app.route("/onboarding/documents", methods=["GET", "POST"])
def onboarding_documents():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    draft = get_onboarding_draft()

    if request.method == "POST":
        doc_types = [
            "CIPC Company Registration Document",
            "Certified ID copies of all Directors",
            "SARS VAT Certificate",
            "Confirmation of Bank Account Letter (not older than 3 months)",
            "Valid B-BBEE certificate, letter from Accountant or Sworn Affidavit",
            "Proof of Company residential address (not older than 3 months)",
            "Declaration Form",
        ]

        draft["documents_status"] = "pending"

        for index, doc_type in enumerate(doc_types, start=1):
            file = request.files.get(f"document_file_{index}")
            if file and file.filename:
                if not file.filename.lower().endswith(".pdf"):
                    flash("Only PDF files are allowed.", "error")
                    return redirect(url_for("onboarding_documents"))
                upload_dir = os.path.join("static", "uploads", str(user.id))
                os.makedirs(upload_dir, exist_ok=True)

                filename = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
                relative_path = f"uploads/{user.id}/{filename}"
                absolute_path = os.path.join("static", "uploads", str(user.id), filename)
                file.save(absolute_path)

                existing = SupplierDocument.query.filter_by(
                    user_id=user.id, document_type=doc_type
                ).first()
                if existing:
                    existing.file_path = relative_path
                    existing.original_filename = file.filename
                else:
                    document = SupplierDocument(
                        user_id=user.id,
                        document_type=doc_type,
                        file_path=relative_path,
                        original_filename=file.filename,
                    )
                    DB.session.add(document)

        other_file = request.files.get("document_file_other")
        if other_file and other_file.filename:
            if not other_file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are allowed.", "error")
                return redirect(url_for("onboarding_documents"))
            upload_dir = os.path.join("static", "uploads", str(user.id))
            os.makedirs(upload_dir, exist_ok=True)

            filename = f"{int(datetime.utcnow().timestamp())}_{other_file.filename}"
            relative_path = f"uploads/{user.id}/{filename}"
            absolute_path = os.path.join("static", "uploads", str(user.id), filename)
            other_file.save(absolute_path)

            existing = SupplierDocument.query.filter_by(
                user_id=user.id, document_type="Other Supporting Documents"
            ).first()
            if existing:
                old_path = os.path.join("static", existing.file_path.replace("/", os.sep))
                if os.path.exists(old_path):
                    os.remove(old_path)
                existing.file_path = relative_path
                existing.original_filename = other_file.filename
            else:
                document = SupplierDocument(
                    user_id=user.id,
                    document_type="Other Supporting Documents",
                    file_path=relative_path,
                    original_filename=other_file.filename,
                )
                DB.session.add(document)

        DB.session.commit()

        uploaded_types = {
            doc.document_type
            for doc in SupplierDocument.query.filter_by(user_id=user.id).all()
        }
        missing_docs = [doc_type for doc_type in doc_types if doc_type not in uploaded_types]
        if missing_docs:
            flash(
                "Please upload all required documents before continuing. "
                "Missing: " + ", ".join(missing_docs),
                "error",
            )
            return redirect(url_for("onboarding_documents"))

        draft["documents_status"] = "complete"
        session["supplier_onboarding"] = draft
        return redirect(url_for("onboarding_review"))

    documents = SupplierDocument.query.filter_by(user_id=user.id).all()
    return render_template("supplier_documents.html", user=user, draft=draft, documents=documents, unread_notifications=Notification.query.filter_by(user_id=user.id, is_read=False).count())


@app.route("/onboarding/review", methods=["GET", "POST"])
def onboarding_review():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    draft = get_onboarding_draft()

    if request.method == "POST":
        required_base = [
            draft.get("company_name"), draft.get("contact_name"), draft.get("email"),
            draft.get("phone"), draft.get("supplier_category"),
            draft.get("registered_vendor_name"),
            draft.get("business_registration_number"), draft.get("tax_number"),
            draft.get("physical_address"), draft.get("city"), draft.get("province"), draft.get("postal_code"),
            draft.get("primary_contact_person"), draft.get("contact_person_role"),
            draft.get("contact_number"), draft.get("email_address"),
        ]
        if not all(required_base):
            flash("Please complete every onboarding step before submitting.", "error")
            return redirect(url_for("onboarding_profile"))

        if not draft.get("directors"):
            flash("Please provide at least one director or shareholder.", "error")
            return redirect(url_for("onboarding_profile"))

        has_company_profile = SupplierDocument.query.filter_by(user_id=user.id, document_type="Company Profile").first() is not None
        has_website = draft.get("website", "").strip()
        if not has_company_profile and not has_website:
            flash("Please upload your Company Profile or provide a company website.", "error")
            return redirect(url_for("onboarding_profile"))

        application = SupplierApplication.query.filter_by(email=draft["email"]).first()
        if not application:
            application = SupplierApplication(
                company_name=draft["company_name"],
                contact_name=draft["contact_name"],
                email=draft["email"],
                phone=draft["phone"],
            supplier_category=draft["supplier_category"],
            supplier_category_detail=draft.get("supplier_category_detail", ""),
            company_profile="",
            documents_status=draft.get("documents_status", "pending"),
            status="pending_review",
                registered_vendor_name=draft.get("registered_vendor_name", ""),
                trading_name=draft.get("trading_name", ""),
                business_registration_number=draft.get("business_registration_number", ""),
                vat_number=draft.get("vat_number", ""),
                tax_number=draft.get("tax_number", ""),
                physical_address=draft.get("physical_address", ""),
                city=draft.get("city", ""),
                province=draft.get("province", ""),
                postal_code=draft.get("postal_code", ""),
                website=draft.get("website", ""),
                primary_contact_person=draft.get("primary_contact_person", ""),
                contact_person_role=draft.get("contact_person_role", ""),
                contact_number=draft.get("contact_number", ""),
                email_address=draft.get("email_address", ""),
            )
            DB.session.add(application)
        else:
            application.company_name = draft["company_name"]
            application.contact_name = draft["contact_name"]
            application.phone = draft["phone"]
        application.supplier_category = draft["supplier_category"]
        application.supplier_category_detail = draft.get("supplier_category_detail", "")
        application.documents_status = draft.get("documents_status", "pending")
        application.status = "pending_review"
        application.registered_vendor_name = draft.get("registered_vendor_name", "")
        application.trading_name = draft.get("trading_name", "")
        application.business_registration_number = draft.get("business_registration_number", "")
        application.vat_number = draft.get("vat_number", "")
        application.tax_number = draft.get("tax_number", "")
        application.physical_address = draft.get("physical_address", "")
        application.city = draft.get("city", "")
        application.province = draft.get("province", "")
        application.postal_code = draft.get("postal_code", "")
        application.website = draft.get("website", "")
        application.primary_contact_person = draft.get("primary_contact_person", "")
        application.contact_person_role = draft.get("contact_person_role", "")
        application.contact_number = draft.get("contact_number", "")
        application.email_address = draft.get("email_address", "")

        DB.session.commit()

        SupplierApplicationCategory.query.filter_by(application_id=application.id).delete()
        DB.session.commit()

        for cat_value in draft.get("supplier_categories", []):
            detail = draft.get("supplier_category_details", {}).get(cat_value, "")
            cat_obj = SupplierApplicationCategory(
                application_id=application.id,
                category=cat_value,
                category_detail=detail,
            )
            DB.session.add(cat_obj)

        DB.session.commit()

        SupplierDirector.query.filter_by(application_id=application.id).delete()
        DB.session.commit()
        for director in draft.get("directors", []):
            director_obj = SupplierDirector(
                application_id=application.id,
                initials_surname=director.get("initials_surname", ""),
                id_number=director.get("id_number", ""),
                role=director.get("role", ""),
                nationality=director.get("nationality", ""),
            )
            DB.session.add(director_obj)
        DB.session.commit()

        SupplierDocument.query.filter_by(user_id=user.id, application_id=None).update({"application_id": application.id})
        DB.session.commit()

        for admin in Admin.query.all():
            notification = Notification(
                admin_id=admin.id,
                title="New Supplier Application",
                message=f"New application submitted by {user.company_name} ({user.contact_name}) for {draft.get('supplier_category', 'N/A')}.",
                related_application_id=application.id,
            )
            DB.session.add(notification)
        DB.session.commit()

        session.pop("supplier_onboarding", None)
        flash("Supplier application submitted successfully.", "success")
        return redirect(url_for("dashboard"))

    documents = SupplierDocument.query.filter_by(user_id=user.id).all()
    return render_template("supplier_review.html", user=user, draft=draft, documents=documents, unread_notifications=Notification.query.filter_by(user_id=user.id, is_read=False).count())


@app.route("/register", methods=["POST"])
def register_supplier():
    draft = get_onboarding_draft()
    required = [
        draft.get("company_name"), draft.get("contact_name"), draft.get("email"),
        draft.get("phone"), draft.get("supplier_category"),
        draft.get("registered_vendor_name"),
        draft.get("business_registration_number"), draft.get("tax_number"),
        draft.get("physical_address"), draft.get("city"), draft.get("province"), draft.get("postal_code"),
        draft.get("primary_contact_person"), draft.get("contact_person_role"),
        draft.get("contact_number"), draft.get("email_address"),
    ]
    if not all(required):
        flash("Please complete all supplier details before submitting.", "error")
        return redirect(url_for("onboarding_profile"))

    if not draft.get("directors"):
        flash("Please provide at least one director or shareholder.", "error")
        return redirect(url_for("onboarding_profile"))

    has_company_profile = SupplierDocument.query.filter_by(user_id=user.id, document_type="Company Profile").first() is not None
    has_website = draft.get("website", "").strip()
    if not has_company_profile and not has_website:
        flash("Please upload your Company Profile or provide a company website.", "error")
        return redirect(url_for("onboarding_profile"))

    application = SupplierApplication.query.filter_by(email=draft["email"]).first()
    if not application:
        application = SupplierApplication(
            company_name=draft["company_name"],
            contact_name=draft["contact_name"],
            email=draft["email"],
            phone=draft["phone"],
            supplier_category=draft["supplier_category"],
                supplier_category_detail=draft.get("supplier_category_detail", ""),
                company_profile="",
                documents_status=draft.get("documents_status", "pending"),
            status="pending_review",
            registered_vendor_name=draft.get("registered_vendor_name", ""),
            trading_name=draft.get("trading_name", ""),
            business_registration_number=draft.get("business_registration_number", ""),
            vat_number=draft.get("vat_number", ""),
            tax_number=draft.get("tax_number", ""),
            physical_address=draft.get("physical_address", ""),
            city=draft.get("city", ""),
            province=draft.get("province", ""),
            postal_code=draft.get("postal_code", ""),
            website=draft.get("website", ""),
            primary_contact_person=draft.get("primary_contact_person", ""),
            contact_person_role=draft.get("contact_person_role", ""),
            contact_number=draft.get("contact_number", ""),
            email_address=draft.get("email_address", ""),
        )
        DB.session.add(application)
    else:
        application.company_name = draft["company_name"]
        application.contact_name = draft["contact_name"]
        application.phone = draft["phone"]
        application.supplier_category = draft["supplier_category"]
        application.supplier_category_detail = draft.get("supplier_category_detail", "")
        application.documents_status = draft.get("documents_status", "pending")
        application.status = "pending_review"
        application.registered_vendor_name = draft.get("registered_vendor_name", "")
        application.trading_name = draft.get("trading_name", "")
        application.business_registration_number = draft.get("business_registration_number", "")
        application.vat_number = draft.get("vat_number", "")
        application.tax_number = draft.get("tax_number", "")
        application.physical_address = draft.get("physical_address", "")
        application.city = draft.get("city", "")
        application.province = draft.get("province", "")
        application.postal_code = draft.get("postal_code", "")
        application.website = draft.get("website", "")
        application.primary_contact_person = draft.get("primary_contact_person", "")
        application.contact_person_role = draft.get("contact_person_role", "")
        application.contact_number = draft.get("contact_number", "")
        application.email_address = draft.get("email_address", "")

    DB.session.commit()

    SupplierApplicationCategory.query.filter_by(application_id=application.id).delete()
    DB.session.commit()

    for cat_value in draft.get("supplier_categories", []):
        detail = draft.get("supplier_category_details", {}).get(cat_value, "")
        cat_obj = SupplierApplicationCategory(
            application_id=application.id,
            category=cat_value,
            category_detail=detail,
        )
        DB.session.add(cat_obj)
    DB.session.commit()

    SupplierDirector.query.filter_by(application_id=application.id).delete()
    DB.session.commit()
    for director in draft.get("directors", []):
        director_obj = SupplierDirector(
            application_id=application.id,
            initials_surname=director.get("initials_surname", ""),
            id_number=director.get("id_number", ""),
            role=director.get("role", ""),
            nationality=director.get("nationality", ""),
        )
        DB.session.add(director_obj)
    DB.session.commit()

    user = get_current_user()
    if user:
        SupplierDocument.query.filter_by(user_id=user.id, application_id=None).update({"application_id": application.id})
        DB.session.commit()

    for admin in Admin.query.all():
        notification = Notification(
            admin_id=admin.id,
            title="New Supplier Application",
            message=f"New application submitted by {user.company_name if user else application.company_name} ({user.contact_name if user else application.contact_name}) for {application.supplier_category}.",
            related_application_id=application.id,
        )
        DB.session.add(notification)
    DB.session.commit()

    session.pop("supplier_onboarding", None)
    flash("Supplier details submitted successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/review/<int:application_id>", methods=["POST"])
def review_application(application_id):
    application = SupplierApplication.query.get_or_404(application_id)
    application.status = request.form.get("status", application.status)
    application.review_comments = request.form.get("review_comments", "")
    application.documents_status = request.form.get("documents_status", application.documents_status)
    DB.session.commit()
    return redirect(url_for("home"))


@app.route("/db-status")
def db_status():
    try:
        with app.app_context():
            DB.session.execute(text("SELECT 1"))
        return jsonify({"status": "connected", "database": MYSQL_DB})
    except Exception as exc:
        return jsonify({"status": "error", "message": str(exc)}), 500


@app.route("/health")
def health():
    return jsonify({"status": "ok"})

@app.route("/dashboard")
def dashboard():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    applications = SupplierApplication.query.filter_by(email=user.email).order_by(SupplierApplication.created_at.desc()).all()
    approved_count = sum(1 for app in applications if app.status == "approved")
    pending_count = sum(1 for app in applications if app.status == "pending_review")
    documents = SupplierDocument.query.filter_by(user_id=user.id).all()
    contact_phone = applications[0].phone if applications and applications[0].phone else user.phone
    unread_notifications = Notification.query.filter_by(user_id=user.id, is_read=False).count()

    return render_template(
        "dashboard.html",
        user=user,
        applications=applications,
        approved_count=approved_count,
        pending_count=pending_count,
        documents=documents,
        contact_phone=contact_phone,
        unread_notifications=unread_notifications,
    )


@app.route("/settings")
def supplier_settings():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    unread_notifications = Notification.query.filter_by(user_id=user.id, is_read=False).count()
    two_fa = User2FA.query.filter_by(user_id=user.id, confirmed=True).first() if user else None

    return render_template(
        "supplier_settings.html",
        user=user,
        unread_notifications=unread_notifications,
        two_fa=two_fa,
    )


@app.route("/dashboard/upload-document", methods=["POST"])
def upload_document():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    document_type = request.form.get("document_type", "").strip()
    file = request.files.get("document_file")

    if not document_type or not file or file.filename == "":
        flash("Please select a document type and file.", "error")
        return redirect(url_for("dashboard"))

    if not file.filename.lower().endswith(".pdf"):
        flash("Only PDF files are allowed.", "error")
        return redirect(url_for("dashboard"))

    upload_dir = os.path.join("static", "uploads", str(user.id))
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
    relative_path = os.path.join("uploads", str(user.id), filename)
    absolute_path = os.path.join("static", relative_path)
    file.save(absolute_path)

    application = (
        SupplierApplication.query.filter_by(email=user.email)
        .order_by(SupplierApplication.created_at.desc())
        .first()
    )

    document = SupplierDocument(
        user_id=user.id,
        application_id=application.id if application else None,
        document_type=document_type,
        file_path=relative_path,
        original_filename=file.filename,
    )
    DB.session.add(document)
    DB.session.commit()

    flash("Document uploaded successfully.", "success")
    return redirect(url_for("dashboard"))


@app.route("/applications/<int:application_id>/edit", methods=["GET", "POST"])
def edit_application(application_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    application = SupplierApplication.query.get_or_404(application_id)

    if application.email != user.email:
        flash("You are not authorized to edit this application.", "error")
        return redirect(url_for("dashboard"))

    documents = SupplierDocument.query.filter(
        DB.or_(
            SupplierDocument.application_id == application.id,
            DB.and_(
                SupplierDocument.user_id == user.id,
                SupplierDocument.application_id.is_(None),
            ),
        )
    ).all()
    doc_types = [
        "CIPC Company Registration Document",
        "Certified ID copies of all Directors",
        "SARS VAT Certificate",
        "Confirmation of Bank Account Letter (not older than 3 months)",
        "Valid B-BBEE certificate, letter from Accountant or Sworn Affidavit",
        "Proof of Company residential address (not older than 3 months)",
        "Declaration Form",
    ]

    if request.method == "POST":
        application.company_name = request.form.get("company_name", "").strip()
        application.contact_name = request.form.get("contact_name", "").strip()
        application.email = request.form.get("email", "").strip()
        application.phone = request.form.get("phone", "").strip()
        application.registered_vendor_name = request.form.get("registered_vendor_name", "").strip()
        application.trading_name = request.form.get("trading_name", "").strip()
        application.business_registration_number = request.form.get("business_registration_number", "").strip()
        application.vat_number = request.form.get("vat_number", "").strip()
        application.tax_number = request.form.get("tax_number", "").strip()
        application.physical_address = request.form.get("physical_address", "").strip()
        application.city = request.form.get("city", "").strip()
        application.province = request.form.get("province", "").strip()
        application.postal_code = request.form.get("postal_code", "").strip()
        application.website = request.form.get("website", "").strip()
        application.primary_contact_person = request.form.get("primary_contact_person", "").strip()
        application.contact_person_role = request.form.get("contact_person_role", "").strip()
        application.contact_number = request.form.get("contact_number", "").strip()
        application.email_address = request.form.get("email_address", "").strip()

        required = [
            application.company_name, application.contact_name, application.email,
            application.phone,
            application.registered_vendor_name,
            application.business_registration_number, application.tax_number,
            application.physical_address, application.city, application.province, application.postal_code,
            application.primary_contact_person, application.contact_person_role,
            application.contact_number, application.email_address,
        ]
        if not all(required):
            flash("Please complete all required fields.", "error")
            return redirect(url_for("edit_application", application_id=application.id))

        if not application.directors:
            flash("Please provide at least one director or shareholder.", "error")
            return redirect(url_for("edit_application", application_id=application.id))

        selected_categories = request.form.getlist("categories")
        if not selected_categories:
            flash("Please select at least one supplier category.", "error")
            return redirect(url_for("edit_application", application_id=application.id))

        application.supplier_category = selected_categories[0]

        for i, cat in enumerate(SUPPLIER_CATEGORIES):
            if cat.get("specify") and cat["value"] in selected_categories:
                detail = request.form.get(f"category_detail_{i}", "").strip()
                if not detail:
                    flash(f"Please provide details for '{cat['label']}'.", "error")
                    return redirect(url_for("edit_application", application_id=application.id))

        existing_cats = SupplierApplicationCategory.query.filter_by(application_id=application.id).all()
        for ec in existing_cats:
            DB.session.delete(ec)

        for cat_value in selected_categories:
            detail = ""
            for i, cat in enumerate(SUPPLIER_CATEGORIES):
                if cat.get("specify") and cat["value"] == cat_value:
                    detail = request.form.get(f"category_detail_{i}", "").strip()
                    break
            cat_obj = SupplierApplicationCategory(
                application_id=application.id,
                category=cat_value,
                category_detail=detail,
            )
            DB.session.add(cat_obj)

        application.documents_status = "complete"

        SupplierDirector.query.filter_by(application_id=application.id).delete()
        DB.session.commit()
        idx = 0
        while True:
            initials = request.form.get(f"director_initials_{idx}", "").strip()
            surname = request.form.get(f"director_surname_{idx}", "").strip()
            id_number = request.form.get(f"director_id_{idx}", "").strip()
            role = request.form.get(f"director_role_{idx}", "").strip()
            nationality = request.form.get(f"director_nationality_{idx}", "").strip()
            if not any([initials, surname, id_number, role, nationality]):
                break
            director_obj = SupplierDirector(
                application_id=application.id,
                initials_surname=f"{initials} {surname}".strip(),
                id_number=id_number,
                role=role,
                nationality=nationality,
            )
            DB.session.add(director_obj)
            idx += 1

        for index, doc_type in enumerate(doc_types, start=1):
            file = request.files.get(f"document_file_{index}")
            if file and file.filename:
                if not file.filename.lower().endswith(".pdf"):
                    flash("Only PDF files are allowed.", "error")
                    return redirect(url_for("edit_application", application_id=application.id))
                upload_dir = os.path.join("static", "uploads", str(user.id))
                os.makedirs(upload_dir, exist_ok=True)

                filename = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
                relative_path = f"uploads/{user.id}/{filename}"
                absolute_path = os.path.join("static", "uploads", str(user.id), filename)
                file.save(absolute_path)

                existing = SupplierDocument.query.filter_by(
                    user_id=user.id, application_id=application.id, document_type=doc_type
                ).first()
                if existing:
                    old_path = os.path.join("static", existing.file_path.replace("/", os.sep))
                    if os.path.exists(old_path):
                        os.remove(old_path)
                    existing.file_path = relative_path
                    existing.original_filename = file.filename
                else:
                    document = SupplierDocument(
                        user_id=user.id,
                        application_id=application.id,
                        document_type=doc_type,
                        file_path=relative_path,
                        original_filename=file.filename,
                    )
                    DB.session.add(document)

        other_file = request.files.get("document_file_other")
        if other_file and other_file.filename:
            if not other_file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are allowed.", "error")
                return redirect(url_for("edit_application", application_id=application.id))
            upload_dir = os.path.join("static", "uploads", str(user.id))
            os.makedirs(upload_dir, exist_ok=True)

            filename = f"{int(datetime.utcnow().timestamp())}_{other_file.filename}"
            relative_path = f"uploads/{user.id}/{filename}"
            absolute_path = os.path.join("static", "uploads", str(user.id), filename)
            other_file.save(absolute_path)

            existing = SupplierDocument.query.filter_by(
                user_id=user.id, application_id=application.id, document_type="Other Supporting Documents"
            ).first()
            if existing:
                old_path = os.path.join("static", existing.file_path.replace("/", os.sep))
                if os.path.exists(old_path):
                    os.remove(old_path)
                existing.file_path = relative_path
                existing.original_filename = other_file.filename
            else:
                document = SupplierDocument(
                    user_id=user.id,
                    application_id=application.id,
                    document_type="Other Supporting Documents",
                    file_path=relative_path,
                    original_filename=other_file.filename,
                )
                DB.session.add(document)

        company_profile_file = request.files.get("document_file_company_profile")
        existing_profile_doc = SupplierDocument.query.filter_by(
            user_id=user.id, application_id=application.id, document_type="Company Profile"
        ).first()
        has_company_profile = existing_profile_doc or (company_profile_file and company_profile_file.filename)
        has_website = (application.website or "").strip()
        if not has_company_profile and not has_website:
            flash("Please upload your Company Profile or provide a company website.", "error")
            return redirect(url_for("edit_application", application_id=application.id))

        if company_profile_file and company_profile_file.filename:
            if not company_profile_file.filename.lower().endswith(".pdf"):
                flash("Only PDF files are allowed.", "error")
                return redirect(url_for("edit_application", application_id=application.id))
            upload_dir = os.path.join("static", "uploads", str(user.id))
            os.makedirs(upload_dir, exist_ok=True)

            filename = f"{int(datetime.utcnow().timestamp())}_{company_profile_file.filename}"
            relative_path = f"uploads/{user.id}/{filename}"
            absolute_path = os.path.join("static", "uploads", str(user.id), filename)
            company_profile_file.save(absolute_path)

            if existing_profile_doc:
                old_path = os.path.join("static", existing_profile_doc.file_path.replace("/", os.sep))
                if os.path.exists(old_path):
                    os.remove(old_path)
                existing_profile_doc.file_path = relative_path
                existing_profile_doc.original_filename = company_profile_file.filename
            else:
                document = SupplierDocument(
                    user_id=user.id,
                    application_id=application.id,
                    document_type="Company Profile",
                    file_path=relative_path,
                    original_filename=company_profile_file.filename,
                )
                DB.session.add(document)

        application.documents_status = "complete"
        DB.session.commit()

        for admin in Admin.query.all():
            notification = Notification(
                admin_id=admin.id,
                title="Application Updated",
                message=f"Supplier {user.company_name} ({user.contact_name}) updated their application.",
                related_application_id=application.id,
            )
            DB.session.add(notification)
        DB.session.commit()

        flash("Application updated successfully.", "success")
        return redirect(url_for("dashboard"))

    selected_categories = [normalize_category_value(ac.category) for ac in application.application_categories]
    selected_details = {normalize_category_value(ac.category): ac.category_detail for ac in application.application_categories}
    directors = [
        {
            "initials_surname": d.initials_surname,
            "id_number": d.id_number,
            "role": d.role,
            "nationality": d.nationality,
        }
        for d in application.directors
    ]
    return render_template(
        "edit_application.html",
        user=user,
        application=application,
        documents=documents,
        doc_types=doc_types,
        selected_categories=selected_categories,
        selected_details=selected_details,
        directors=directors,
        unread_notifications=Notification.query.filter_by(user_id=user.id, is_read=False).count(),
    )


@app.route("/applications/<int:application_id>")
def application_detail(application_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    application = SupplierApplication.query.get_or_404(application_id)

    if application.email != user.email:
        flash("You are not authorized to view this application.", "error")
        return redirect(url_for("dashboard"))

    documents = SupplierDocument.query.filter(
        DB.or_(
            SupplierDocument.application_id == application.id,
            DB.and_(
                SupplierDocument.user_id == user.id,
                SupplierDocument.application_id.is_(None),
            ),
        )
    ).all() if user else SupplierDocument.query.filter_by(application_id=application.id).all()

    unread_notifications = Notification.query.filter_by(user_id=user.id, is_read=False).count()

    return render_template(
        "application_detail.html",
        user=user,
        application=application,
        documents=documents,
        unread_notifications=unread_notifications,
    )


@app.route("/apps")
def applications():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    search_query = request.args.get("q", "").strip()
    current_status = request.args.get("status", "all").strip()

    query = SupplierApplication.query.filter_by(email=user.email)

    if search_query:
        like_pattern = f"%{search_query}%"
        query = query.filter(
            DB.or_(
                SupplierApplication.company_name.ilike(like_pattern),
                SupplierApplication.contact_name.ilike(like_pattern),
            )
        )

    if current_status != "all":
        query = query.filter(SupplierApplication.status == current_status)

    applications = query.order_by(SupplierApplication.created_at.desc()).all()
    approved_count = sum(1 for app in applications if app.status == "approved")
    under_review_count = sum(1 for app in applications if app.status == "pending_review")
    declined_count = sum(1 for app in applications if app.status == "rejected")
    total_count = len(applications)

    if total_count > 0:
        approved_percent = round((approved_count / total_count) * 100)
        under_review_percent = round((under_review_count / total_count) * 100)
        declined_percent = round((declined_count / total_count) * 100)
    else:
        approved_percent = 0
        under_review_percent = 0
        declined_percent = 0

    unread_notifications = Notification.query.filter_by(user_id=user.id, is_read=False).count()

    return render_template(
        "application.html",
        user=user,
        applications=applications,
        approved_count=approved_count,
        under_review_count=under_review_count,
        declined_count=declined_count,
        total_count=total_count,
        approved_percent=approved_percent,
        under_review_percent=under_review_percent,
        declined_percent=declined_percent,
        current_status=current_status,
        search_query=search_query,
        unread_notifications=unread_notifications,
    )


def generate_supplier_id():
    import random
    for _ in range(20):
        number = random.randint(1000, 9999)
        supplier_id = f"IGSP{number}"
        if not User.query.filter_by(supplier_id=supplier_id).first():
            return supplier_id
    return f"IGSP{random.randint(1000, 9999)}"


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")
        company_name = request.form.get("company_name", "").strip()
        contact_name = request.form.get("contact_name", "").strip()
        phone = request.form.get("phone", "").strip()

        if not all([email, password, confirm_password, company_name, contact_name, phone]):
            flash("All fields are required.", "error")
            return redirect(url_for("signup"))

        if password != confirm_password:
            flash("Passwords do not match.", "error")
            return redirect(url_for("signup"))

        existing = User.query.filter_by(email=email).first()
        if existing:
            flash("An account with this email already exists.", "error")
            return redirect(url_for("signup"))

        user = User(
            supplier_id=generate_supplier_id(),
            email=email,
            company_name=company_name,
            contact_name=contact_name,
            phone=phone,
        )
        user.set_password(password)
        DB.session.add(user)
        DB.session.commit()
        session["user_id"] = user.id
        flash("Account created successfully.", "success")
        return redirect(url_for("home"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("login.html", login_error="Email and password are required.")

        user = User.query.filter_by(email=email).first()
        if not user or not user.check_password(password):
            return render_template("login.html", login_error="Invalid email or password.")

        two_fa = User2FA.query.filter_by(user_id=user.id).first()
        if not two_fa or not two_fa.confirmed:
            session["pending_user_id"] = user.id
            return redirect(url_for("supplier_setup_2fa"))

        session["pending_user_id"] = user.id
        return redirect(url_for("supplier_verify_2fa"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("user_id", None)
    session.pop("supplier_onboarding", None)
    session.pop("pending_user_id", None)
    session.pop("show_qr_for_new_device", None)
    return redirect(url_for("home"))


@app.route("/2fa/setup", methods=["GET", "POST"])
def supplier_setup_2fa():
    pending_user_id = session.get("pending_user_id")
    user = User.query.get(pending_user_id) if pending_user_id else get_current_user()
    if not user:
        return redirect(url_for("login"))

    two_fa = User2FA.query.filter_by(user_id=user.id).first()

    if request.method == "POST":
        if two_fa and two_fa.confirmed:
            action = request.form.get("action", "setup")

            if action == "add_device":
                password = request.form.get("password", "")
                if not user.check_password(password):
                    flash("Incorrect password. Cannot add device.", "error")
                    return redirect(url_for("supplier_setup_2fa"))

                session["show_qr_for_new_device"] = True
                flash("Scan the QR code with your new Microsoft Authenticator device.", "success")
                return redirect(url_for("supplier_setup_2fa"))

            if action == "disable":
                password = request.form.get("password", "")
                if not user.check_password(password):
                    flash("Incorrect password. 2FA was not disabled.", "error")
                    return redirect(url_for("supplier_setup_2fa"))

                if two_fa:
                    DB.session.delete(two_fa)
                    DB.session.commit()
                flash("Two-factor authentication has been disabled.", "success")
                return redirect(url_for("dashboard"))

            flash("Invalid action.", "error")
            return redirect(url_for("supplier_setup_2fa"))

        totp_code = request.form.get("totp_code", "").strip()
        if not totp_code:
            flash("Please enter the 6-digit code from your authenticator app.", "error")
            return redirect(url_for("supplier_setup_2fa"))

        temp_secret = session.pop("temp_2fa_secret", None)
        if not temp_secret:
            flash("Setup session expired. Please try again.", "error")
            return redirect(url_for("supplier_setup_2fa"))

        totp = pyotp.TOTP(temp_secret)
        if not totp.verify(totp_code):
            flash("Invalid code. Please try again.", "error")
            session["temp_2fa_secret"] = temp_secret
            return redirect(url_for("supplier_setup_2fa"))

        if not two_fa:
            two_fa = User2FA(user_id=user.id, totp_secret=temp_secret, confirmed=True)
            DB.session.add(two_fa)
        else:
            two_fa.totp_secret = temp_secret
            two_fa.confirmed = True
        DB.session.commit()

        session["user_id"] = user.id
        session.pop("pending_user_id", None)
        flash("Two-factor authentication enabled successfully.", "success")
        return redirect(url_for("dashboard"))

    if two_fa and two_fa.confirmed:
        show_qr = session.pop("show_qr_for_new_device", False)
        if show_qr:
            provisioning_uri = pyotp.TOTP(two_fa.totp_secret).provisioning_uri(name=user.email, issuer_name="IGSP")
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(provisioning_uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            buf.seek(0)
            qr_data = "data:image/png;base64," + base64.b64encode(buf.read()).decode()
            return render_template("supplier_2fa_setup.html", user=user, two_fa=two_fa, qr_data=qr_data, show_qr=True, secret=two_fa.totp_secret)
        return render_template("supplier_2fa_setup.html", user=user, two_fa=two_fa, qr_data=None, show_qr=False, secret=None)

    secret = pyotp.random_base32()
    session["temp_2fa_secret"] = secret
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="IGSP")

    qr = qrcode.QRCode(version=1, box_size=6, border=2)
    qr.add_data(provisioning_uri)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    qr_data = "data:image/png;base64," + base64.b64encode(buf.read()).decode()

    return render_template("supplier_2fa_setup.html", user=user, two_fa=None, qr_data=qr_data)


@app.route("/2fa/verify", methods=["GET", "POST"])
def supplier_verify_2fa():
    pending_user_id = session.get("pending_user_id")
    if not pending_user_id:
        return redirect(url_for("login"))

    user = User.query.get(pending_user_id)
    if not user:
        session.pop("pending_user_id", None)
        return redirect(url_for("login"))

    if request.method == "POST":
        totp_code = request.form.get("totp_code", "").strip()
        two_fa = User2FA.query.filter_by(user_id=user.id, confirmed=True).first()
        if not two_fa:
            session.pop("pending_user_id", None)
            return redirect(url_for("login"))

        totp = pyotp.TOTP(two_fa.totp_secret)
        if totp.verify(totp_code):
            session.pop("pending_user_id", None)
            session["user_id"] = user.id
            flash("Login successful.", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid verification code. Please try again.", "error")
        return redirect(url_for("supplier_verify_2fa"))

    return render_template("supplier_2fa_verify.html", user=user)


def get_current_user():
    if "user_id" in session:
        return User.query.get(session["user_id"])
    return None


@app.context_processor
def inject_microsoft_sso():
    return {
        "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
        "SUPPLIER_CATEGORIES": SUPPLIER_CATEGORIES,
        "CATEGORY_SECTIONS": get_category_sections(),
    }


def get_current_admin():
    if "admin_id" in session:
        return Admin.query.get(session["admin_id"])
    return None


def admin_required(view_func):
    def wrapper(*args, **kwargs):
        if not get_current_admin():
            return redirect(url_for("admin_login"))
        return view_func(*args, **kwargs)
    wrapper.__name__ = view_func.__name__
    return wrapper


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        password = request.form.get("password", "")

        if not email or not password:
            return render_template("admin_login.html", login_error="Email and password are required.")

        admin = Admin.query.filter_by(email=email).first()
        if not admin or not admin.check_password(password):
            return render_template("admin_login.html", login_error="Invalid email or password.")

        session["admin_id"] = admin.id
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html")


@app.route("/admin/login/microsoft")
def admin_login_microsoft():
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        flash("Microsoft SSO is not configured.", "error")
        return redirect(url_for("admin_login"))
    redirect_uri = url_for("admin_login_microsoft_callback", _external=True)
    return oauth.microsoft.authorize_redirect(redirect_uri)


@app.route("/admin/login/microsoft/callback")
def admin_login_microsoft_callback():
    try:
        token = oauth.microsoft.authorize_access_token()
        user_info = oauth.microsoft.userinfo()
    except Exception as exc:
        flash(f"Microsoft authentication failed: {exc}", "error")
        return redirect(url_for("admin_login"))

    email = user_info.get("email") or user_info.get("preferred_username")
    name = user_info.get("name", email)

    if not email:
        flash("Could not retrieve email from Microsoft account.", "error")
        return redirect(url_for("admin_login"))

    admin = Admin.query.filter_by(email=email).first()
    if not admin:
        flash("No admin account found for this Microsoft account.", "error")
        return redirect(url_for("admin_login"))

    session["admin_id"] = admin.id
    flash(f"Welcome, {admin.name}", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    return redirect(url_for("home"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    admin = get_current_admin()
    total_suppliers = User.query.count()
    total_applications = SupplierApplication.query.count()
    pending_count = SupplierApplication.query.filter_by(status="pending_review").count()
    approved_count = SupplierApplication.query.filter_by(status="approved").count()
    rejected_count = SupplierApplication.query.filter_by(status="rejected").count()
    recent_applications = SupplierApplication.query.order_by(SupplierApplication.created_at.desc()).limit(10).all()
    unread_notifications = Notification.query.filter_by(admin_id=admin.id, is_read=False).count()

    return render_template(
        "admin_dashboard.html",
        admin=admin,
        total_suppliers=total_suppliers,
        total_applications=total_applications,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
        recent_applications=recent_applications,
        unread_notifications=unread_notifications,
    )


@app.route("/admin/suppliers")
@admin_required
def admin_suppliers():
    admin = get_current_admin()
    search = request.args.get("search", "").strip()
    query = User.query

    if search:
        query = query.filter(
            DB.or_(
                User.company_name.ilike(f"%{search}%"),
                User.contact_name.ilike(f"%{search}%"),
                User.email.ilike(f"%{search}%"),
            )
        )

    suppliers = query.order_by(User.created_at.desc()).all()
    return render_template("admin_suppliers.html", admin=admin, suppliers=suppliers, search=search, unread_notifications=Notification.query.filter_by(admin_id=admin.id, is_read=False).count())


@app.route("/admin/applications")
@admin_required
def admin_applications():
    admin = get_current_admin()
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()

    query = SupplierApplication.query

    if search:
        query = query.filter(
            DB.or_(
                SupplierApplication.company_name.ilike(f"%{search}%"),
                SupplierApplication.contact_name.ilike(f"%{search}%"),
            )
        )

    if status_filter:
        query = query.filter(SupplierApplication.status == status_filter)

    applications = query.order_by(SupplierApplication.created_at.desc()).all()
    statuses = ["submitted", "pending_review", "approved", "rejected"]
    return render_template(
        "admin_applications.html",
        admin=admin,
        applications=applications,
        search=search,
        status_filter=status_filter,
        statuses=statuses,
        unread_notifications=Notification.query.filter_by(admin_id=admin.id, is_read=False).count(),
    )


@app.route("/notifications")
def notifications():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    notifications_list = Notification.query.filter_by(user_id=user.id).order_by(Notification.created_at.desc()).all()
    unread_count = Notification.query.filter_by(user_id=user.id, is_read=False).count()

    Notification.query.filter_by(user_id=user.id, is_read=False).update({"is_read": True})
    DB.session.commit()

    return render_template(
        "notifications.html",
        user=user,
        notifications=notifications_list,
        unread_notifications=unread_count,
    )


@app.route("/notifications/mark-read/<int:notification_id>", methods=["POST"])
def mark_notification_read(notification_id):
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    notification = Notification.query.filter_by(id=notification_id, user_id=user.id).first_or_404()
    notification.is_read = True
    DB.session.commit()
    return redirect(url_for("notifications"))


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    admin = get_current_admin()
    notifications_list = Notification.query.filter_by(admin_id=admin.id).order_by(Notification.created_at.desc()).all()
    unread_count = Notification.query.filter_by(admin_id=admin.id, is_read=False).count()

    Notification.query.filter_by(admin_id=admin.id, is_read=False).update({"is_read": True})
    DB.session.commit()

    return render_template(
        "admin_notifications.html",
        admin=admin,
        notifications=notifications_list,
        unread_notifications=unread_count,
    )


@app.route("/admin/notifications/mark-read/<int:notification_id>", methods=["POST"])
@admin_required
def admin_mark_notification_read(notification_id):
    admin = get_current_admin()
    notification = Notification.query.filter_by(id=notification_id, admin_id=admin.id).first_or_404()
    notification.is_read = True
    DB.session.commit()
    return redirect(url_for("admin_notifications"))


@app.route("/admin/suppliers/<int:supplier_id>/toggle", methods=["POST"])
@admin_required
def toggle_supplier(supplier_id):
    supplier = User.query.get_or_404(supplier_id)
    supplier.active = not supplier.active
    DB.session.commit()

    notification = Notification(
        user_id=supplier.id,
        title="Account Status Updated",
        message=f"Your account has been {'activated' if supplier.active else 'deactivated'} by the administrator.",
    )
    DB.session.add(notification)
    DB.session.commit()

    flash(f"Supplier {'activated' if supplier.active else 'deactivated'} successfully.", "success")
    return redirect(url_for("admin_suppliers"))


def ensure_notification_schema():
    with app.app_context():
        try:
            columns = DB.session.execute(text("SHOW COLUMNS FROM notifications")).fetchall()
            existing = {column[0].lower() for column in columns}

            if not columns:
                DB.session.execute(text("""
                    CREATE TABLE notifications (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NULL,
                        admin_id INT NULL,
                        title VARCHAR(150) NOT NULL,
                        message TEXT NOT NULL,
                        is_read BOOLEAN NOT NULL DEFAULT FALSE,
                        related_application_id INT NULL,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id),
                        FOREIGN KEY (admin_id) REFERENCES admins(id),
                        FOREIGN KEY (related_application_id) REFERENCES supplier_applications(id)
                    )
                """))
                DB.session.commit()
                app.logger.info("Notifications table created.")
            else:
                if "admin_id" not in existing:
                    DB.session.execute(text("ALTER TABLE notifications ADD COLUMN admin_id INT NULL"))
                    DB.session.execute(text("ALTER TABLE notifications ADD FOREIGN KEY (admin_id) REFERENCES admins(id)"))
                    DB.session.commit()
                    app.logger.info("Added admin_id to notifications table.")
                
                user_col = [c for c in columns if c[0].lower() == "user_id"]
                if user_col and user_col[0][2] == "NO":
                    DB.session.execute(text("ALTER TABLE notifications MODIFY COLUMN user_id INT NULL"))
                    DB.session.commit()
                    app.logger.info("Made user_id nullable in notifications table.")
        except Exception as exc:
            app.logger.warning("Notification schema check skipped: %s", exc)


@app.route("/admin/convert-documents")
@admin_required
def convert_existing_documents():
    documents = SupplierDocument.query.all()
    converted = 0
    failed = 0
    skipped = 0

    for doc in documents:
        if not doc.file_path:
            skipped += 1
            continue

        absolute_path = os.path.join("static", doc.file_path.replace("/", os.sep))
        if not os.path.exists(absolute_path):
            skipped += 1
            continue

        mime_type, _ = mimetypes.guess_type(absolute_path)
        if mime_type == "application/pdf":
            skipped += 1
            continue

        pdf_path = convert_to_pdf(absolute_path)
        if pdf_path and pdf_path != absolute_path:
            try:
                os.remove(absolute_path)
                doc.file_path = os.path.relpath(pdf_path, "static").replace(os.sep, "/")
                converted += 1
            except OSError:
                failed += 1
        else:
            failed += 1

    DB.session.commit()
    flash(f"Conversion complete: {converted} converted, {failed} failed, {skipped} skipped.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reports")
@admin_required
def admin_reports():
    admin = get_current_admin()
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    category_filter = request.args.get("category", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    base_query = SupplierApplication.query

    if search:
        like_pattern = f"%{search}%"
        base_query = base_query.filter(
            DB.or_(
                SupplierApplication.company_name.ilike(like_pattern),
                SupplierApplication.contact_name.ilike(like_pattern),
            )
        )

    if status_filter:
        base_query = base_query.filter(SupplierApplication.status == status_filter)

    if category_filter:
        base_query = base_query.filter(SupplierApplication.supplier_category == category_filter)

    if date_from:
        try:
            from_date = datetime.strptime(date_from, "%Y-%m-%d")
            base_query = base_query.filter(SupplierApplication.created_at >= from_date)
        except ValueError:
            pass

    if date_to:
        try:
            to_date = datetime.strptime(date_to, "%Y-%m-%d")
            base_query = base_query.filter(SupplierApplication.created_at <= to_date)
        except ValueError:
            pass

    filtered_applications = base_query.order_by(SupplierApplication.created_at.asc()).all()
    total_suppliers = User.query.count()
    active_suppliers = User.query.filter_by(active=True).count()
    inactive_suppliers = User.query.filter_by(active=False).count()
    total_applications = SupplierApplication.query.count()
    pending_count = SupplierApplication.query.filter_by(status="pending_review").count()
    approved_count = SupplierApplication.query.filter_by(status="approved").count()
    rejected_count = SupplierApplication.query.filter_by(status="rejected").count()
    submitted_count = SupplierApplication.query.filter_by(status="submitted").count()

    monthly_trends = {}
    category_breakdown = {}
    status_breakdown = {
        "submitted": 0,
        "pending_review": 0,
        "approved": 0,
        "rejected": 0,
    }
    doc_status_breakdown = {
        "complete": 0,
        "pending": 0,
    }
    monthly_suppliers = {}

    for app in filtered_applications:
        month_key = app.created_at.strftime("%Y-%m") if app.created_at else "Unknown"
        monthly_trends[month_key] = monthly_trends.get(month_key, 0) + 1
        cat = app.supplier_category or "Uncategorized"
        category_breakdown[cat] = category_breakdown.get(cat, 0) + 1
        for app_cat in app.application_categories:
            c = app_cat.category or "Uncategorized"
            category_breakdown[c] = category_breakdown.get(c, 0) + 1
        if app.status in status_breakdown:
            status_breakdown[app.status] += 1
        if app.documents_status == "complete":
            doc_status_breakdown["complete"] += 1
        else:
            doc_status_breakdown["pending"] += 1

    for user in User.query.order_by(User.created_at.asc()).all():
        if user.created_at:
            month_key = user.created_at.strftime("%Y-%m")
            monthly_suppliers[month_key] = monthly_suppliers.get(month_key, 0) + 1

    all_months = sorted(set(list(monthly_trends.keys()) + list(monthly_suppliers.keys())))
    if not all_months:
        all_months = [datetime.utcnow().strftime("%Y-%m")]

    chart_labels = all_months
    chart_app_trend = [monthly_trends.get(m, 0) for m in all_months]
    chart_supplier_trend = [monthly_suppliers.get(m, 0) for m in all_months]

    recent_applications = base_query.order_by(SupplierApplication.created_at.desc()).limit(20).all()
    unread_notifications = Notification.query.filter_by(admin_id=admin.id, is_read=False).count()

    all_statuses = ["submitted", "pending_review", "approved", "rejected"]
    all_categories = sorted({
        cat for app in SupplierApplication.query.all()
        for cat in (
            [app.supplier_category] + [ac.category for ac in app.application_categories]
        ) if cat
    })

    all_applications_payload = []
    for app in filtered_applications:
        all_applications_payload.append({
            "id": app.id,
            "company_name": app.company_name,
            "contact_name": app.contact_name,
            "categories": [ac.category for ac in app.application_categories],
            "category_details": {ac.category: ac.category_detail for ac in app.application_categories},
            "status": app.status,
            "documents_status": app.documents_status,
            "created_at": app.created_at.strftime("%Y-%m-%d") if app.created_at else "N/A",
        })

    return render_template(
        "admin_reports.html",
        admin=admin,
        total_suppliers=total_suppliers,
        active_suppliers=active_suppliers,
        inactive_suppliers=inactive_suppliers,
        total_applications=total_applications,
        pending_count=pending_count,
        approved_count=approved_count,
        rejected_count=rejected_count,
        submitted_count=submitted_count,
        status_breakdown=status_breakdown,
        category_breakdown=category_breakdown,
        doc_status_breakdown=doc_status_breakdown,
        chart_labels=chart_labels,
        chart_app_trend=chart_app_trend,
        chart_supplier_trend=chart_supplier_trend,
        recent_applications=recent_applications,
        unread_notifications=unread_notifications,
        all_applications_json=all_applications_payload,
        search=search,
        status_filter=status_filter,
        category_filter=category_filter,
        date_from=date_from,
        date_to=date_to,
        all_statuses=all_statuses,
        all_categories=all_categories,
    )


@app.route("/admin/applications/<int:application_id>/update", methods=["POST"])
@admin_required
def update_application(application_id):
    application = SupplierApplication.query.get_or_404(application_id)
    old_status = application.status
    application.status = request.form.get("status", application.status)
    application.review_comments = request.form.get("review_comments", "")
    application.documents_status = request.form.get("documents_status", application.documents_status)
    DB.session.commit()

    supplier = User.query.filter_by(email=application.email).first()
    if supplier:
        status_changed = old_status != application.status
        comments_changed = request.form.get("review_comments", "").strip()

        if status_changed:
            notification = Notification(
                user_id=supplier.id,
                title="Application Status Updated",
                message=f"Your application for {application.company_name} has been updated to: {application.status.replace('_', ' ').title()}.",
                related_application_id=application.id,
            )
            DB.session.add(notification)

            if application.status == "approved" and not supplier.active:
                supplier.active = True
                activation_notification = Notification(
                    user_id=supplier.id,
                    title="Account Activated",
                    message=f"Congratulations! Your supplier account has been activated following the approval of your application.",
                    related_application_id=application.id,
                )
                DB.session.add(activation_notification)

            if application.status == "rejected" and supplier.active:
                supplier.active = False
                deactivation_notification = Notification(
                    user_id=supplier.id,
                    title="Account Deactivated",
                    message=f"Your supplier account has been deactivated because your application was rejected.",
                    related_application_id=application.id,
                )
                DB.session.add(deactivation_notification)

        if comments_changed:
            notification = Notification(
                user_id=supplier.id,
                title="Review Comments Added",
                    message=f"Admin has added review comments to your application: {application.review_comments}",
                related_application_id=application.id,
            )
            DB.session.add(notification)

        DB.session.commit()

    flash("Application updated successfully.", "success")
    return redirect(url_for("admin_applications"))


@app.route("/admin/applications/<int:application_id>")
@admin_required
def admin_application_detail(application_id):
    admin = get_current_admin()
    application = SupplierApplication.query.get_or_404(application_id)
    supplier = User.query.filter_by(email=application.email).first()
    documents = SupplierDocument.query.filter(
        DB.or_(
            SupplierDocument.application_id == application.id,
            DB.and_(
                SupplierDocument.user_id == supplier.id if supplier else False,
                SupplierDocument.application_id.is_(None),
            ),
        )
    ).all() if supplier else SupplierDocument.query.filter_by(application_id=application.id).all()
    return render_template(
        "admin_application_detail.html",
        admin=admin,
        application=application,
        supplier=supplier,
        documents=documents,
        unread_notifications=Notification.query.filter_by(admin_id=admin.id, is_read=False).count(),
    )


@app.route("/admin/applications/<int:application_id>/documents/<int:document_id>/replace", methods=["POST"])
@admin_required
def replace_admin_document(application_id, document_id):
    application = SupplierApplication.query.get_or_404(application_id)
    document = SupplierDocument.query.get_or_404(document_id)

    if document.application_id != application.id:
        flash("Document does not belong to this application.", "error")
        return redirect(url_for("admin_application_detail", application_id=application_id))

    file = request.files.get("document_file")
    if not file or file.filename == "":
        flash("Please select a file to replace the document.", "error")
        return redirect(url_for("admin_application_detail", application_id=application_id))

    if not file.filename.lower().endswith(".pdf"):
        flash("Only PDF files are allowed.", "error")
        return redirect(url_for("admin_application_detail", application_id=application_id))

    upload_dir = os.path.join("static", "uploads", str(document.user_id))
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"{int(datetime.utcnow().timestamp())}_{file.filename}"
    relative_path = f"uploads/{document.user_id}/{filename}"
    absolute_path = os.path.join("static", "uploads", str(document.user_id), filename)
    file.save(absolute_path)

    old_path = os.path.join("static", document.file_path.replace("/", os.sep))
    if os.path.exists(old_path):
        os.remove(old_path)

    document.file_path = relative_path
    document.original_filename = file.filename
    DB.session.commit()

    flash("Document replaced successfully.", "success")
    return redirect(url_for("admin_application_detail", application_id=application_id))


@app.route("/admin/applications/<int:application_id>/download-all")
@admin_required
def download_all_documents(application_id):
    admin = get_current_admin()
    application = SupplierApplication.query.get_or_404(application_id)
    supplier = User.query.filter_by(email=application.email).first()
    documents = SupplierDocument.query.filter(
        DB.or_(
            SupplierDocument.application_id == application.id,
            DB.and_(
                SupplierDocument.user_id == supplier.id if supplier else False,
                SupplierDocument.application_id.is_(None),
            ),
        )
    ).all() if supplier else SupplierDocument.query.filter_by(application_id=application.id).all()

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for doc in documents:
            absolute_path = os.path.join("static", doc.file_path.replace("/", os.sep))
            if os.path.exists(absolute_path):
                arcname = doc.original_filename or os.path.basename(absolute_path)
                zipf.write(absolute_path, arcname)

    zip_buffer.seek(0)
    company_name = (supplier.company_name if supplier and supplier.company_name else application.company_name or "supplier").strip()
    safe_name = re.sub(r'[^A-Za-z0-9 _-]', '', company_name).strip().replace(' ', '_')
    filename = f"{safe_name}_combined documents.zip"

    return send_file(
        zip_buffer,
        mimetype="application/zip",
        as_attachment=True,
        download_name=filename,
    )


@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    absolute_path = os.path.join("static", filepath)
    if not os.path.exists(absolute_path):
        return "File not found", 404

    mime_type, _ = mimetypes.guess_type(absolute_path)
    if mime_type is None:
        mime_type = "application/octet-stream"

    response = send_file(
        absolute_path,
        mimetype=mime_type,
        as_attachment=False,
        download_name=os.path.basename(absolute_path),
    )
    response.headers["Content-Disposition"] = f"inline; filename={os.path.basename(absolute_path)}"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _libreoffice_paths():
    candidates = [
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        "/usr/bin/libreoffice",
        "/usr/bin/soffice",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    return "libreoffice"


def convert_to_pdf(absolute_path):
    if not os.path.exists(absolute_path):
        return None

    mime_type, _ = mimetypes.guess_type(absolute_path)
    if mime_type == "application/pdf":
        return absolute_path

    output_dir = os.path.dirname(absolute_path)
    libreoffice = _libreoffice_paths()
    try:
        subprocess.run(
            [
                libreoffice,
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                output_dir,
                absolute_path,
            ],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None

    base_name = os.path.splitext(os.path.basename(absolute_path))[0]
    pdf_path = os.path.join(output_dir, f"{base_name}.pdf")
    if os.path.exists(pdf_path):
        return pdf_path
    return None


def ensure_supplier_category_detail():
    with app.app_context():
        try:
            DB.create_all()
            columns = DB.session.execute(text("SHOW COLUMNS FROM supplier_applications")).fetchall()
            existing = {column[0].lower() for column in columns}

            if "supplier_category_detail" not in existing:
                DB.session.execute(text("ALTER TABLE supplier_applications ADD COLUMN supplier_category_detail VARCHAR(255) NOT NULL DEFAULT ''"))
                DB.session.commit()
                app.logger.info("Added supplier_category_detail column to supplier_applications table.")
        except Exception as exc:
            app.logger.warning("Supplier category detail schema check skipped: %s", exc)


def ensure_supplier_application_categories_table():
    with app.app_context():
        try:
            columns = DB.session.execute(text("SHOW COLUMNS FROM supplier_application_categories")).fetchall()
            if not columns:
                DB.session.execute(text("""
                    CREATE TABLE supplier_application_categories (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        application_id INT NOT NULL,
                        category VARCHAR(120) NOT NULL,
                        category_detail VARCHAR(255) NOT NULL DEFAULT '',
                        FOREIGN KEY (application_id) REFERENCES supplier_applications(id) ON DELETE CASCADE
                    )
                """))
                DB.session.commit()
                app.logger.info("Created supplier_application_categories table.")
        except Exception as exc:
            app.logger.warning("Supplier application categories table check skipped: %s", exc)


def ensure_user_2fa_table():
    with app.app_context():
        try:
            DB.create_all()
            columns = DB.session.execute(text("SHOW COLUMNS FROM user_2fa")).fetchall()
            if not columns:
                DB.session.execute(text("""
                    CREATE TABLE user_2fa (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        totp_secret VARCHAR(64) NOT NULL,
                        confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                        UNIQUE KEY uk_user_2fa_user (user_id)
                    )
                """))
                DB.session.commit()
                app.logger.info("Created user_2fa table.")
        except Exception as exc:
            app.logger.warning("User 2FA table check skipped: %s", exc)


def ensure_admin_schema():
    with app.app_context():
        try:
            columns = DB.session.execute(text("SHOW COLUMNS FROM admins")).fetchall()
            if not columns:
                DB.session.execute(text("""
                    CREATE TABLE admins (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        email VARCHAR(120) UNIQUE NOT NULL,
                        password_hash VARCHAR(256) NOT NULL,
                        name VARCHAR(100) NOT NULL DEFAULT '',
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                DB.session.commit()
                app.logger.info("Admins table created.")
        except Exception as exc:
            app.logger.warning("Admin schema check skipped: %s", exc)


def ensure_user_schema():
    with app.app_context():
        try:
            columns = DB.session.execute(text("SHOW COLUMNS FROM users")).fetchall()
            existing = {column[0].lower() for column in columns}

            if "name" in existing:
                DB.session.execute(text("ALTER TABLE users MODIFY COLUMN name VARCHAR(100) NOT NULL DEFAULT ''"))
            if "password_hash" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN password_hash VARCHAR(256) NOT NULL DEFAULT ''"))
            if "company_name" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN company_name VARCHAR(150) NOT NULL DEFAULT ''"))
            if "contact_name" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN contact_name VARCHAR(120) NOT NULL DEFAULT ''"))
            if "phone" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN phone VARCHAR(50) NOT NULL DEFAULT ''"))
            if "created_at" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN created_at DATETIME NULL"))
            if "active" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN active BOOLEAN NOT NULL DEFAULT FALSE"))
            if "address" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN address VARCHAR(255) NOT NULL DEFAULT ''"))
            if "supplier_id" not in existing:
                DB.session.execute(text("ALTER TABLE users ADD COLUMN supplier_id VARCHAR(20) NULL"))
                DB.session.execute(text("CREATE UNIQUE INDEX idx_users_supplier_id ON users(supplier_id)"))

            DB.session.commit()
            app.logger.info("User schema compatibility check completed.")
        except Exception as exc:
            app.logger.warning("User schema migration skipped: %s", exc)


def ensure_supplier_application_extra_fields():
    with app.app_context():
        try:
            DB.create_all()
            columns = DB.session.execute(text("SHOW COLUMNS FROM supplier_applications")).fetchall()
            existing = {column[0].lower() for column in columns}

            extras = {
                "registered_vendor_name": "VARCHAR(150) DEFAULT ''",
                "trading_name": "VARCHAR(150) DEFAULT ''",
                "business_registration_number": "VARCHAR(120) DEFAULT ''",
                "vat_number": "VARCHAR(50) DEFAULT ''",
                "tax_number": "VARCHAR(50) DEFAULT ''",
                "physical_address": "VARCHAR(255) DEFAULT ''",
                "city": "VARCHAR(100) DEFAULT ''",
                "province": "VARCHAR(100) DEFAULT ''",
                "postal_code": "VARCHAR(20) DEFAULT ''",
                "website": "VARCHAR(255) DEFAULT ''",
                "primary_contact_person": "VARCHAR(120) DEFAULT ''",
                "contact_person_role": "VARCHAR(120) DEFAULT ''",
                "contact_number": "VARCHAR(50) DEFAULT ''",
                "email_address": "VARCHAR(120) DEFAULT ''",
            }
            for col, col_type in extras.items():
                if col not in existing:
                    DB.session.execute(text(f"ALTER TABLE supplier_applications ADD COLUMN {col} {col_type}"))
            DB.session.commit()
            app.logger.info("Supplier application extra fields ensured.")
        except Exception as exc:
            app.logger.warning("Supplier application extra fields migration skipped: %s", exc)


def ensure_supplier_directors_table():
    with app.app_context():
        try:
            DB.create_all()
            columns = DB.session.execute(text("SHOW COLUMNS FROM supplier_directors")).fetchall()
            if not columns:
                DB.session.execute(text("""
                    CREATE TABLE supplier_directors (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        application_id INT NOT NULL,
                        initials_surname VARCHAR(120) NOT NULL,
                        id_number VARCHAR(50) NULL,
                        role VARCHAR(120) NOT NULL,
                        nationality VARCHAR(80) NOT NULL,
                        FOREIGN KEY (application_id) REFERENCES supplier_applications(id) ON DELETE CASCADE
                    )
                """))
                DB.session.commit()
                app.logger.info("Supplier directors table created.")
            id_number_col = [c for c in columns if c[0].lower() == "id_number"]
            if id_number_col and id_number_col[0][2] == "NO":
                DB.session.execute(text("ALTER TABLE supplier_directors MODIFY COLUMN id_number VARCHAR(50) NULL"))
                DB.session.commit()
                app.logger.info("Made id_number nullable in supplier_directors table.")
        except Exception as exc:
            app.logger.warning("Supplier directors table check skipped: %s", exc)


def init_database():
    with app.app_context():
        try:
            DB.create_all()
            ensure_user_schema()
            ensure_admin_schema()
            ensure_notification_schema()
            ensure_supplier_category_detail()
            ensure_supplier_application_categories_table()
            ensure_supplier_application_extra_fields()
            ensure_supplier_directors_table()
            ensure_user_2fa_table()
            app.logger.info("Database tables initialized successfully.")
        except Exception as exc:
            app.logger.warning("Database initialization skipped: %s", exc)
   


init_database()


if __name__ == "__main__":
    app.run(debug=True) 