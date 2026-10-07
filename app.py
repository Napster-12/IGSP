"""IGSP — Icebolethu Group Supplier Portal.

Configuration is read from environment variables (or a local ``.env`` file);
see ``.env.example`` for the full list.
"""
import csv
import io
import json
import os
import re
import secrets
import smtplib
import threading
import time
import zipfile
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from email.mime.text import MIMEText
from functools import wraps
from urllib.parse import quote_plus, urlparse

import boto3
import click
from authlib.integrations.flask_client import OAuth
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import case, func, text
from sqlalchemy.exc import OperationalError
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash, safe_join
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# Load .env from the project folder regardless of the directory the app is started from.
load_dotenv(os.path.join(BASE_DIR, ".env"))
STATIC_DIR = os.path.join(BASE_DIR, "static")
UPLOAD_ROOT = os.path.join(STATIC_DIR, "uploads")


def env_flag(name, default=False):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# ---------------------------------------------------------------------------
# App configuration
# ---------------------------------------------------------------------------

app = Flask(__name__)

_secret_key = os.getenv("SECRET_KEY")
if not _secret_key:
    _secret_key = secrets.token_hex(32)
    app.logger.warning("SECRET_KEY is not set; using a random key. Sessions will reset on restart.")

MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DB = os.getenv("MYSQL_DB", "igsp")

DATABASE_URL = os.getenv("DATABASE_URL") or (
    f"mysql+pymysql://{quote_plus(MYSQL_USER)}:{quote_plus(MYSQL_PASSWORD)}@"
    f"{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}?charset=utf8mb4"
)

app.config.update(
    SECRET_KEY=_secret_key,
    SQLALCHEMY_DATABASE_URI=DATABASE_URL,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    SQLALCHEMY_ENGINE_OPTIONS={"pool_pre_ping": True, "pool_recycle": 280},
    MAX_CONTENT_LENGTH=int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_flag("SESSION_COOKIE_SECURE"),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    CSRF_ENABLED=True,
)

DB = SQLAlchemy(app)

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
        server_metadata_url=f"{MICROSOFT_AUTHORITY}/.well-known/openid-configuration",
        client_kwargs={"scope": " ".join(MICROSOFT_SCOPE)},
    )

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)
# "ses" sends through the Amazon SES API with the server's AWS credentials (e.g. an EC2
# instance role) instead of SMTP. af-south-1 has no SES SMTP endpoint. SMTP_FROM is the sender.
MAIL_PROVIDER = os.getenv("MAIL_PROVIDER", "smtp").strip().lower()
SES_REGION = os.getenv("SES_REGION", "af-south-1")

# Shown on printed quotations. Optional; blank values are simply left off the document.
COMPANY_DETAILS = {
    "name": os.getenv("COMPANY_NAME", "Icebolethu Group"),
    "address": os.getenv("COMPANY_ADDRESS", ""),
    "registration_number": os.getenv("COMPANY_REG_NUMBER", ""),
    "vat_number": os.getenv("COMPANY_VAT_NUMBER", ""),
    "phone": os.getenv("COMPANY_PHONE", ""),
    "email": os.getenv("COMPANY_EMAIL", ""),
}
VAT_RATE = Decimal(os.getenv("VAT_RATE", "15"))


# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

RENTALS = "Burials related rentals and Purchases"
SUPPLIER_CATEGORIES = [
    {"value": "Catering", "label": "Catering", "specify": False, "group": "SERVICES"},
    {"value": "Body Storage, Cold-Room and other Burial Services", "label": "Body Storage, Cold-Room and other Burial Services", "specify": False, "group": "SERVICES"},
    {"value": "Consulting", "label": "Consulting", "specify": False, "group": "SERVICES"},
    {"value": "Other", "label": "Other, please specify:", "specify": True, "group": "SERVICES"},
    {"value": "Caskets and Tombstones", "label": "Caskets and Tombstones", "specify": False, "group": "GOODS"},
    {"value": "Livestock", "label": "Livestock", "specify": False, "group": "GOODS"},
    {"value": "Flowers, crosses and plaques", "label": "Flowers, crosses and plaques", "specify": False, "group": "GOODS"},
    {"value": "ICT Equipment, Media and Electronic Devices", "label": "ICT Equipment, Media and Electronic Devices, please specify:", "specify": True, "group": "GOODS"},
    {"value": "Tents, Draping and Décor", "label": "Tents, Draping and Décor", "specify": False, "group": RENTALS},
    {"value": "Funeral Vehicles (Hearse / Family Car)", "label": "Funeral Vehicles (Hearse / Family Car), please specify:", "specify": True, "group": RENTALS},
    {"value": "Lowering Device", "label": "Lowering Device", "specify": False, "group": RENTALS},
    {"value": "Mobile toilet", "label": "Mobile toilet", "specify": False, "group": RENTALS},
]
CATEGORY_VALUES = {cat["value"] for cat in SUPPLIER_CATEGORIES}
# Old category names, mapped to the merged category that replaced them.
LEGACY_CATEGORY_MAP = {
    "Catering services": "Catering",
    "Body Storage and other Burial Services": "Body Storage, Cold-Room and other Burial Services",
    "Cold-Room": "Body Storage, Cold-Room and other Burial Services",
    "ICT Equipment": "ICT Equipment, Media and Electronic Devices",
    "Media/Electronic Devices": "ICT Equipment, Media and Electronic Devices",
    "Tents": "Tents, Draping and Décor",
    "Draping and Décor": "Tents, Draping and Décor",
    "Hearse": "Funeral Vehicles (Hearse / Family Car)",
    "Family Car": "Funeral Vehicles (Hearse / Family Car)",
    "Caskets": "Caskets and Tombstones",
    "Tombstones": "Caskets and Tombstones",
}

# Required documents, in upload-slot order (form field document_file_1 .. _7).
REQUIRED_DOCUMENTS = [
    "CIPC Company Registration Document",
    "Certified ID copies of all Directors",
    "SARS VAT Certificate",
    "Confirmation of Bank Account Letter (not older than 3 months)",
    "Valid B-BBEE certificate, letter from Accountant or Sworn Affidavit",
    "Proof of Company residential address (not older than 3 months)",
    "Declaration Form",
]
COMPANY_PROFILE_DOC = "Company Profile"
OTHER_DOCS = "Other Supporting Documents"

UPLOAD_SLOTS = {f"document_file_{i}": doc for i, doc in enumerate(REQUIRED_DOCUMENTS, start=1)}
UPLOAD_SLOTS["document_file_other"] = OTHER_DOCS
UPLOAD_SLOTS["document_file_company_profile"] = COMPANY_PROFILE_DOC

STATUSES = ["draft", "submitted", "pending_review", "under_review", "approved", "declined", "returned_for_update"]
DOCUMENT_STATUSES = ["pending", "complete"]
EDITABLE_STATUSES = {"draft", "submitted", "pending_review", "under_review", "returned_for_update", "declined"}

PROVINCES = [
    "Eastern Cape", "Free State", "Gauteng", "KwaZulu-Natal", "Limpopo",
    "Mpumalanga", "Northern Cape", "North West", "Western Cape",
]

PROFILE_FIELD_LABELS = {
    "registered_vendor_name": "Registered Vendor Name",
    "trading_name": "Trading Name",
    "business_registration_number": "Business Registration Number",
    "vat_number": "VAT Number",
    "tax_number": "Tax Number",
    "physical_address": "Physical Address",
    "city": "City",
    "province": "Province",
    "postal_code": "Postal Code",
    "website": "Website",
    "primary_contact_person": "Primary Contact Person",
    "contact_person_role": "Contact Person's Role",
    "contact_number": "Contact Number",
    "email_address": "E-Mail Address",
}
PROFILE_FIELDS = list(PROFILE_FIELD_LABELS)
OPTIONAL_PROFILE_FIELDS = {"trading_name", "vat_number", "website"}
REQUIRED_PROFILE_FIELDS = [f for f in PROFILE_FIELDS if f not in OPTIONAL_PROFILE_FIELDS]

SOUTH_AFRICA = "South Africa"
# Director nationality choices; South Africa first, then alphabetical.
COUNTRIES = [SOUTH_AFRICA] + [
    'Afghanistan', 'Albania', 'Algeria', 'Andorra', 'Angola', 'Antigua and Barbuda', 'Argentina', 'Armenia',
    'Australia', 'Austria', 'Azerbaijan', 'Bahamas', 'Bahrain', 'Bangladesh', 'Barbados', 'Belarus',
    'Belgium', 'Belize', 'Benin', 'Bhutan', 'Bolivia', 'Bosnia and Herzegovina', 'Botswana', 'Brazil',
    'Brunei', 'Bulgaria', 'Burkina Faso', 'Burundi', 'Cabo Verde', 'Cambodia', 'Cameroon', 'Canada',
    'Central African Republic', 'Chad', 'Chile', 'China', 'Colombia', 'Comoros',
    'Congo (Democratic Republic)', 'Congo (Republic)', 'Costa Rica', "Côte d'Ivoire", 'Croatia', 'Cuba',
    'Cyprus', 'Czechia', 'Denmark', 'Djibouti', 'Dominica', 'Dominican Republic', 'Ecuador', 'Egypt',
    'El Salvador', 'Equatorial Guinea', 'Eritrea', 'Estonia', 'Eswatini', 'Ethiopia', 'Fiji', 'Finland',
    'France', 'Gabon', 'Gambia', 'Georgia', 'Germany', 'Ghana', 'Greece', 'Grenada', 'Guatemala', 'Guinea',
    'Guinea-Bissau', 'Guyana', 'Haiti', 'Honduras', 'Hungary', 'Iceland', 'India', 'Indonesia', 'Iran',
    'Iraq', 'Ireland', 'Israel', 'Italy', 'Jamaica', 'Japan', 'Jordan', 'Kazakhstan', 'Kenya', 'Kiribati',
    'Kuwait', 'Kyrgyzstan', 'Laos', 'Latvia', 'Lebanon', 'Lesotho', 'Liberia', 'Libya', 'Liechtenstein',
    'Lithuania', 'Luxembourg', 'Madagascar', 'Malawi', 'Malaysia', 'Maldives', 'Mali', 'Malta',
    'Marshall Islands', 'Mauritania', 'Mauritius', 'Mexico', 'Micronesia', 'Moldova', 'Monaco', 'Mongolia',
    'Montenegro', 'Morocco', 'Mozambique', 'Myanmar', 'Namibia', 'Nauru', 'Nepal', 'Netherlands',
    'New Zealand', 'Nicaragua', 'Niger', 'Nigeria', 'North Korea', 'North Macedonia', 'Norway', 'Oman',
    'Pakistan', 'Palau', 'Palestine', 'Panama', 'Papua New Guinea', 'Paraguay', 'Peru', 'Philippines',
    'Poland', 'Portugal', 'Qatar', 'Romania', 'Russia', 'Rwanda', 'Saint Kitts and Nevis', 'Saint Lucia',
    'Saint Vincent and the Grenadines', 'Samoa', 'San Marino', 'São Tomé and Príncipe', 'Saudi Arabia',
    'Senegal', 'Serbia', 'Seychelles', 'Sierra Leone', 'Singapore', 'Slovakia', 'Slovenia', 'Solomon Islands',
    'Somalia', 'South Korea', 'South Sudan', 'Spain', 'Sri Lanka', 'Sudan', 'Suriname', 'Sweden',
    'Switzerland', 'Syria', 'Taiwan', 'Tajikistan', 'Tanzania', 'Thailand', 'Timor-Leste', 'Togo', 'Tonga',
    'Trinidad and Tobago', 'Tunisia', 'Turkey', 'Turkmenistan', 'Tuvalu', 'Uganda', 'Ukraine',
    'United Arab Emirates', 'United Kingdom', 'United States', 'Uruguay', 'Uzbekistan', 'Vanuatu',
    'Vatican City', 'Venezuela', 'Vietnam', 'Yemen', 'Zambia', 'Zimbabwe',
]
COUNTRY_SET = set(COUNTRIES)
# Older records stored demonyms or free text.
NATIONALITY_ALIASES = {"south african": SOUTH_AFRICA, "rsa": SOUTH_AFRICA, "sa": SOUTH_AFRICA,
                       "zimbabwean": "Zimbabwe", "nigerian": "Nigeria", "motswana": "Botswana",
                       "mozambican": "Mozambique"}
PASSPORT_RE = re.compile(r"^[A-Z0-9]{6,20}$")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DIRECTOR_FIELD_RE = re.compile(r"^director_(initials|id|role|nationality)_(\d+)$")

VERIFY_CODE_MAX_ATTEMPTS = 5
RESEND_COOLDOWN_SECONDS = 30
PASSWORD_RESET_MAX_AGE = 3600


def utcnow():
    """Naive UTC timestamp, matching how MySQL DATETIME columns store values."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def normalize_email(value):
    return (value or "").strip().lower()


def normalize_category_value(value):
    return LEGACY_CATEGORY_MAP.get(value, value)


def get_specify_categories():
    return {cat["value"] for cat in SUPPLIER_CATEGORIES if cat.get("specify")}


def get_category_sections():
    """Group SUPPLIER_CATEGORIES into sections for rendering."""
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


def status_label(status):
    return (status or "").replace("_", " ").title()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

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
    created_at = DB.Column(DB.DateTime, default=utcnow)
    updated_at = DB.Column(DB.DateTime, default=utcnow, onupdate=utcnow)

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

    # The admin currently responsible for reviewing this application (see /assign).
    assigned_admin_id = DB.Column(DB.Integer, DB.ForeignKey("admins.id"), nullable=True, index=True)
    assigned_at = DB.Column(DB.DateTime, nullable=True)
    assigned_admin = DB.relationship("Admin", foreign_keys=[assigned_admin_id])

    application_categories = DB.relationship(
        "SupplierApplicationCategory", backref="application", cascade="all, delete-orphan", lazy=True,
    )
    directors = DB.relationship(
        "SupplierDirector", backref="application", cascade="all, delete-orphan", lazy=True,
    )

    @property
    def category_names(self):
        names = [ac.category for ac in self.application_categories if ac.category]
        return names or ([self.supplier_category] if self.supplier_category else [])


class SupplierDocument(DB.Model):
    __tablename__ = "supplier_documents"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=False)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=True)
    document_type = DB.Column(DB.String(120), nullable=False)
    file_path = DB.Column(DB.String(255), nullable=False)
    original_filename = DB.Column(DB.String(255), nullable=False)
    uploaded_at = DB.Column(DB.DateTime, default=utcnow)


class SupplierDirector(DB.Model):
    __tablename__ = "supplier_directors"

    id = DB.Column(DB.Integer, primary_key=True)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=False)
    initials_surname = DB.Column(DB.String(120), nullable=False)
    id_number = DB.Column(DB.String(50), nullable=False)
    role = DB.Column(DB.String(120), nullable=False)
    nationality = DB.Column(DB.String(80), nullable=False)


class ApplicationEvent(DB.Model):
    """Append-only audit trail for an application. Nothing in the app updates or deletes these rows."""
    __tablename__ = "application_events"

    id = DB.Column(DB.Integer, primary_key=True)
    application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=False, index=True)
    created_at = DB.Column(DB.DateTime, default=utcnow, nullable=False, index=True)
    actor_type = DB.Column(DB.String(20), nullable=False, default="system")  # supplier | admin | system
    actor_id = DB.Column(DB.Integer, nullable=True)
    actor_name = DB.Column(DB.String(150), nullable=False, default="")
    actor_email = DB.Column(DB.String(120), nullable=False, default="")
    action = DB.Column(DB.String(40), nullable=False)
    summary = DB.Column(DB.String(255), nullable=False, default="")
    from_status = DB.Column(DB.String(50), nullable=True)
    to_status = DB.Column(DB.String(50), nullable=True)
    changes_json = DB.Column(DB.Text, nullable=True)
    ip_address = DB.Column(DB.String(64), nullable=False, default="")

    @property
    def changes(self):
        try:
            return json.loads(self.changes_json) if self.changes_json else []
        except ValueError:
            return []


PRODUCT_UNITS = ["each", "per day", "per hour", "per event", "per kg", "per litre", "per box", "per set", "per service"]
REQUEST_STATUSES = ["requested", "accepted", "declined", "delivered", "completed", "cancelled"]
OPEN_REQUEST_STATUSES = {"requested", "accepted", "delivered"}


class Product(DB.Model):
    """An item or service an approved supplier offers to Icebolethu Group."""
    __tablename__ = "products"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=False, index=True)
    name = DB.Column(DB.String(150), nullable=False)
    category = DB.Column(DB.String(120), nullable=False, default="")
    description = DB.Column(DB.Text, nullable=False, default="")
    unit = DB.Column(DB.String(40), nullable=False, default="each")
    price = DB.Column(DB.Numeric(12, 2), nullable=False, default=0)
    quantity_available = DB.Column(DB.Integer, nullable=True)  # None = not tracked (e.g. services)
    lead_time_days = DB.Column(DB.Integer, nullable=True)
    is_listed = DB.Column(DB.Boolean, nullable=False, default=True)
    is_archived = DB.Column(DB.Boolean, nullable=False, default=False)
    created_at = DB.Column(DB.DateTime, default=utcnow)
    updated_at = DB.Column(DB.DateTime, default=utcnow, onupdate=utcnow)

    supplier = DB.relationship("User", backref=DB.backref("products", lazy=True))

    @property
    def in_stock(self):
        return self.quantity_available is None or self.quantity_available > 0


class ProductRequest(DB.Model):
    """An admin's request for a supplier's product. Name, unit and price are copied at request time."""
    __tablename__ = "product_requests"

    id = DB.Column(DB.Integer, primary_key=True)
    product_id = DB.Column(DB.Integer, DB.ForeignKey("products.id"), nullable=False, index=True)
    supplier_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=False, index=True)
    admin_id = DB.Column(DB.Integer, DB.ForeignKey("admins.id"), nullable=False, index=True)
    product_name = DB.Column(DB.String(150), nullable=False)
    unit = DB.Column(DB.String(40), nullable=False, default="each")
    unit_price = DB.Column(DB.Numeric(12, 2), nullable=False, default=0)
    quantity = DB.Column(DB.Integer, nullable=False, default=1)
    required_by = DB.Column(DB.Date, nullable=True)
    delivery_location = DB.Column(DB.String(255), nullable=False, default="")
    notes = DB.Column(DB.Text, nullable=False, default="")
    status = DB.Column(DB.String(20), nullable=False, default="requested", index=True)
    supplier_note = DB.Column(DB.Text, nullable=False, default="")
    created_at = DB.Column(DB.DateTime, default=utcnow, index=True)
    updated_at = DB.Column(DB.DateTime, default=utcnow, onupdate=utcnow)

    product = DB.relationship("Product")
    supplier = DB.relationship("User")
    admin = DB.relationship("Admin")
    events = DB.relationship("ProductRequestEvent", backref="request", lazy=True,
                             order_by="ProductRequestEvent.id.desc()")

    @property
    def reference(self):
        return f"PR-{self.id:05d}"

    @property
    def total(self):
        return (self.unit_price or Decimal("0")) * (self.quantity or 0)


class ProductRequestEvent(DB.Model):
    """Append-only history of a product request."""
    __tablename__ = "product_request_events"

    id = DB.Column(DB.Integer, primary_key=True)
    request_id = DB.Column(DB.Integer, DB.ForeignKey("product_requests.id"), nullable=False, index=True)
    created_at = DB.Column(DB.DateTime, default=utcnow, nullable=False)
    actor_type = DB.Column(DB.String(20), nullable=False, default="system")
    actor_name = DB.Column(DB.String(150), nullable=False, default="")
    action = DB.Column(DB.String(30), nullable=False)
    from_status = DB.Column(DB.String(20), nullable=True)
    to_status = DB.Column(DB.String(20), nullable=True)
    note = DB.Column(DB.Text, nullable=False, default="")


class Notification(DB.Model):
    __tablename__ = "notifications"

    id = DB.Column(DB.Integer, primary_key=True)
    user_id = DB.Column(DB.Integer, DB.ForeignKey("users.id"), nullable=True)
    admin_id = DB.Column(DB.Integer, DB.ForeignKey("admins.id"), nullable=True)
    title = DB.Column(DB.String(150), nullable=False)
    message = DB.Column(DB.Text, nullable=False)
    is_read = DB.Column(DB.Boolean, nullable=False, default=False)
    related_application_id = DB.Column(DB.Integer, DB.ForeignKey("supplier_applications.id"), nullable=True)
    created_at = DB.Column(DB.DateTime, default=utcnow)

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
    email_verified = DB.Column(DB.Boolean, nullable=False, default=False)
    active = DB.Column(DB.Boolean, nullable=False, default=False)
    verification_code_hash = DB.Column(DB.String(256), nullable=True)
    verification_code_expires = DB.Column(DB.DateTime, nullable=True)
    created_at = DB.Column(DB.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def generate_verification_code(self):
        code = f"{secrets.randbelow(900000) + 100000}"
        self.verification_code_hash = generate_password_hash(code)
        self.verification_code_expires = utcnow() + timedelta(minutes=10)
        return code

    def verify_code(self, code):
        if not code or not self.verification_code_hash or not self.verification_code_expires:
            return False
        expires = self.verification_code_expires
        if expires.tzinfo is not None:
            expires = expires.astimezone(timezone.utc).replace(tzinfo=None)
        if utcnow() > expires:
            return False
        return check_password_hash(self.verification_code_hash, code)

    def clear_verification_code(self):
        self.verification_code_hash = None
        self.verification_code_expires = None


class Admin(DB.Model):
    __tablename__ = "admins"

    id = DB.Column(DB.Integer, primary_key=True)
    email = DB.Column(DB.String(120), unique=True, nullable=False)
    password_hash = DB.Column(DB.String(256), nullable=False)
    name = DB.Column(DB.String(100), nullable=False, default="")
    created_at = DB.Column(DB.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


def find_user_by_email(email):
    return User.query.filter(func.lower(User.email) == normalize_email(email)).first()


def find_admin_by_email(email):
    return Admin.query.filter(func.lower(Admin.email) == normalize_email(email)).first()


def get_user_application(user):
    return (
        SupplierApplication.query.filter(func.lower(SupplierApplication.email) == normalize_email(user.email))
        .order_by(SupplierApplication.created_at.desc())
        .first()
    )


def get_application_supplier(application):
    return find_user_by_email(application.email)


def documents_for_application(application, supplier):
    """Documents attached to an application, plus the supplier's not-yet-attached uploads."""
    query = SupplierDocument.query
    if supplier:
        query = query.filter(
            DB.or_(
                SupplierDocument.application_id == application.id,
                DB.and_(SupplierDocument.user_id == supplier.id, SupplierDocument.application_id.is_(None)),
            )
        )
    else:
        query = query.filter(SupplierDocument.application_id == application.id)
    return query.order_by(SupplierDocument.id).all()


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def email_configured():
    if MAIL_PROVIDER == "ses":
        return bool(SMTP_FROM)
    return bool(SMTP_USER and SMTP_PASSWORD)


_ses_client = None


def _ses():
    global _ses_client
    if _ses_client is None:
        _ses_client = boto3.client("sesv2", region_name=SES_REGION)
    return _ses_client


def _deliver(to_email, subject, body, subtype):
    if MAIL_PROVIDER == "ses":
        _ses().send_email(
            FromEmailAddress=SMTP_FROM,
            Destination={"ToAddresses": [to_email]},
            Content={"Simple": {
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {"Html" if subtype == "html" else "Text": {"Data": body, "Charset": "UTF-8"}},
            }},
        )
        return
    msg = MIMEText(body, subtype, "utf-8")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to_email
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, [to_email], msg.as_string())


def _deliver_in_background(to_email, subject, body, subtype):
    def run():
        try:
            _deliver(to_email, subject, body, subtype)
        except Exception as exc:  # noqa: BLE001 — log and carry on; email is best-effort
            app.logger.error("Failed to send email to %s: %s", to_email, exc)

    threading.Thread(target=run, daemon=True).start()


def render_email(subject, message, recipient_name, application=None, action_url=None, action_label=None):
    return render_template(
        "email/notification.html",
        subject=subject,
        recipient_name=recipient_name,
        message=message,
        application=application,
        action_url=action_url,
        action_label=action_label,
        logo_url=url_for("static", filename="images/logo.png", _external=True),
    )


def send_verification_email(to_email, code):
    """Send a login code synchronously so the caller can report failures."""
    if not email_configured():
        app.logger.warning("Email not configured. Verification code for %s: %s", to_email, code)
        return True
    body = render_email(
        "Your IGSP Verification Code",
        f"Your verification code is {code}. It expires in 10 minutes. "
        "If you did not try to sign in, you can ignore this email.",
        "Supplier",
    )
    try:
        _deliver(to_email, "Your IGSP Verification Code", body, "html")
        return True
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Failed to send verification email to %s: %s", to_email, exc)
        return False


def send_notification_email(to_email, subject, body, recipient_name=None, application=None,
                            action_url=None, action_label=None):
    if not to_email:
        return False
    if not email_configured():
        app.logger.info("Email not configured. Email to %s: %s", to_email, subject)
        return False
    html = render_email(subject, body, recipient_name or "Supplier", application, action_url, action_label)
    _deliver_in_background(to_email, subject, html, "html")
    return True


def notify_supplier(supplier, title, message, application=None):
    DB.session.add(Notification(
        user_id=supplier.id,
        title=title,
        message=message,
        related_application_id=application.id if application else None,
    ))
    send_notification_email(
        supplier.email, title, message,
        recipient_name=supplier.contact_name or supplier.company_name,
        application=application,
        action_url=url_for("dashboard", _external=True),
        action_label="Open your dashboard",
    )


def notify_admins(title, message, application=None):
    action_url = (
        url_for("admin_application_detail", application_id=application.id, _external=True)
        if application else None
    )
    for admin in Admin.query.all():
        DB.session.add(Notification(
            admin_id=admin.id,
            title=title,
            message=message,
            related_application_id=application.id if application else None,
        ))
        send_notification_email(
            admin.email, title, message,
            recipient_name=admin.name or "Admin",
            application=application,
            action_url=action_url,
            action_label="Review application" if action_url else None,
        )


# ---------------------------------------------------------------------------
# Security helpers: auth, CSRF, rate limiting, headers
# ---------------------------------------------------------------------------

def get_current_user():
    if "user" not in g:
        user_id = session.get("user_id")
        g.user = DB.session.get(User, user_id) if user_id else None
    return g.user


def get_current_admin():
    if "admin" not in g:
        admin_id = session.get("admin_id")
        g.admin = DB.session.get(Admin, admin_id) if admin_id else None
    return g.admin


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not get_current_user():
            session.pop("user_id", None)
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapper


def admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not get_current_admin():
            session.pop("admin_id", None)
            return redirect(url_for("admin_login"))
        g.is_admin_view = True
        return view_func(*args, **kwargs)
    return wrapper


def safe_next_url(target, fallback):
    """Only follow redirects that stay on this site."""
    if target:
        parsed = urlparse(target)
        if not parsed.scheme and not parsed.netloc and target.startswith("/") and not target.startswith("//"):
            return target
    return fallback


def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def protect_requests():
    # Supplier uploads live under static/ for historical reasons; never serve them
    # through the public static route — /uploads/ enforces access control instead.
    if request.path.startswith("/static/uploads/"):
        abort(404)

    if request.method in {"POST", "PUT", "PATCH", "DELETE"} and app.config.get("CSRF_ENABLED", True):
        sent = request.form.get("csrf_token") or request.headers.get("X-CSRFToken", "")
        expected = session.get("_csrf_token", "")
        if not expected or not secrets.compare_digest(sent, expected):
            flash("Your session expired. Please try again.", "error")
            referrer = request.referrer or ""
            ref = urlparse(referrer)
            if ref.netloc == request.host:
                return redirect(ref.path + (f"?{ref.query}" if ref.query else ""))
            return redirect(url_for("home"))


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if g.get("user") or g.get("admin"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


class RateLimiter:
    """Small in-process sliding-window limiter. Good enough for a single app server."""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self.hits = defaultdict(list)
        self.lock = threading.Lock()

    def _prune(self, key, now):
        self.hits[key] = [t for t in self.hits[key] if now - t < self.window]

    def is_blocked(self, key):
        with self.lock:
            now = time.monotonic()
            self._prune(key, now)
            return len(self.hits[key]) >= self.limit

    def hit(self, key):
        with self.lock:
            now = time.monotonic()
            self._prune(key, now)
            self.hits[key].append(now)

    def reset(self, key):
        with self.lock:
            self.hits.pop(key, None)


login_limiter = RateLimiter(limit=10, window_seconds=15 * 60)
reset_limiter = RateLimiter(limit=5, window_seconds=15 * 60)


def client_ip():
    return request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()


EVENT_LABELS = {
    "submitted": "Application submitted",
    "updated": "Application updated by supplier",
    "resubmitted": "Application resubmitted",
    "status_changed": "Status changed",
    "review_updated": "Review details updated",
    "document_replaced": "Document replaced by admin",
    "documents_downloaded": "All documents downloaded",
    "account_activated": "Supplier account activated",
    "account_deactivated": "Supplier account deactivated",
    "assigned": "Assigned to admin",
    "unassigned": "Unassigned",
    "document_viewed": "Document viewed",
}

AUDIT_FIELD_LABELS = {
    "company_name": "Company name", "contact_name": "Key contact", "phone": "Phone",
    **PROFILE_FIELD_LABELS,
}


def log_event(application, action, summary="", from_status=None, to_status=None, changes=None, actor=None):
    """Record one audit-trail entry. `actor` defaults to whoever is signed in for this request."""
    if actor is None:
        if g.get("is_admin_view") and get_current_admin():
            actor = get_current_admin()
        else:
            actor = get_current_user()
    if isinstance(actor, Admin):
        actor_type, name = "admin", actor.name or actor.email
    elif isinstance(actor, User):
        actor_type, name = "supplier", actor.contact_name or actor.company_name
    else:
        actor_type, name = "system", "System"
    DB.session.add(ApplicationEvent(
        application_id=application.id,
        actor_type=actor_type,
        actor_id=getattr(actor, "id", None),
        actor_name=(name or "")[:150],
        actor_email=(getattr(actor, "email", "") or "")[:120],
        action=action,
        summary=(summary or EVENT_LABELS.get(action, action))[:255],
        from_status=from_status,
        to_status=to_status,
        changes_json=json.dumps(changes) if changes else None,
        ip_address=client_ip()[:64] if request else "",
    ))


def application_snapshot(application):
    """Plain-text view of everything a supplier can edit, for before/after comparison."""
    snapshot = {AUDIT_FIELD_LABELS[f]: (getattr(application, f) or "") for f in AUDIT_FIELD_LABELS}
    snapshot["Category"] = "; ".join(
        f"{c.category} ({c.category_detail})" if c.category_detail else c.category
        for c in application.application_categories
    )
    snapshot["Directors & shareholders"] = "; ".join(
        f"{d.initials_surname}, {d.role}, {d.nationality}, {d.id_number}" for d in application.directors
    )
    return snapshot


def diff_snapshots(before, after):
    return [{"field": k, "old": before.get(k, ""), "new": after.get(k, "")}
            for k in after if (before.get(k) or "") != (after.get(k) or "")]


def validate_new_password(password, confirm):
    if password != confirm:
        return "Passwords do not match."
    if len(password) < 8:
        return "Password must be at least 8 characters long."
    if not re.search(r"[0-9]", password):
        return "Password must contain at least one number."
    if not re.search(r"[^a-zA-Z0-9]", password):
        return "Password must contain at least one special character."
    return None


def password_reset_serializer():
    return URLSafeTimedSerializer(app.config["SECRET_KEY"], salt="igsp-password-reset")


def make_password_reset_token(user):
    # Embedding part of the hash makes the token single-use: it dies once the password changes.
    return password_reset_serializer().dumps({"uid": user.id, "ph": user.password_hash[-16:]})


def load_password_reset_token(token):
    try:
        data = password_reset_serializer().loads(token, max_age=PASSWORD_RESET_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    user = DB.session.get(User, data.get("uid"))
    if not user or user.password_hash[-16:] != data.get("ph"):
        return None
    return user


@app.template_filter("zar")
def format_zar(value):
    """R 1 234.50 (South African formatting)."""
    try:
        amount = Decimal(value or 0)
    except (InvalidOperation, TypeError):
        return value
    return "R " + f"{amount:,.2f}".replace(",", " ")


def open_request_count():
    """Open product requests for the nav badge: the supplier's own, or all (for admins)."""
    try:
        if g.get("is_admin_view") and g.get("admin"):
            return ProductRequest.query.filter(ProductRequest.status.in_(OPEN_REQUEST_STATUSES)).count()
        if g.get("user") and g.user.active:
            return ProductRequest.query.filter(ProductRequest.supplier_id == g.user.id,
                                               ProductRequest.status == "requested").count()
    except Exception:  # noqa: BLE001 — never break page rendering over a badge
        DB.session.rollback()
    return 0


@app.context_processor
def inject_globals():
    cats = []
    for i, cat in enumerate(SUPPLIER_CATEGORIES):
        item = dict(cat)
        item["index"] = i
        cats.append(item)

    unread = 0
    supplier_application = None
    if g.get("is_admin_view") and g.get("admin"):
        unread = Notification.query.filter_by(admin_id=g.admin.id, is_read=False).count()
    elif g.get("user"):
        unread = Notification.query.filter_by(user_id=g.user.id, is_read=False).count()
        supplier_application = get_user_application(g.user)

    return {
        "MICROSOFT_CLIENT_ID": MICROSOFT_CLIENT_ID,
        "SUPPLIER_CATEGORIES": cats,
        "CATEGORY_SECTIONS": get_category_sections(),
        "PROVINCES": PROVINCES,
        "COUNTRIES": COUNTRIES,
        "normalize_nationality": normalize_nationality,
        "csrf_token": csrf_token,
        "status_label": status_label,
        "unread_notifications": unread,
        "supplier_application": supplier_application,
        "REQUEST_STATUSES": REQUEST_STATUSES,
        "PRODUCT_UNITS": PRODUCT_UNITS,
        "open_request_count": open_request_count(),
        "EDITABLE_STATUSES": EDITABLE_STATUSES,
    }


# ---------------------------------------------------------------------------
# Uploads
# ---------------------------------------------------------------------------

class UploadError(ValueError):
    pass


def validate_pdf(file_storage):
    """Raise UploadError unless the upload is a non-empty PDF (by extension and content)."""
    if not file_storage.filename.lower().endswith(".pdf"):
        raise UploadError(f"'{file_storage.filename}' is not a PDF. Only PDF files are allowed.")
    head = file_storage.stream.read(5)
    file_storage.stream.seek(0)
    if head != b"%PDF-":
        raise UploadError(f"'{file_storage.filename}' does not appear to be a valid PDF file.")


def collect_uploads(files, slots=None):
    """Return [(document_type, FileStorage)] for every filled upload slot, validating each file."""
    uploads = []
    for field, doc_type in (slots or UPLOAD_SLOTS).items():
        file_storage = files.get(field)
        if file_storage and file_storage.filename:
            validate_pdf(file_storage)
            uploads.append((doc_type, file_storage))
    return uploads


def absolute_upload_path(relative_path):
    """Map a stored file_path (``uploads/<user>/<file>``) to disk, refusing anything outside UPLOAD_ROOT."""
    if not relative_path:
        return None
    path = safe_join(STATIC_DIR, relative_path.replace("\\", "/"))
    if not path:
        return None
    path = os.path.realpath(path)
    if not path.startswith(os.path.realpath(UPLOAD_ROOT) + os.sep):
        return None
    return path


def save_upload(file_storage, user_id):
    upload_dir = os.path.join(UPLOAD_ROOT, str(user_id))
    os.makedirs(upload_dir, exist_ok=True)
    safe_name = secure_filename(file_storage.filename) or "document.pdf"
    filename = f"{int(time.time())}_{secrets.token_hex(4)}_{safe_name}"
    file_storage.save(os.path.join(upload_dir, filename))
    return f"uploads/{user_id}/{filename}"


def remove_upload(relative_path):
    path = absolute_upload_path(relative_path)
    if path and os.path.isfile(path):
        try:
            os.remove(path)
        except OSError as exc:
            app.logger.warning("Could not delete %s: %s", path, exc)


def store_document(user_id, application_id, document_type, file_storage):
    """Save an upload, replacing any existing document of the same type for that application."""
    query = SupplierDocument.query.filter_by(user_id=user_id, document_type=document_type)
    if application_id is None:
        query = query.filter(SupplierDocument.application_id.is_(None))
    else:
        query = query.filter(SupplierDocument.application_id == application_id)
    existing = query.first()

    relative_path = save_upload(file_storage, user_id)
    if existing:
        remove_upload(existing.file_path)
        existing.file_path = relative_path
        existing.original_filename = file_storage.filename
        existing.uploaded_at = utcnow()
        return existing

    document = SupplierDocument(
        user_id=user_id,
        application_id=application_id,
        document_type=document_type,
        file_path=relative_path,
        original_filename=file_storage.filename,
    )
    DB.session.add(document)
    return document


def missing_required_documents(documents):
    uploaded = {doc.document_type for doc in documents}
    return [doc for doc in REQUIRED_DOCUMENTS if doc not in uploaded]


# ---------------------------------------------------------------------------
# Form parsing
# ---------------------------------------------------------------------------

def read_fields(form, fields):
    return {field: form.get(field, "").strip() for field in fields}


def missing_field_labels(values, required, labels=None):
    labels = labels or PROFILE_FIELD_LABELS
    return [labels.get(f, f.replace("_", " ").title()) for f in required if not values.get(f)]


def validate_profile_values(values):
    missing = missing_field_labels(values, REQUIRED_PROFILE_FIELDS)
    if missing:
        return "Please complete the required fields: " + ", ".join(missing) + "."
    if values.get("email_address") and not EMAIL_RE.match(values["email_address"]):
        return "Please enter a valid e-mail address."
    if values.get("province") and values["province"] not in PROVINCES:
        return "Please select a valid province."
    return None


def normalize_nationality(value):
    value = (value or "").strip()
    if value in COUNTRY_SET:
        return value
    return NATIONALITY_ALIASES.get(value.lower(), "")


def luhn_valid(digits):
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def sa_id_number_error(id_number):
    """Validate a South African ID: YYMMDD SSSS C A Z (13 digits, Luhn checksum)."""
    if not re.fullmatch(r"\d{13}", id_number):
        return "must be exactly 13 digits"
    yy, mm, dd = int(id_number[0:2]), int(id_number[2:4]), int(id_number[4:6])
    valid_date = False
    for century in (1900, 2000):
        try:
            datetime(century + yy, mm, dd)
            valid_date = True
        except ValueError:
            pass
    if not valid_date:
        return "does not start with a valid date of birth (YYMMDD)"
    if id_number[10] not in "012":
        return "has an invalid citizenship digit"
    if not luhn_valid(id_number):
        return "is not a valid SA ID number (checksum failed)"
    return None


def director_id_error(id_number, nationality):
    if nationality == SOUTH_AFRICA:
        error = sa_id_number_error(id_number)
        return f"ID number {error}" if error else None
    if not PASSPORT_RE.match(id_number):
        return "passport number must be 6–20 letters or digits"
    return None


def parse_directors(form):
    """Parse director rows (director_<field>_<n>), tolerating gaps left by removed rows."""
    rows = defaultdict(dict)
    for key, value in form.items():
        match = DIRECTOR_FIELD_RE.match(key)
        if match:
            rows[int(match.group(2))][match.group(1)] = value.strip()

    directors = []
    seen_ids = set()
    for index in sorted(rows):
        row = rows[index]
        values = [row.get("initials", ""), row.get("id", ""), row.get("role", ""), row.get("nationality", "")]
        if not any(values):
            continue
        if not all(values):
            return None, ("All director/shareholder fields (Initials & Surname, ID Number, Role, "
                          "Nationality) are required.")
        name = values[0]
        nationality = normalize_nationality(values[3])
        if not nationality:
            return None, f"Please select a nationality from the list for {name}."
        id_number = re.sub(r"[\s-]", "", values[1]).upper()
        error = director_id_error(id_number, nationality)
        if error:
            return None, f"{name}: {error}."
        if id_number in seen_ids:
            return None, f"{name}: the same ID/passport number is entered for more than one director."
        seen_ids.add(id_number)
        directors.append({
            "initials_surname": name[:120],
            "id_number": id_number[:50],
            "role": values[2][:120],
            "nationality": nationality,
        })
    if not directors:
        return None, "Please provide at least one director or shareholder."
    return directors, None


def parse_categories(form):
    """Return (categories, details, error). The UI offers a single choice; the model allows many."""
    selected = [normalize_category_value(c) for c in form.getlist("categories") if c]
    selected = [c for c in dict.fromkeys(selected) if c in CATEGORY_VALUES]
    if not selected:
        return None, None, "Please select a supplier category."

    details = {}
    for i, cat in enumerate(SUPPLIER_CATEGORIES):
        if cat["value"] in selected and cat.get("specify"):
            detail = form.get(f"category_detail_{i}", "").strip()
            if not detail:
                return None, None, f"Please provide details for '{cat['label'].rstrip(':')}'."
            details[cat["value"]] = detail[:255]
    return selected, details, None


def replace_categories(application, categories, details):
    application.application_categories.clear()
    for value in categories:
        application.application_categories.append(
            SupplierApplicationCategory(category=value, category_detail=details.get(value, ""))
        )
    application.supplier_category = categories[0]
    application.supplier_category_detail = details.get(categories[0], "")


def replace_directors(application, directors):
    application.directors.clear()
    for director in directors:
        application.directors.append(SupplierDirector(**director))


# ---------------------------------------------------------------------------
# Public pages and authentication
# ---------------------------------------------------------------------------

@app.route("/")
def home():
    return render_template("index.html", user=get_current_user())


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/db-status")
@admin_required
def db_status():
    try:
        DB.session.execute(text("SELECT 1"))
        return jsonify({"status": "connected", "database": DB.engine.url.database})
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Database check failed: %s", exc)
        return jsonify({"status": "error"}), 500


def generate_supplier_id():
    for digits in (4, 4, 4, 4, 4, 6, 6, 6, 6, 6):
        candidate = f"IGSP{secrets.randbelow(9 * 10 ** (digits - 1)) + 10 ** (digits - 1)}"
        if not User.query.filter_by(supplier_id=candidate).first():
            return candidate
    return f"IGSP{secrets.token_hex(6).upper()}"


def start_email_verification(user):
    """Issue a code, email it, and park the user in the pending-verification state."""
    code = user.generate_verification_code()
    DB.session.commit()
    session["pending_user_id"] = user.id
    session["verify_attempts"] = 0
    session["code_sent_at"] = time.time()
    return send_verification_email(user.email, code)


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        form = read_fields(request.form, ["email", "company_name", "contact_name", "phone"])
        email = normalize_email(form["email"])
        password = request.form.get("password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not all([email, password, confirm_password, form["company_name"], form["contact_name"], form["phone"]]):
            flash("All fields are required.", "error")
            return redirect(url_for("signup"))
        if not EMAIL_RE.match(email):
            flash("Please enter a valid email address.", "error")
            return redirect(url_for("signup"))
        error = validate_new_password(password, confirm_password)
        if error:
            flash(error, "error")
            return redirect(url_for("signup"))
        if find_user_by_email(email):
            flash("An account with this email already exists. Please log in instead.", "error")
            return redirect(url_for("signup"))

        user = User(
            supplier_id=generate_supplier_id(),
            name=form["contact_name"][:100],
            email=email,
            company_name=form["company_name"],
            contact_name=form["contact_name"],
            phone=form["phone"],
            active=False,
        )
        user.set_password(password)
        DB.session.add(user)
        DB.session.commit()

        if start_email_verification(user):
            flash("Account created. Enter the verification code we sent to your email.", "success")
        else:
            flash("Account created, but we couldn't send your verification code. Use 'Resend code' to try again.", "error")
        return redirect(url_for("verify_email"))

    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user():
        return redirect(url_for("dashboard"))

    if request.method == "GET" and request.args.get("next"):
        session["login_next"] = safe_next_url(request.args.get("next"), "")

    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password", "")
        limiter_key = ("login", client_ip(), email)

        if not email or not password:
            return render_template("login.html", login_error="Email and password are required.")
        if login_limiter.is_blocked(limiter_key):
            return render_template("login.html", login_error="Too many login attempts. Please wait 15 minutes and try again.")

        user = find_user_by_email(email)
        if not user or not user.check_password(password):
            login_limiter.hit(limiter_key)
            return render_template("login.html", login_error="Invalid email or password.")

        login_limiter.reset(limiter_key)
        if not start_email_verification(user):
            session.pop("pending_user_id", None)
            return render_template("login.html", login_error="We couldn't send your verification code. Please try again shortly.")
        return redirect(url_for("verify_email"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    for key in ("user_id", "supplier_onboarding", "pending_user_id", "verify_attempts", "code_sent_at", "login_next"):
        session.pop(key, None)
    return redirect(url_for("home"))


def get_pending_user():
    pending_user_id = session.get("pending_user_id")
    user = DB.session.get(User, pending_user_id) if pending_user_id else None
    if not user:
        session.pop("pending_user_id", None)
    return user


@app.route("/verify-email", methods=["GET", "POST"])
def verify_email():
    user = get_pending_user()
    if not user:
        return redirect(url_for("login"))

    if request.method == "POST":
        code = re.sub(r"\D", "", request.form.get("code", ""))

        if user.verify_code(code):
            next_url = session.pop("login_next", "") or url_for("dashboard")
            for key in ("pending_user_id", "verify_attempts", "code_sent_at"):
                session.pop(key, None)
            session["user_id"] = user.id
            session.permanent = True
            user.email_verified = True
            user.clear_verification_code()
            DB.session.commit()
            flash("Login successful.", "success")
            return redirect(next_url)

        attempts = session.get("verify_attempts", 0) + 1
        session["verify_attempts"] = attempts
        if attempts >= VERIFY_CODE_MAX_ATTEMPTS:
            user.clear_verification_code()
            DB.session.commit()
            session.pop("pending_user_id", None)
            flash("Too many incorrect codes. Please log in again to get a new code.", "error")
            return redirect(url_for("login"))

        remaining = VERIFY_CODE_MAX_ATTEMPTS - attempts
        flash(f"Invalid or expired verification code. {remaining} attempt(s) left.", "error")
        return redirect(url_for("verify_email"))

    return render_template("email_verify.html", user=user)


@app.route("/resend-code", methods=["POST"])
def resend_code():
    user = get_pending_user()
    if not user:
        return redirect(url_for("login"))

    elapsed = time.time() - session.get("code_sent_at", 0)
    if elapsed < RESEND_COOLDOWN_SECONDS:
        flash(f"Please wait {int(RESEND_COOLDOWN_SECONDS - elapsed) + 1} seconds before requesting another code.", "error")
        return redirect(url_for("verify_email"))

    if start_email_verification(user):
        flash("A new verification code has been sent to your email.", "success")
    else:
        flash("We couldn't send a new code right now. Please try again shortly.", "error")
    return redirect(url_for("verify_email"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        limiter_key = ("reset", client_ip())
        if reset_limiter.is_blocked(limiter_key):
            flash("Too many reset requests. Please wait a few minutes and try again.", "error")
            return redirect(url_for("forgot_password"))
        reset_limiter.hit(limiter_key)

        user = find_user_by_email(email) if email else None
        if user:
            reset_url = url_for("reset_password", token=make_password_reset_token(user), _external=True)
            if email_configured():
                send_notification_email(
                    user.email,
                    "Reset your IGSP password",
                    "We received a request to reset your password. The link below is valid for one hour. "
                    "If you didn't ask for this, you can ignore this email.",
                    recipient_name=user.contact_name or user.company_name,
                    action_url=reset_url,
                    action_label="Reset password",
                )
            else:
                app.logger.warning("Email not configured. Password reset link for %s: %s", user.email, reset_url)
        # Same response whether or not the account exists, so emails can't be enumerated.
        flash("If an account exists for that email, a password reset link has been sent.", "success")
        return redirect(url_for("login"))

    return render_template("forgot_password.html")


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    user = load_password_reset_token(token)
    if not user:
        flash("This password reset link is invalid or has expired. Please request a new one.", "error")
        return redirect(url_for("forgot_password"))

    if request.method == "POST":
        error = validate_new_password(request.form.get("password", ""), request.form.get("confirm_password", ""))
        if error:
            flash(error, "error")
            return redirect(url_for("reset_password", token=token))
        user.set_password(request.form["password"])
        user.clear_verification_code()
        DB.session.commit()
        flash("Your password has been reset. Please log in.", "success")
        return redirect(url_for("login"))

    return render_template("reset_password.html", token=token)


# ---------------------------------------------------------------------------
# Supplier onboarding wizard
# ---------------------------------------------------------------------------

def empty_draft(user):
    draft = {
        "company_name": user.company_name,
        "contact_name": user.contact_name,
        "email": user.email,
        "phone": user.phone,
        "address": user.address,
        "supplier_category": "",
        "supplier_categories": [],
        "supplier_category_detail": "",
        "supplier_category_details": {},
        "documents_status": "pending",
        "directors": [],
    }
    draft.update({field: "" for field in PROFILE_FIELDS})
    return draft


def registration_details(user):
    """Application form fields that can be taken from what the supplier entered at signup."""
    return {
        "registered_vendor_name": user.company_name or "",
        "physical_address": user.address or "",
        "primary_contact_person": user.contact_name or "",
        "contact_number": user.phone or "",
        "email_address": user.email or "",
    }


def get_onboarding_draft():
    user = get_current_user()
    draft = session.get("supplier_onboarding")
    if not draft or normalize_email(draft.get("email")) != normalize_email(user.email):
        draft = empty_draft(user)
    # Pre-fill from the registration; anything the supplier already typed wins.
    for field, value in registration_details(user).items():
        if not draft.get(field):
            draft[field] = value
    save_draft(draft)
    return draft


def save_draft(draft):
    session["supplier_onboarding"] = draft
    session.modified = True


def redirect_if_already_applied(user):
    if get_user_application(user):
        flash("You already have an application on file. You can view or update it from your dashboard.", "error")
        return redirect(url_for("dashboard"))
    return None


def pending_documents(user):
    return SupplierDocument.query.filter_by(user_id=user.id).filter(SupplierDocument.application_id.is_(None)).all()


def pending_company_profile(user):
    return (
        SupplierDocument.query.filter_by(user_id=user.id, document_type=COMPANY_PROFILE_DOC)
        .filter(SupplierDocument.application_id.is_(None))
        .first()
    )


def profile_step_error(draft, user):
    error = validate_profile_values(draft)
    if error:
        return error
    if not draft.get("directors"):
        return "Please provide at least one director or shareholder."
    if not pending_company_profile(user) and not draft.get("website"):
        return "Please upload your Company Profile or provide a company website."
    return None


@app.route("/onboarding/profile", methods=["GET", "POST"])
@login_required
def onboarding_profile():
    user = get_current_user()
    blocked = redirect_if_already_applied(user)
    if blocked:
        return blocked

    draft = get_onboarding_draft()

    if request.method == "POST":
        draft.update(read_fields(request.form, PROFILE_FIELDS))
        directors, director_error = parse_directors(request.form)
        if directors:
            draft["directors"] = directors
        save_draft(draft)

        if director_error:
            flash(director_error, "error")
            return redirect(url_for("onboarding_profile"))

        try:
            uploads = collect_uploads(request.files, {"document_file_company_profile": COMPANY_PROFILE_DOC})
        except UploadError as exc:
            flash(str(exc), "error")
            return redirect(url_for("onboarding_profile"))
        for doc_type, file_storage in uploads:
            store_document(user.id, None, doc_type, file_storage)
        DB.session.commit()

        error = profile_step_error(draft, user)
        if error:
            flash(error, "error")
            return redirect(url_for("onboarding_profile"))

        return redirect(url_for("onboarding_category"))

    return render_template(
        "supplier_profile.html",
        user=user,
        draft=draft,
        company_profile_doc=pending_company_profile(user),
    )


@app.route("/onboarding/category", methods=["GET", "POST"])
@login_required
def onboarding_category():
    user = get_current_user()
    blocked = redirect_if_already_applied(user)
    if blocked:
        return blocked

    draft = get_onboarding_draft()
    error = profile_step_error(draft, user)
    if error:
        flash("Please complete your company profile first.", "error")
        return redirect(url_for("onboarding_profile"))

    if request.method == "POST":
        categories, details, error = parse_categories(request.form)
        if error:
            flash(error, "error")
            return redirect(url_for("onboarding_category"))

        draft["supplier_categories"] = categories
        draft["supplier_category"] = categories[0]
        draft["supplier_category_details"] = details
        draft["supplier_category_detail"] = details.get(categories[0], "")
        save_draft(draft)
        return redirect(url_for("onboarding_documents"))

    return render_template("supplier_category.html", user=user, draft=draft)


@app.route("/onboarding/documents", methods=["GET", "POST"])
@login_required
def onboarding_documents():
    user = get_current_user()
    blocked = redirect_if_already_applied(user)
    if blocked:
        return blocked

    draft = get_onboarding_draft()
    if not draft.get("supplier_category"):
        flash("Please select a supplier category first.", "error")
        return redirect(url_for("onboarding_category"))

    if request.method == "POST":
        slots = {k: v for k, v in UPLOAD_SLOTS.items() if v != COMPANY_PROFILE_DOC}
        try:
            uploads = collect_uploads(request.files, slots)
        except UploadError as exc:
            flash(str(exc), "error")
            return redirect(url_for("onboarding_documents"))

        for doc_type, file_storage in uploads:
            store_document(user.id, None, doc_type, file_storage)
        DB.session.commit()

        missing = missing_required_documents(pending_documents(user))
        if missing:
            draft["documents_status"] = "pending"
            save_draft(draft)
            flash("Please upload all required documents before continuing. Missing: " + ", ".join(missing), "error")
            return redirect(url_for("onboarding_documents"))

        draft["documents_status"] = "complete"
        save_draft(draft)
        return redirect(url_for("onboarding_review"))

    return render_template("supplier_documents.html", user=user, draft=draft, documents=pending_documents(user))


@app.route("/onboarding/review", methods=["GET", "POST"])
@login_required
def onboarding_review():
    user = get_current_user()
    blocked = redirect_if_already_applied(user)
    if blocked:
        return blocked

    draft = get_onboarding_draft()
    documents = pending_documents(user)

    if request.method == "POST":
        error = profile_step_error(draft, user)
        if error:
            flash(error, "error")
            return redirect(url_for("onboarding_profile"))
        if not draft.get("supplier_categories"):
            flash("Please select a supplier category.", "error")
            return redirect(url_for("onboarding_category"))
        missing = missing_required_documents(documents)
        if missing:
            flash("Please upload all required documents. Missing: " + ", ".join(missing), "error")
            return redirect(url_for("onboarding_documents"))
        if not request.form.get("confirm"):
            flash("Please confirm that the information you provided is correct.", "error")
            return redirect(url_for("onboarding_review"))

        application = SupplierApplication(
            company_name=user.company_name,
            contact_name=user.contact_name,
            email=user.email,
            phone=user.phone,
            company_profile="",
            documents_status="complete",
            status="pending_review",
            **{field: draft.get(field, "") for field in PROFILE_FIELDS},
        )
        replace_categories(application, draft["supplier_categories"], draft.get("supplier_category_details", {}))
        replace_directors(application, draft["directors"])
        DB.session.add(application)
        DB.session.flush()

        for document in documents:
            document.application_id = application.id

        log_event(application, "submitted", to_status="pending_review",
                  summary=f"Submitted with {len(documents)} document(s)")
        notify_admins(
            "New Supplier Application",
            f"New application submitted by {user.company_name} ({user.contact_name}) for {application.supplier_category}.",
            application,
        )
        notify_supplier(
            user,
            "Application Received",
            "Thank you — your supplier application has been received and is awaiting review. "
            "We'll notify you when its status changes.",
            application,
        )
        DB.session.commit()

        session.pop("supplier_onboarding", None)
        flash("Supplier application submitted successfully.", "success")
        return redirect(url_for("dashboard"))

    documents_by_type = {doc.document_type: doc for doc in documents}
    return render_template(
        "supplier_review.html",
        user=user,
        draft=draft,
        documents=documents,
        documents_by_type=documents_by_type,
        required_documents=REQUIRED_DOCUMENTS,
        optional_documents=[COMPANY_PROFILE_DOC, OTHER_DOCS],
        missing_documents=missing_required_documents(documents),
        profile_complete=profile_step_error(draft, user) is None,
    )


# ---------------------------------------------------------------------------
# Supplier dashboard
# ---------------------------------------------------------------------------

def supplier_applications_query(user):
    return SupplierApplication.query.filter(func.lower(SupplierApplication.email) == normalize_email(user.email))


@app.route("/dashboard")
@login_required
def dashboard():
    user = get_current_user()
    page = request.args.get("page", 1, type=int)
    base_query = supplier_applications_query(user)
    pagination = base_query.order_by(SupplierApplication.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    latest = pagination.items[0] if pagination.items else None

    return render_template(
        "dashboard.html",
        user=user,
        applications=pagination.items,
        pagination=pagination,
        approved_count=base_query.filter(SupplierApplication.status == "approved").count(),
        pending_count=base_query.filter(SupplierApplication.status == "pending_review").count(),
        documents=SupplierDocument.query.filter_by(user_id=user.id).all(),
        contact_phone=(latest.phone if latest and latest.phone else user.phone),
    )


@app.route("/settings")
@login_required
def supplier_settings():
    return render_template("supplier_settings.html", user=get_current_user())


@app.route("/settings/password", methods=["POST"])
@login_required
def change_password():
    user = get_current_user()
    if not user.check_password(request.form.get("current_password", "")):
        flash("Your current password is incorrect.", "error")
        return redirect(url_for("supplier_settings"))
    error = validate_new_password(request.form.get("new_password", ""), request.form.get("confirm_password", ""))
    if error:
        flash(error, "error")
        return redirect(url_for("supplier_settings"))
    user.set_password(request.form["new_password"])
    DB.session.commit()
    flash("Your password has been updated.", "success")
    return redirect(url_for("supplier_settings"))


@app.route("/settings/contact", methods=["POST"])
@login_required
def update_contact_details():
    user = get_current_user()
    values = read_fields(request.form, ["company_name", "contact_name", "phone", "address"])
    missing = missing_field_labels(values, ["company_name", "contact_name", "phone"])
    if missing:
        flash("Please complete: " + ", ".join(missing) + ".", "error")
        return redirect(url_for("supplier_settings"))
    user.company_name = values["company_name"][:150]
    user.contact_name = values["contact_name"][:120]
    user.name = values["contact_name"][:100]
    user.phone = values["phone"][:50]
    user.address = values["address"][:255]
    DB.session.commit()
    session.pop("supplier_onboarding", None)
    flash("Your account details have been updated.", "success")
    return redirect(url_for("supplier_settings"))


def get_owned_application(application_id):
    user = get_current_user()
    application = DB.get_or_404(SupplierApplication, application_id)
    if normalize_email(application.email) != normalize_email(user.email):
        abort(404)
    return application


@app.route("/applications/<int:application_id>/edit", methods=["GET", "POST"])
@login_required
def edit_application(application_id):
    user = get_current_user()
    application = get_owned_application(application_id)
    documents = documents_for_application(application, user)

    if application.status not in EDITABLE_STATUSES:
        flash(f"This application is {status_label(application.status)} and can no longer be edited. "
              "Contact us if your details have changed.", "error")
        return redirect(url_for("application_detail", application_id=application.id))

    if request.method == "POST":
        edit_url = url_for("edit_application", application_id=application.id)
        values = read_fields(request.form, ["company_name", "contact_name", "phone"] + PROFILE_FIELDS)

        missing = missing_field_labels(values, ["company_name", "contact_name", "phone"],
                                       {"company_name": "Company Name", "contact_name": "Key Contact", "phone": "Phone"})
        error = ("Please complete the required fields: " + ", ".join(missing) + ".") if missing else validate_profile_values(values)
        directors, director_error = parse_directors(request.form)
        categories, details, category_error = parse_categories(request.form)
        for problem in (error, director_error, category_error):
            if problem:
                flash(problem, "error")
                return redirect(edit_url)

        try:
            uploads = collect_uploads(request.files)
        except UploadError as exc:
            flash(str(exc), "error")
            return redirect(edit_url)

        has_profile_doc = any(d.document_type == COMPANY_PROFILE_DOC for d in documents) or any(
            doc_type == COMPANY_PROFILE_DOC for doc_type, _ in uploads
        )
        if not has_profile_doc and not values["website"]:
            flash("Please upload your Company Profile or provide a company website.", "error")
            return redirect(edit_url)

        # Validation passed — apply everything in one transaction.
        before = application_snapshot(application)
        previous_files = {d.document_type: d.original_filename for d in documents}
        old_status = application.status
        for field, value in values.items():
            setattr(application, field, value)
        replace_categories(application, categories, details)
        replace_directors(application, directors)
        for document in documents:
            if document.application_id is None:
                document.application_id = application.id
        DB.session.flush()
        for doc_type, file_storage in uploads:
            store_document(user.id, application.id, doc_type, file_storage)
        DB.session.flush()

        all_docs = SupplierDocument.query.filter_by(application_id=application.id).all()
        application.documents_status = "pending" if missing_required_documents(all_docs) else "complete"

        resubmitted = application.status in {"returned_for_update", "declined"}
        if resubmitted:
            application.status = "pending_review"

        changes = diff_snapshots(before, application_snapshot(application))
        changes += [{"field": f"Document: {doc_type}", "old": previous_files.get(doc_type, ""), "new": fs.filename}
                    for doc_type, fs in uploads]
        log_event(
            application, "resubmitted" if resubmitted else "updated",
            summary=(f"{'Resubmitted' if resubmitted else 'Updated'}: {len(changes)} change(s)" if changes
                     else ("Resubmitted without changes" if resubmitted else "Saved without changes")),
            from_status=old_status if resubmitted else None,
            to_status=application.status if resubmitted else None,
            changes=changes,
        )
        notify_admins(
            "Application Resubmitted" if resubmitted else "Application Updated",
            f"Supplier {application.company_name} ({application.contact_name}) "
            f"{'resubmitted' if resubmitted else 'updated'} their application.",
            application,
        )
        DB.session.commit()

        flash("Application resubmitted for review." if resubmitted else "Application updated successfully.", "success")
        return redirect(url_for("application_detail", application_id=application.id))

    return render_template(
        "edit_application.html",
        user=user,
        application=application,
        documents=documents,
        doc_types=REQUIRED_DOCUMENTS,
        selected_categories=[normalize_category_value(c) for c in application.category_names],
        selected_details={normalize_category_value(ac.category): ac.category_detail for ac in application.application_categories},
        directors=[
            {"initials_surname": d.initials_surname, "id_number": d.id_number, "role": d.role, "nationality": d.nationality}
            for d in application.directors
        ],
    )


@app.route("/applications/<int:application_id>")
@login_required
def application_detail(application_id):
    user = get_current_user()
    application = get_owned_application(application_id)
    return render_template(
        "application_detail.html",
        user=user,
        application=application,
        documents=documents_for_application(application, user),
        can_edit=application.status in EDITABLE_STATUSES,
    )


@app.route("/apps")
@login_required
def applications():
    user = get_current_user()
    search_query = request.args.get("q", "").strip()
    current_status = request.args.get("status", "all").strip()
    page = request.args.get("page", 1, type=int)

    base_query = supplier_applications_query(user)
    if search_query:
        like_pattern = f"%{search_query}%"
        base_query = base_query.filter(DB.or_(
            SupplierApplication.company_name.ilike(like_pattern),
            SupplierApplication.contact_name.ilike(like_pattern),
        ))
    if current_status != "all":
        base_query = base_query.filter(SupplierApplication.status == current_status)

    total_count = base_query.count()
    approved_count = base_query.filter(SupplierApplication.status == "approved").count()
    under_review_count = base_query.filter(SupplierApplication.status == "pending_review").count()
    declined_count = base_query.filter(SupplierApplication.status == "declined").count()

    def percent(n):
        return round(n / total_count * 100) if total_count else 0

    pagination = base_query.order_by(SupplierApplication.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    return render_template(
        "application.html",
        user=user,
        applications=pagination.items,
        pagination=pagination,
        approved_count=approved_count,
        under_review_count=under_review_count,
        declined_count=declined_count,
        total_count=total_count,
        approved_percent=percent(approved_count),
        under_review_percent=percent(under_review_count),
        declined_percent=percent(declined_count),
        current_status=current_status,
        search_query=search_query,
    )


@app.route("/notifications")
@login_required
def notifications():
    user = get_current_user()
    page = request.args.get("page", 1, type=int)
    pagination = (
        Notification.query.filter_by(user_id=user.id)
        .order_by(Notification.created_at.desc())
        .paginate(page=page, per_page=10, error_out=False)
    )
    new_ids = {n.id for n in pagination.items if not n.is_read}
    unread_count = Notification.query.filter_by(user_id=user.id, is_read=False).count()
    Notification.query.filter_by(user_id=user.id, is_read=False).update({"is_read": True})
    DB.session.commit()

    return render_template(
        "notifications.html",
        user=user,
        new_ids=new_ids,
        notifications=pagination.items,
        pagination=pagination,
        unread_notifications=unread_count,
    )


@app.route("/notifications/mark-read/<int:notification_id>", methods=["POST"])
@login_required
def mark_notification_read(notification_id):
    user = get_current_user()
    notification = Notification.query.filter_by(id=notification_id, user_id=user.id).first_or_404()
    notification.is_read = True
    DB.session.commit()
    return redirect(url_for("notifications"))


@app.route("/uploads/<path:filepath>")
def serve_upload(filepath):
    """Serve an uploaded document to an admin or to the supplier who owns it."""
    user = get_current_user()
    admin = get_current_admin()
    if not user and not admin:
        abort(404)

    normalized = filepath.replace("\\", "/")
    absolute_path = absolute_upload_path(normalized)
    if not absolute_path or not os.path.isfile(absolute_path):
        abort(404)

    document = SupplierDocument.query.filter_by(file_path=normalized).first()
    parts = normalized.split("/")
    owns_file = user is not None and len(parts) >= 3 and parts[1] == str(user.id)

    if not owns_file:
        # Any admin may open documents attached to an application; every view is logged.
        application = DB.session.get(SupplierApplication, document.application_id) if document and document.application_id else None
        if not admin or not application:
            abort(404)
        log_event(application, "document_viewed", summary=f"Viewed {document.document_type}",
                  changes=[{"field": document.document_type, "old": "", "new": document.original_filename}], actor=admin)
        DB.session.commit()

    download_name = document.original_filename if document else os.path.basename(absolute_path)
    return send_file(absolute_path, mimetype="application/pdf", as_attachment=False, download_name=download_name)


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if get_current_admin():
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        email = normalize_email(request.form.get("email"))
        password = request.form.get("password", "")
        limiter_key = ("admin-login", client_ip(), email)

        if not email or not password:
            return render_template("admin_login.html", step="options", login_error="Email and password are required.")
        if login_limiter.is_blocked(limiter_key):
            return render_template("admin_login.html", step="options", login_error="Too many login attempts. Please wait 15 minutes and try again.")

        admin = find_admin_by_email(email)
        if not admin or not admin.check_password(password):
            login_limiter.hit(limiter_key)
            return render_template("admin_login.html", step="options", login_error="Invalid email or password.")

        login_limiter.reset(limiter_key)
        session["admin_id"] = admin.id
        session.permanent = True
        return redirect(url_for("admin_dashboard"))

    return render_template("admin_login.html", step="start")


@app.route("/admin/login/options")
def admin_login_options():
    if get_current_admin():
        return redirect(url_for("admin_dashboard"))
    return render_template("admin_login.html", step="options")


@app.route("/admin/login/microsoft")
def admin_login_microsoft():
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        flash("Microsoft SSO is not configured.", "error")
        return redirect(url_for("admin_login_options"))
    return oauth.microsoft.authorize_redirect(url_for("admin_login_microsoft_callback", _external=True))


@app.route("/admin/login/microsoft/callback")
def admin_login_microsoft_callback():
    if not MICROSOFT_CLIENT_ID or not MICROSOFT_CLIENT_SECRET:
        return redirect(url_for("admin_login_options"))
    try:
        token = oauth.microsoft.authorize_access_token()
        user_info = token.get("userinfo") or oauth.microsoft.userinfo()
    except Exception as exc:  # noqa: BLE001
        app.logger.error("Microsoft authentication failed: %s", exc)
        flash("Microsoft authentication failed. Please try again.", "error")
        return redirect(url_for("admin_login_options"))

    email = user_info.get("email") or user_info.get("preferred_username")
    if not email:
        flash("Could not retrieve an email address from your Microsoft account.", "error")
        return redirect(url_for("admin_login_options"))

    admin = find_admin_by_email(email)
    if not admin:
        flash("No admin account found for this Microsoft account.", "error")
        return redirect(url_for("admin_login_options"))

    session["admin_id"] = admin.id
    session.permanent = True
    flash(f"Welcome, {admin.name or admin.email}", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_id", None)
    return redirect(url_for("admin_login"))


AWAITING_REVIEW_STATUSES = ["submitted", "pending_review", "under_review"]


@app.route("/admin")
@admin_required
def admin_dashboard():
    status_counts = {status: 0 for status in STATUSES}
    for status, count in DB.session.query(SupplierApplication.status, func.count()).group_by(SupplierApplication.status):
        if status in status_counts:
            status_counts[status] = count
    return render_template(
        "admin_dashboard.html",
        admin=get_current_admin(),
        total_suppliers=User.query.count(),
        active_suppliers=User.query.filter_by(active=True).count(),
        total_applications=SupplierApplication.query.count(),
        status_counts=status_counts,
        awaiting_count=sum(status_counts[s] for s in AWAITING_REVIEW_STATUSES),
        assigned_to_me_count=SupplierApplication.query.filter(
            SupplierApplication.assigned_admin_id == get_current_admin().id,
            SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES)).count(),
        unassigned_count=SupplierApplication.query.filter(
            SupplierApplication.assigned_admin_id.is_(None),
            SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES)).count(),
        pending_count=status_counts["pending_review"],
        approved_count=status_counts["approved"],
        rejected_count=status_counts["declined"],
        awaiting_review=(
            SupplierApplication.query.filter(SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES))
            .order_by(SupplierApplication.created_at.asc()).limit(6).all()
        ),
        recent_applications=SupplierApplication.query.order_by(SupplierApplication.created_at.desc()).limit(8).all(),
    )


@app.route("/admin/suppliers")
@admin_required
def admin_suppliers():
    search = request.args.get("search", "").strip()
    account_filter = request.args.get("account", "").strip()
    if account_filter not in {"active", "inactive"}:
        account_filter = ""
    application_filter = request.args.get("application", "").strip()
    if application_filter != "none" and application_filter not in STATUSES:
        application_filter = ""
    page = request.args.get("page", 1, type=int)

    # Each supplier has at most one application, linked by email.
    base = User.query.outerjoin(SupplierApplication, func.lower(SupplierApplication.email) == func.lower(User.email))
    if search:
        like = f"%{search}%"
        base = base.filter(DB.or_(
            User.company_name.ilike(like),
            User.contact_name.ilike(like),
            User.email.ilike(like),
            User.supplier_id.ilike(like),
        ))

    def by_account(query, value):
        if value == "active":
            return query.filter(User.active.is_(True))
        if value == "inactive":
            return query.filter(User.active.is_(False))
        return query

    def by_application(query, value):
        if value == "none":
            return query.filter(SupplierApplication.id.is_(None))
        return query.filter(SupplierApplication.status == value) if value else query

    # Slicer cards: each count is what clicking the card would show, given the other slicer and the search.
    in_application = by_application(base, application_filter)
    account_cards = [("", "All suppliers", in_application.count()),
                     ("active", "Active", by_account(in_application, "active").count()),
                     ("inactive", "Inactive", by_account(in_application, "inactive").count())]
    rows = dict(by_account(base, account_filter).with_entities(
        SupplierApplication.status, func.count(User.id)).group_by(SupplierApplication.status).all())
    application_cards = [("none", "Not submitted", rows.get(None, 0))] + [
        (status, status_label(status), rows.get(status, 0))
        for status in STATUSES
        if status != "draft" and (status != "submitted" or rows.get(status) or application_filter == status)
    ]

    query = by_application(by_account(base, account_filter), application_filter)
    pagination = query.order_by(User.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    emails = [normalize_email(u.email) for u in pagination.items]
    applications_by_email = {}
    if emails:
        for application in SupplierApplication.query.filter(func.lower(SupplierApplication.email).in_(emails)):
            applications_by_email[normalize_email(application.email)] = application
    return render_template(
        "admin_suppliers.html",
        admin=get_current_admin(),
        suppliers=pagination.items,
        pagination=pagination,
        search=search,
        account_filter=account_filter,
        application_filter=application_filter,
        account_cards=account_cards,
        application_cards=application_cards,
        applications_by_email=applications_by_email,
        normalize_email=normalize_email,
    )


@app.route("/admin/suppliers/<int:supplier_id>/toggle", methods=["POST"])
@admin_required
def toggle_supplier(supplier_id):
    supplier = DB.get_or_404(User, supplier_id)
    supplier.active = not supplier.active
    state = "activated" if supplier.active else "deactivated"
    supplier_application = get_user_application(supplier)
    if supplier_application:
        log_event(supplier_application, f"account_{state}", summary=f"Supplier account {state}",
                  changes=[{"field": "Account", "old": "Inactive" if supplier.active else "Active",
                            "new": "Active" if supplier.active else "Inactive"}])
    notify_supplier(supplier, "Account Status Updated", f"Your supplier account has been {state} by the administrator.")
    DB.session.commit()
    flash(f"Supplier {state} successfully.", "success")
    return redirect(safe_next_url(request.form.get("next"), url_for("admin_suppliers")))


@app.route("/admin/applications")
@admin_required
def admin_applications():
    search = request.args.get("search", "").strip()
    status_filter = request.args.get("status", "").strip()
    assigned_filter = request.args.get("assigned", "").strip()
    page = request.args.get("page", 1, type=int)
    admin = get_current_admin()

    if status_filter not in STATUSES:
        status_filter = ""
    if assigned_filter not in {"me", "unassigned"}:
        assigned_filter = ""

    base = SupplierApplication.query.filter(SupplierApplication.status != "draft")
    if search:
        like = f"%{search}%"
        base = base.filter(DB.or_(
            SupplierApplication.company_name.ilike(like),
            SupplierApplication.contact_name.ilike(like),
            SupplierApplication.email.ilike(like),
        ))

    def by_assignment(query, who):
        if who == "me":
            return query.filter(SupplierApplication.assigned_admin_id == admin.id)
        if who == "unassigned":
            return query.filter(SupplierApplication.assigned_admin_id.is_(None))
        return query

    def by_status(query, status):
        return query.filter(SupplierApplication.status == status) if status else query

    # Slicer cards. Each card's count is what clicking it would show, given the other slicer and the search.
    status_rows = dict(by_assignment(base, assigned_filter).with_entities(
        SupplierApplication.status, func.count(SupplierApplication.id)).group_by(SupplierApplication.status).all())
    status_cards = [("", "All applications", sum(status_rows.values()))] + [
        (status, status_label(status), status_rows.get(status, 0))
        for status in STATUSES
        if status != "draft" and (status != "submitted" or status_rows.get(status) or status_filter == status)
    ]
    in_status = by_status(base, status_filter)
    assignment_cards = [
        ("", "Everyone's", in_status.count()),
        ("me", "Assigned to me", by_assignment(in_status, "me").count()),
        ("unassigned", "Unassigned", by_assignment(in_status, "unassigned").count()),
    ]

    query = by_status(by_assignment(base, assigned_filter), status_filter)
    pagination = query.order_by(SupplierApplication.created_at.desc()).paginate(page=page, per_page=10, error_out=False)
    return render_template(
        "admin_applications.html",
        admin=admin,
        applications=pagination.items,
        pagination=pagination,
        search=search,
        status_filter=status_filter,
        assigned_filter=assigned_filter,
        statuses=STATUSES,
        status_cards=status_cards,
        assignment_cards=assignment_cards,
    )


def is_assigned_to_me(application):
    admin = get_current_admin()
    return bool(admin and application.assigned_admin_id == admin.id)


def assigned_application_or_redirect(application_id):
    """Load an application for an action that only its assigned admin may take."""
    application = DB.get_or_404(SupplierApplication, application_id)
    if not is_assigned_to_me(application):
        owner = application.assigned_admin
        if owner:
            flash(f"Only {owner.name or owner.email} can work on this application because it's assigned to them.", "error")
        else:
            flash("Assign this application to yourself before working on it.", "error")
        return application, redirect(url_for("admin_application_detail", application_id=application.id))
    return application, None


@app.route("/admin/applications/<int:application_id>/assign", methods=["POST"])
@admin_required
def assign_application(application_id):
    admin = get_current_admin()
    application = DB.get_or_404(SupplierApplication, application_id)
    detail_url = url_for("admin_application_detail", application_id=application.id)
    owner = application.assigned_admin

    if owner:
        if owner.id != admin.id:
            flash(f"This application is already assigned to {owner.name or owner.email}. "
                  "They need to unassign themselves before anyone else can take it.", "error")
        return redirect(detail_url)

    me = admin.name or admin.email
    application.assigned_admin_id = admin.id
    application.assigned_at = utcnow()
    log_event(application, "assigned", summary=f"Assigned to {me}",
              changes=[{"field": "Assigned to", "old": "", "new": me}])
    DB.session.commit()
    flash(f"{application.company_name} is now assigned to you.", "success")
    return redirect(detail_url)


@app.route("/admin/applications/<int:application_id>/release", methods=["POST"])
@admin_required
def release_application(application_id):
    application, blocked = assigned_application_or_redirect(application_id)
    if blocked:
        return blocked
    me = get_current_admin()
    application.assigned_admin_id = None
    application.assigned_at = None
    log_event(application, "unassigned", summary=f"{me.name or me.email} unassigned themselves",
              changes=[{"field": "Assigned to", "old": me.name or me.email, "new": ""}])
    DB.session.commit()
    flash("You've been unassigned. Another admin can now assign this application to themselves.", "success")
    return redirect(url_for("admin_application_detail", application_id=application.id))


@app.route("/admin/applications/<int:application_id>")
@admin_required
def admin_application_detail(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    supplier = get_application_supplier(application)
    documents = documents_for_application(application, supplier)
    return render_template(
        "admin_application_detail.html",
        admin=get_current_admin(),
        application=application,
        supplier=supplier,
        documents=documents,
        company_profile_doc=[d for d in documents if d.document_type == COMPANY_PROFILE_DOC],
        missing_documents=missing_required_documents(documents),
        statuses=STATUSES,
        is_mine=is_assigned_to_me(application),
        history_count=len(application_history(application)),
    )


@app.route("/admin/applications/<int:application_id>/history")
@admin_required
def admin_application_history(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    return render_template(
        "admin_application_history.html",
        admin=get_current_admin(),
        application=application,
        supplier=get_application_supplier(application),
        history=application_history(application),
        event_labels=EVENT_LABELS,
    )


def application_history(application):
    """Newest-first audit trail. Applications created before auditing existed get a placeholder entry."""
    events = (ApplicationEvent.query.filter_by(application_id=application.id)
              .order_by(ApplicationEvent.created_at.desc(), ApplicationEvent.id.desc()).all())
    if not any(e.action == "submitted" for e in events) and application.created_at:
        events.append(ApplicationEvent(
            application_id=application.id, created_at=application.created_at, actor_type="system",
            actor_name=application.contact_name or "", action="submitted",
            summary="Submitted (recorded before the audit trail was enabled)", to_status=None,
        ))
    return events


@app.route("/admin/applications/<int:application_id>/history.csv")
@admin_required
def admin_application_history_export(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date & time (UTC)", "Actor type", "Actor", "Actor email", "Action", "Summary",
                     "From status", "To status", "Field", "Old value", "New value", "IP address"])
    for event in reversed(application_history(application)):
        base = [
            event.created_at.strftime("%Y-%m-%d %H:%M:%S") if event.created_at else "",
            event.actor_type, event.actor_name, event.actor_email,
            EVENT_LABELS.get(event.action, event.action), event.summary,
            status_label(event.from_status) if event.from_status else "",
            status_label(event.to_status) if event.to_status else "",
        ]
        for change in event.changes or [{}]:
            writer.writerow(base + [change.get("field", ""), change.get("old", ""), change.get("new", ""), event.ip_address])
    safe_name = secure_filename(application.company_name or "application") or "application"
    return Response(
        "\ufeff" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={safe_name}_audit_trail.csv"},
    )


@app.route("/admin/applications/<int:application_id>/update", methods=["POST"])
@admin_required
def update_application(application_id):
    application, blocked = assigned_application_or_redirect(application_id)
    if blocked:
        return blocked
    old_status = application.status
    old_comments = (application.review_comments or "").strip()

    new_status = request.form.get("status", application.status)
    new_doc_status = request.form.get("documents_status", application.documents_status)
    if new_status not in STATUSES or new_doc_status not in DOCUMENT_STATUSES:
        flash("Invalid status selected.", "error")
        return redirect(url_for("admin_application_detail", application_id=application.id))
    if new_status in {"declined", "returned_for_update"} and not request.form.get("review_comments", "").strip():
        flash("Please add a comment explaining to the supplier why the application is "
              f"{'declined' if new_status == 'declined' else 'being returned'}.", "error")
        return redirect(url_for("admin_application_detail", application_id=application.id))

    old_doc_status = application.documents_status
    application.status = new_status
    application.documents_status = new_doc_status
    application.review_comments = request.form.get("review_comments", "").strip()

    changes = []
    if old_doc_status != new_doc_status:
        changes.append({"field": "Documents status", "old": (old_doc_status or "").title(), "new": new_doc_status.title()})
    if application.review_comments != old_comments:
        changes.append({"field": "Review comments", "old": old_comments, "new": application.review_comments})
    if old_status != new_status:
        log_event(application, "status_changed", summary=f"{status_label(old_status)} → {status_label(new_status)}",
                  from_status=old_status, to_status=new_status, changes=changes)
    elif changes:
        log_event(application, "review_updated", summary=", ".join(c["field"] for c in changes) + " updated",
                  changes=changes)

    supplier = get_application_supplier(application)
    if supplier:
        if old_status != application.status:
            notify_supplier(
                supplier,
                "Application Status Updated",
                f"Your application for {application.company_name} has been updated to: {status_label(application.status)}.",
                application,
            )
            if application.status == "approved" and not supplier.active:
                supplier.active = True
                log_event(application, "account_activated", summary="Supplier account activated automatically on approval",
                          changes=[{"field": "Account", "old": "Inactive", "new": "Active"}])
                notify_supplier(
                    supplier, "Account Activated",
                    "Congratulations! Your supplier account has been activated following the approval of your application.",
                    application,
                )
            elif application.status == "declined" and supplier.active:
                supplier.active = False
                log_event(application, "account_deactivated", summary="Supplier account deactivated automatically on decline",
                          changes=[{"field": "Account", "old": "Active", "new": "Inactive"}])
                notify_supplier(
                    supplier, "Account Deactivated",
                    "Your supplier account has been deactivated because your application was declined.",
                    application,
                )
        if application.review_comments and application.review_comments != old_comments:
            notify_supplier(
                supplier, "Review Comments Added",
                f"An administrator added review comments to your application: {application.review_comments}",
                application,
            )

    DB.session.commit()
    flash("Application updated successfully.", "success")
    return redirect(url_for("admin_application_detail", application_id=application.id))


@app.route("/admin/applications/<int:application_id>/documents/<int:document_id>/replace", methods=["POST"])
@admin_required
def replace_admin_document(application_id, document_id):
    application, blocked = assigned_application_or_redirect(application_id)
    if blocked:
        return blocked
    document = DB.get_or_404(SupplierDocument, document_id)
    detail_url = url_for("admin_application_detail", application_id=application_id)

    if document not in documents_for_application(application, get_application_supplier(application)):
        flash("Document does not belong to this application.", "error")
        return redirect(detail_url)

    file_storage = request.files.get("document_file")
    if not file_storage or not file_storage.filename:
        flash("Please select a file to replace the document.", "error")
        return redirect(detail_url)
    try:
        validate_pdf(file_storage)
    except UploadError as exc:
        flash(str(exc), "error")
        return redirect(detail_url)

    log_event(application, "document_replaced", summary=f"Replaced {document.document_type}",
              changes=[{"field": document.document_type, "old": document.original_filename, "new": file_storage.filename}])
    new_path = save_upload(file_storage, document.user_id)
    remove_upload(document.file_path)
    document.file_path = new_path
    document.original_filename = file_storage.filename
    document.uploaded_at = utcnow()
    DB.session.commit()

    flash("Document replaced successfully.", "success")
    return redirect(detail_url)


@app.route("/admin/applications/<int:application_id>/download-all")
@admin_required
def download_all_documents(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    supplier = get_application_supplier(application)
    documents = documents_for_application(application, supplier)

    zip_buffer = io.BytesIO()
    used_names = set()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        for doc in documents:
            path = absolute_upload_path(doc.file_path)
            if not path or not os.path.isfile(path):
                continue
            base = secure_filename(f"{doc.document_type} - {doc.original_filename}") or f"document_{doc.id}.pdf"
            name, n = base, 1
            while name in used_names:
                n += 1
                stem, ext = os.path.splitext(base)
                name = f"{stem}_{n}{ext}"
            used_names.add(name)
            zipf.write(path, name)

    zip_buffer.seek(0)
    log_event(application, "documents_downloaded", summary=f"Downloaded {len(used_names)} document(s) as ZIP")
    DB.session.commit()
    company_name = (supplier.company_name if supplier and supplier.company_name else application.company_name) or "supplier"
    safe_name = secure_filename(company_name) or "supplier"
    return send_file(zip_buffer, mimetype="application/zip", as_attachment=True,
                     download_name=f"{safe_name}_documents.zip")


@app.route("/admin/notifications")
@admin_required
def admin_notifications():
    admin = get_current_admin()
    page = request.args.get("page", 1, type=int)
    pagination = (
        Notification.query.filter_by(admin_id=admin.id)
        .order_by(Notification.created_at.desc())
        .paginate(page=page, per_page=10, error_out=False)
    )
    new_ids = {n.id for n in pagination.items if not n.is_read}
    unread_count = Notification.query.filter_by(admin_id=admin.id, is_read=False).count()
    Notification.query.filter_by(admin_id=admin.id, is_read=False).update({"is_read": True})
    DB.session.commit()

    return render_template(
        "admin_notifications.html",
        admin=admin,
        notifications=pagination.items,
        new_ids=new_ids,
        pagination=pagination,
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


def report_filters(args):
    filters = {
        "search": args.get("search", "").strip(),
        "status_filter": args.get("status", "").strip(),
        "category_filter": args.get("category", "").strip(),
        "date_from": args.get("date_from", "").strip(),
        "date_to": args.get("date_to", "").strip(),
    }
    if filters["status_filter"] not in STATUSES:
        filters["status_filter"] = ""
    for key in ("date_from", "date_to"):
        try:
            datetime.strptime(filters[key], "%Y-%m-%d")
        except ValueError:
            filters[key] = ""
    # ISO dates compare correctly as strings; accept a reversed range instead of returning nothing.
    if filters["date_from"] and filters["date_to"] and filters["date_from"] > filters["date_to"]:
        filters["date_from"], filters["date_to"] = filters["date_to"], filters["date_from"]
    return filters


def month_range(first, last):
    """Every YYYY-MM from first to last inclusive, so gaps show as zero instead of disappearing."""
    year, month = map(int, first.split("-"))
    end_year, end_month = map(int, last.split("-"))
    months = []
    while (year, month) <= (end_year, end_month):
        months.append(f"{year:04d}-{month:02d}")
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return months


def report_filter_options():
    """Statuses and categories that actually occur, so the dropdowns never offer empty choices."""
    used_statuses = {s for (s,) in DB.session.query(SupplierApplication.status).distinct() if s}
    categories = {c for (c,) in DB.session.query(SupplierApplicationCategory.category).distinct() if c}
    categories |= {c for (c,) in DB.session.query(SupplierApplication.supplier_category).distinct() if c}
    return [s for s in STATUSES if s in used_statuses], sorted(categories)


def filtered_applications_query(filters):
    query = SupplierApplication.query
    if filters["search"]:
        like = f"%{filters['search']}%"
        query = query.filter(DB.or_(
            SupplierApplication.company_name.ilike(like),
            SupplierApplication.contact_name.ilike(like),
        ))
    if filters["status_filter"]:
        query = query.filter(SupplierApplication.status == filters["status_filter"])
    if filters["category_filter"]:
        category = filters["category_filter"]
        query = query.filter(DB.or_(
            SupplierApplication.supplier_category == category,
            SupplierApplication.application_categories.any(SupplierApplicationCategory.category == category),
        ))
    try:
        if filters["date_from"]:
            query = query.filter(SupplierApplication.created_at >= datetime.strptime(filters["date_from"], "%Y-%m-%d"))
    except ValueError:
        pass
    try:
        if filters["date_to"]:
            # Inclusive: everything up to the end of the selected day.
            end = datetime.strptime(filters["date_to"], "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(SupplierApplication.created_at < end)
    except ValueError:
        pass
    return query


def users_by_email(applications):
    emails = {normalize_email(a.email) for a in applications if a.email}
    if not emails:
        return {}
    users = User.query.filter(func.lower(User.email).in_(emails)).all()
    return {normalize_email(u.email): u for u in users}


@app.route("/admin/reports")
@admin_required
def admin_reports():
    filters = report_filters(request.args)
    base_query = filtered_applications_query(filters)
    filtered_applications = base_query.order_by(SupplierApplication.created_at.asc()).all()
    suppliers = users_by_email(filtered_applications)

    status_breakdown = {status: 0 for status in STATUSES}
    doc_status_breakdown = {"complete": 0, "pending": 0}
    category_breakdown = {}
    monthly_trends = {}
    for application in filtered_applications:
        month_key = application.created_at.strftime("%Y-%m") if application.created_at else "Unknown"
        monthly_trends[month_key] = monthly_trends.get(month_key, 0) + 1
        for category in application.category_names or ["Uncategorized"]:
            category_breakdown[category] = category_breakdown.get(category, 0) + 1
        if application.status in status_breakdown:
            status_breakdown[application.status] += 1
        doc_status_breakdown["complete" if application.documents_status == "complete" else "pending"] += 1

    monthly_active, monthly_inactive = {}, {}
    for supplier in suppliers.values():
        if supplier.created_at:
            bucket = monthly_active if supplier.active else monthly_inactive
            key = supplier.created_at.strftime("%Y-%m")
            bucket[key] = bucket.get(key, 0) + 1

    application_months = sorted(m for m in monthly_trends if m != "Unknown")
    all_months = month_range(application_months[0], application_months[-1]) if application_months else []

    page = request.args.get("page", 1, type=int)
    pagination = base_query.order_by(SupplierApplication.created_at.desc()).paginate(page=page, per_page=10, error_out=False)

    available_statuses, all_categories = report_filter_options()
    # Keep the current selection visible even if it no longer matches anything.
    if filters["status_filter"] and filters["status_filter"] not in available_statuses:
        available_statuses = [s for s in STATUSES if s in set(available_statuses) | {filters["status_filter"]}]
    if filters["category_filter"] and filters["category_filter"] not in all_categories:
        all_categories = sorted(set(all_categories) | {filters["category_filter"]})

    payload = []
    for application in filtered_applications:
        supplier = suppliers.get(normalize_email(application.email))
        payload.append({
            "id": application.id,
            "company_name": application.company_name,
            "contact_name": application.contact_name,
            "categories": application.category_names,
            "category_details": {ac.category: ac.category_detail for ac in application.application_categories},
            "status": application.status,
            "documents_status": application.documents_status,
            "created_at": application.created_at.strftime("%Y-%m-%d") if application.created_at else "N/A",
            "user_active": supplier.active if supplier else False,
            "user_created_at": supplier.created_at.strftime("%Y-%m-%d") if supplier and supplier.created_at else None,
        })

    active_suppliers = sum(1 for s in suppliers.values() if s.active)
    return render_template(
        "admin_reports.html",
        report_kinds=REPORT_KINDS,
        admin=get_current_admin(),
        total_suppliers=len(suppliers),
        active_suppliers=active_suppliers,
        inactive_suppliers=len(suppliers) - active_suppliers,
        total_applications=len(filtered_applications),
        pending_count=status_breakdown["pending_review"],
        approved_count=status_breakdown["approved"],
        rejected_count=status_breakdown["declined"],
        submitted_count=status_breakdown["submitted"],
        status_breakdown=status_breakdown,
        category_breakdown=category_breakdown,
        doc_status_breakdown=doc_status_breakdown,
        chart_labels=all_months,
        chart_app_trend=[monthly_trends.get(m, 0) for m in all_months],
        chart_supplier_trend=[monthly_active.get(m, 0) for m in all_months],
        chart_inactive_supplier_trend=[monthly_inactive.get(m, 0) for m in all_months],
        recent_applications=pagination.items,
        pagination=pagination,
        all_applications_json=payload,
        category_breakdown_sorted=sorted(category_breakdown.items(), key=lambda kv: (-kv[1], kv[0])),
        page_args={k: v for k, v in {
            "search": filters["search"], "status": filters["status_filter"], "category": filters["category_filter"],
            "date_from": filters["date_from"], "date_to": filters["date_to"],
        }.items() if v},
        all_statuses=available_statuses,
        all_categories=all_categories,
        **filters,
    )


# ---- Additional reports (one template, one shape: KPIs + charts + table) ----

REPORT_KINDS = [
    ("applications", "Applications"),
    ("requests", "Product requests"),
    ("suppliers", "Suppliers"),
    ("inventory", "Inventory"),
    ("activity", "Admin activity"),
]
REPORT_PERIODS = [("30d", "Last 30 days"), ("90d", "Last 90 days"), ("12m", "Last 12 months"),
                  ("ytd", "This year"), ("all", "All time")]
COMMITTED_REQUEST_STATUSES = {"accepted", "delivered", "completed"}


def report_period(args):
    period = args.get("period", "12m")
    if period not in dict(REPORT_PERIODS):
        period = "12m"
    now = utcnow()
    start = {
        "30d": now - timedelta(days=30),
        "90d": now - timedelta(days=90),
        "12m": now - timedelta(days=365),
        "ytd": datetime(now.year, 1, 1),
        "all": None,
    }[period]
    return period, start


def monthly_series(pairs, start):
    """pairs: iterable of (datetime, value). Returns (labels, values) with every month in range, gaps as 0."""
    totals = defaultdict(lambda: Decimal("0"))
    for when, value in pairs:
        if when:
            totals[when.strftime("%Y-%m")] += Decimal(value)
    if not totals:
        return [], []
    first = start.strftime("%Y-%m") if start else min(totals)
    months = month_range(min(first, min(totals)), utcnow().strftime("%Y-%m"))
    return months, [float(totals.get(m, 0)) for m in months]


def top_n(counter, n=10):
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [k for k, _ in items], [float(v) for _, v in items]


def cell(text, href=None, badge=None, num=False, mono=False, money=None):
    return {"text": text, "href": href, "badge": badge, "num": num or money is not None, "mono": mono,
            "money": money}


def requests_report(start):
    query = ProductRequest.query
    if start:
        query = query.filter(ProductRequest.created_at >= start)
    reqs = query.order_by(ProductRequest.created_at.desc()).all()
    committed = [r for r in reqs if r.status in COMMITTED_REQUEST_STATUSES]
    committed_value = sum((r.total for r in committed), Decimal("0"))
    completed_value = sum((r.total for r in reqs if r.status == "completed"), Decimal("0"))
    open_count = sum(1 for r in reqs if r.status in OPEN_REQUEST_STATUSES)
    lost = sum(1 for r in reqs if r.status in {"declined", "cancelled"})

    by_supplier, by_product = defaultdict(Decimal), defaultdict(Decimal)
    for r in committed:
        by_supplier[r.supplier.company_name] += r.total
        by_product[r.product_name] += r.total
    months, monthly = monthly_series(((r.created_at, r.total) for r in committed), start)
    status_counts = [(s, sum(1 for r in reqs if r.status == s)) for s in REQUEST_STATUSES]
    status_counts = [(s, c) for s, c in status_counts if c]

    return {
        "kpis": [
            ("Requests", len(reqs), f"{open_count} open"),
            ("Committed value", format_zar(committed_value), "accepted, delivered or completed"),
            ("Completed value", format_zar(completed_value), "received and confirmed"),
            ("Average order", format_zar(committed_value / len(committed)) if committed else "—", "committed requests"),
            ("Declined / cancelled", lost, f"{round(lost / len(reqs) * 100) if reqs else 0}% of requests"),
        ],
        "charts": [
            {"id": "c1", "title": "Committed value per month", "subtitle": "Accepted, delivered and completed requests",
             "labels": months, "values": monthly, "money": True, "months": True},
            {"id": "c2", "title": "Requests by status", "subtitle": "Only statuses in use",
             "labels": [status_label(s) for s, _ in status_counts], "values": [c for _, c in status_counts], "horizontal": True},
            {"id": "c3", "title": "Top suppliers", "subtitle": "By committed value", "horizontal": True, "money": True,
             **dict(zip(("labels", "values"), top_n(by_supplier)))},
            {"id": "c4", "title": "Top products", "subtitle": "By committed value", "horizontal": True, "money": True,
             **dict(zip(("labels", "values"), top_n(by_product)))},
        ],
        "columns": ["Reference", "Date", "Product", "Supplier", "Qty", "Total", "Status", "Requested by"],
        "rows": [[
            cell(r.reference, href=url_for("admin_request_detail", request_id=r.id), mono=True),
            cell(r.created_at.strftime("%d %b %Y") if r.created_at else ""),
            cell(r.product_name), cell(r.supplier.company_name), cell(str(r.quantity), num=True),
            cell(format_zar(r.total), money=r.total), cell(status_label(r.status), badge=r.status),
            cell(r.admin.name or r.admin.email),
        ] for r in reqs],
    }


def suppliers_report(start):
    query = User.query
    if start:
        query = query.filter(User.created_at >= start)
    suppliers = query.order_by(User.created_at.desc()).all()
    apps = users_by_email_applications(suppliers)
    ids = [u.id for u in suppliers]
    product_counts = dict(DB.session.query(Product.user_id, func.count()).filter(
        Product.user_id.in_(ids), Product.is_archived.is_(False)).group_by(Product.user_id).all()) if ids else {}
    request_rows = ProductRequest.query.filter(ProductRequest.supplier_id.in_(ids)).all() if ids else []
    request_counts, committed = defaultdict(int), defaultdict(Decimal)
    for r in request_rows:
        request_counts[r.supplier_id] += 1
        if r.status in COMMITTED_REQUEST_STATUSES:
            committed[r.supplier_id] += r.total

    by_category, by_province = defaultdict(int), defaultdict(int)
    for u in suppliers:
        application = apps.get(normalize_email(u.email))
        if application:
            for c in application.category_names:
                by_category[c] += 1
            by_province[application.province or "Not given"] += 1
    months, monthly = monthly_series(((u.created_at, 1) for u in suppliers), start)
    active = sum(1 for u in suppliers if u.active)
    applied = sum(1 for u in suppliers if normalize_email(u.email) in apps)

    return {
        "kpis": [
            ("Registered", len(suppliers), "in this period"),
            ("Active", active, "approved and trading"),
            ("Inactive", len(suppliers) - active, "not yet approved or deactivated"),
            ("Applied", applied, f"{len(suppliers) - applied} registered without applying"),
            ("With inventory", sum(1 for i in ids if product_counts.get(i)), "have products listed"),
        ],
        "charts": [
            {"id": "c1", "title": "Registrations per month", "subtitle": "New supplier accounts",
             "labels": months, "values": monthly, "months": True},
            {"id": "c2", "title": "Account status", "subtitle": "Active vs inactive",
             "labels": [l for l, v in (("Active", active), ("Inactive", len(suppliers) - active)) if v],
             "values": [v for v in (active, len(suppliers) - active) if v], "horizontal": True},
            {"id": "c3", "title": "By category", "subtitle": "From supplier applications", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(by_category, 15)))},
            {"id": "c4", "title": "By province", "subtitle": "From supplier applications", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(by_province, 10)))},
        ],
        "columns": ["Supplier", "Supplier ID", "Registered", "Application", "Account", "Products", "Requests", "Committed value"],
        "rows": [[
            cell(u.company_name, href=(url_for("admin_application_detail", application_id=apps[normalize_email(u.email)].id)
                                       if normalize_email(u.email) in apps else None)),
            cell(u.supplier_id or "—", mono=True),
            cell(u.created_at.strftime("%d %b %Y") if u.created_at else ""),
            (cell(status_label(apps[normalize_email(u.email)].status), badge=apps[normalize_email(u.email)].status)
             if normalize_email(u.email) in apps else cell("Not submitted")),
            cell("Active" if u.active else "Inactive", badge="active" if u.active else "inactive"),
            cell(str(product_counts.get(u.id, 0)), num=True),
            cell(str(request_counts.get(u.id, 0)), num=True),
            cell(format_zar(committed.get(u.id, 0)), money=committed.get(u.id, Decimal("0"))),
        ] for u in suppliers],
    }


def users_by_email_applications(users):
    emails = [normalize_email(u.email) for u in users]
    if not emails:
        return {}
    return {normalize_email(a.email): a for a in SupplierApplication.query.filter(func.lower(SupplierApplication.email).in_(emails))}


def inventory_report(_start):
    products = (Product.query.join(User, Product.user_id == User.id)
                .filter(Product.is_archived.is_(False)).order_by(Product.category, Product.name).all())
    live = [p for p in products if p.supplier.active]
    listed = [p for p in live if p.is_listed]
    out_of_stock = [p for p in listed if not p.in_stock]
    low_stock = [p for p in listed if p.quantity_available is not None and 0 < p.quantity_available <= 2]
    by_category, by_supplier = defaultdict(int), defaultdict(int)
    for p in listed:
        by_category[p.category] += 1
        by_supplier[p.supplier.company_name] += 1

    def stock_text(p):
        if p.quantity_available is None:
            return "Not tracked"
        return "Out of stock" if p.quantity_available == 0 else str(p.quantity_available)

    return {
        "snapshot": True,
        "kpis": [
            ("Listed products", len(listed), "requestable in the catalogue"),
            ("Hidden", len(live) - len(listed), "in inventory, not listed"),
            ("Out of stock", len(out_of_stock), "listed but can't be requested"),
            ("Low stock", len(low_stock), "2 or fewer left"),
            ("Suppliers with products", len({p.user_id for p in listed}), f"{len(by_category)} categories covered"),
        ],
        "charts": [
            {"id": "c1", "title": "Listed products by category", "subtitle": "Active suppliers only", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(by_category, 15)))},
            {"id": "c2", "title": "Suppliers by listed products", "subtitle": "Top 10", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(by_supplier)))},
        ],
        "columns": ["Product", "Category", "Supplier", "Price", "Unit", "In stock", "Listed"],
        "rows": [[
            cell(p.name), cell(p.category), cell(p.supplier.company_name + ("" if p.supplier.active else " (inactive)")),
            cell(format_zar(p.price), money=p.price), cell(p.unit), cell(stock_text(p), num=True),
            cell("Listed" if p.is_listed and p.supplier.active else "Hidden",
                 badge="listed" if p.is_listed and p.supplier.active else "hidden"),
        ] for p in products],
    }


ACTIVITY_LABELS = {**EVENT_LABELS, "requested": "Product requested", "complete": "Request completed",
                   "cancel": "Request cancelled", "quotation": "Quotation generated"}


def activity_report(start):
    app_events = ApplicationEvent.query.filter(ApplicationEvent.actor_type == "admin")
    req_events = ProductRequestEvent.query.filter(ProductRequestEvent.actor_type == "admin")
    if start:
        app_events = app_events.filter(ApplicationEvent.created_at >= start)
        req_events = req_events.filter(ProductRequestEvent.created_at >= start)
    events = [("application", e) for e in app_events.all()] + [("request", e) for e in req_events.all()]
    events.sort(key=lambda pair: pair[1].created_at or datetime.min, reverse=True)

    def count(*actions):
        return sum(1 for _, e in events if e.action in actions)

    per_admin, per_action = defaultdict(int), defaultdict(int)
    for _, e in events:
        per_admin[e.actor_name or "Unknown"] += 1
        per_action[ACTIVITY_LABELS.get(e.action, e.action.replace("_", " ").capitalize())] += 1
    months, monthly = monthly_series(((e.created_at, 1) for _, e in events), start)
    applications = {a.id: a for a in SupplierApplication.query.filter(
        SupplierApplication.id.in_({e.application_id for kind, e in events if kind == "application"})).all()} if events else {}
    requests_by_id = {r.id: r for r in ProductRequest.query.filter(
        ProductRequest.id.in_({e.request_id for kind, e in events if kind == "request"})).all()} if events else {}

    def subject(kind, e):
        if kind == "application":
            a = applications.get(e.application_id)
            return cell(a.company_name if a else f"Application {e.application_id}",
                        href=url_for("admin_application_history", application_id=e.application_id))
        r = requests_by_id.get(e.request_id)
        return cell(r.reference if r else f"Request {e.request_id}",
                    href=url_for("admin_request_detail", request_id=e.request_id), mono=True)

    return {
        "kpis": [
            ("Admin actions", len(events), "recorded in the audit trail"),
            ("Decisions", count("status_changed"), "application status changes"),
            ("Assignments", count("assigned", "unassigned"), "assigned or unassigned"),
            ("Documents opened", count("document_viewed", "documents_downloaded"), "views and ZIP downloads"),
            ("Product requests", count("requested"), f"{count('quotation')} quotations generated"),
        ],
        "charts": [
            {"id": "c1", "title": "Actions per admin", "subtitle": "All recorded actions", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(per_admin, 15)))},
            {"id": "c2", "title": "Actions by type", "subtitle": "What admins did", "horizontal": True,
             **dict(zip(("labels", "values"), top_n(per_action, 12)))},
            {"id": "c3", "title": "Activity per month", "subtitle": "All admin actions",
             "labels": months, "values": monthly, "months": True},
        ],
        "columns": ["When (UTC)", "Admin", "Action", "Subject", "Details"],
        "rows": [[
            cell(e.created_at.strftime("%d %b %Y, %H:%M") if e.created_at else ""),
            cell(e.actor_name),
            cell(ACTIVITY_LABELS.get(e.action, e.action.replace("_", " ").capitalize())),
            subject(kind, e),
            cell((getattr(e, "summary", None) or getattr(e, "note", "") or "")[:140]),
        ] for kind, e in events[:500]],
    }


REPORT_BUILDERS = {"requests": requests_report, "suppliers": suppliers_report,
                   "inventory": inventory_report, "activity": activity_report}
REPORT_DESCRIPTIONS = {
    "requests": "Spend and fulfilment of product requests to suppliers",
    "suppliers": "Supplier registrations, status and activity",
    "inventory": "What approved suppliers currently offer — a live snapshot",
    "activity": "What each admin has done, from the audit trail",
}


@app.route("/admin/reports/<kind>")
@admin_required
def admin_report(kind):
    if kind == "applications":
        return redirect(url_for("admin_reports", **request.args))
    if kind not in REPORT_BUILDERS:
        abort(404)
    period, start = report_period(request.args)
    report = REPORT_BUILDERS[kind](start)
    return render_template(
        "admin_report.html", admin=get_current_admin(), kind=kind, report=report,
        title=dict(REPORT_KINDS)[kind], description=REPORT_DESCRIPTIONS[kind],
        period=period, periods=REPORT_PERIODS, report_kinds=REPORT_KINDS,
    )


@app.route("/admin/reports/<kind>/export")
@admin_required
def admin_report_export(kind):
    if kind not in REPORT_BUILDERS:
        abort(404)
    period, start = report_period(request.args)
    report = REPORT_BUILDERS[kind](start)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(report["columns"])
    for row in report["rows"]:
        writer.writerow([f"{c['money']:.2f}" if c.get("money") is not None else c["text"] for c in row])
    filename = f"{kind}_report_{period}_{datetime.now().strftime('%Y%m%d')}.csv"
    return Response("﻿" + output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/reports/export")
@admin_required
def admin_reports_export():
    applications = filtered_applications_query(report_filters(request.args)).order_by(SupplierApplication.created_at.asc()).all()
    suppliers = users_by_email(applications)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Supplier ID", "Company", "Contact Name", "Email", "Phone", "Categories", "Province", "City",
        "Status", "Documents Status", "Supplier Active", "Created At", "Review Comments",
    ])
    for application in applications:
        supplier = suppliers.get(normalize_email(application.email))
        writer.writerow([
            application.id,
            supplier.supplier_id if supplier else "",
            application.company_name,
            application.contact_name,
            application.email,
            application.phone,
            "; ".join(application.category_names) or "Uncategorized",
            application.province,
            application.city,
            status_label(application.status),
            application.documents_status,
            "Yes" if supplier and supplier.active else "No",
            application.created_at.strftime("%Y-%m-%d %H:%M") if application.created_at else "",
            application.review_comments or "",
        ])

    filename = f"admin_reports_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    # BOM so Excel opens UTF-8 (e.g. "Décor") correctly.
    return Response(
        "﻿" + output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


# ---------------------------------------------------------------------------
# Inventory (approved suppliers) and product requests (admins)
# ---------------------------------------------------------------------------

def active_supplier_required(view_func):
    """Inventory and requests are only for suppliers whose account is active (application approved)."""
    @wraps(view_func)
    @login_required
    def wrapper(*args, **kwargs):
        if not get_current_user().active:
            flash("Your inventory becomes available once your supplier application is approved and your account is active.", "error")
            return redirect(url_for("dashboard"))
        return view_func(*args, **kwargs)
    return wrapper


def parse_product_form(form):
    values = read_fields(form, ["name", "category", "description", "unit", "price", "quantity_available", "lead_time_days"])
    if not values["name"]:
        return None, "Please enter a product or service name."
    if values["category"] not in CATEGORY_VALUES:
        return None, "Please choose a category."
    if values["unit"] not in PRODUCT_UNITS:
        return None, "Please choose a unit."
    try:
        price = Decimal(values["price"].replace(" ", "").replace(",", ".").lstrip("Rr") or "x").quantize(Decimal("0.01"))
        if price < 0 or price > Decimal("9999999999"):
            raise InvalidOperation
    except InvalidOperation:
        return None, "Please enter a valid price, e.g. 1500.00."
    numbers = {}
    for field, label in (("quantity_available", "Quantity in stock"), ("lead_time_days", "Lead time")):
        raw = values[field]
        if raw == "":
            numbers[field] = None
            continue
        if not raw.isdigit() or int(raw) > 1_000_000:
            return None, f"{label} must be a whole number of 0 or more (leave it blank if not applicable)."
        numbers[field] = int(raw)
    return {
        "name": values["name"][:150], "category": values["category"], "description": values["description"][:5000],
        "unit": values["unit"], "price": price, **numbers,
    }, None


def log_request_event(product_request, action, note="", from_status=None, to_status=None, actor=None):
    actor = actor or (get_current_admin() if g.get("is_admin_view") else get_current_user())
    if isinstance(actor, Admin):
        actor_type, name = "admin", actor.name or actor.email
    elif isinstance(actor, User):
        actor_type, name = "supplier", actor.contact_name or actor.company_name
    else:
        actor_type, name = "system", "System"
    DB.session.add(ProductRequestEvent(
        request_id=product_request.id, actor_type=actor_type, actor_name=(name or "")[:150],
        action=action, from_status=from_status, to_status=to_status, note=(note or "")[:2000],
    ))


def notify_request_admin(product_request, title, message):
    admin = product_request.admin
    if not admin:
        return
    DB.session.add(Notification(admin_id=admin.id, title=title, message=message))
    send_notification_email(admin.email, title, message, recipient_name=admin.name or "Admin",
                            action_url=url_for("admin_request_detail", request_id=product_request.id, _external=True),
                            action_label="View request")


# ---- Supplier: inventory ----

@app.route("/inventory")
@active_supplier_required
def inventory():
    user = get_current_user()
    search = request.args.get("search", "").strip()
    show = request.args.get("show", "").strip()
    query = Product.query.filter_by(user_id=user.id, is_archived=False)
    if search:
        query = query.filter(Product.name.ilike(f"%{search}%"))
    if show == "listed":
        query = query.filter(Product.is_listed.is_(True))
    elif show == "hidden":
        query = query.filter(Product.is_listed.is_(False))
    products = query.order_by(Product.name).all()
    all_products = Product.query.filter_by(user_id=user.id, is_archived=False).all()
    return render_template(
        "inventory.html", user=user, products=products, search=search, show=show,
        listed_count=sum(1 for p in all_products if p.is_listed), total_count=len(all_products),
        out_of_stock_count=sum(1 for p in all_products if not p.in_stock),
    )


def default_product_category(user):
    application = get_user_application(user)
    return application.supplier_category if application else ""


@app.route("/inventory/new", methods=["GET", "POST"])
@app.route("/inventory/<int:product_id>/edit", methods=["GET", "POST"])
@active_supplier_required
def product_form(product_id=None):
    user = get_current_user()
    product = None
    if product_id is not None:
        product = Product.query.filter_by(id=product_id, user_id=user.id, is_archived=False).first_or_404()

    if request.method == "POST":
        values, error = parse_product_form(request.form)
        if error:
            flash(error, "error")
            return render_template("product_form.html", user=user, product=product, form=request.form), 400
        if product is None:
            product = Product(user_id=user.id, is_listed=bool(request.form.get("is_listed")))
            DB.session.add(product)
        else:
            product.is_listed = bool(request.form.get("is_listed"))
        for field, value in values.items():
            setattr(product, field, value)
        DB.session.commit()
        flash(f"“{product.name}” saved.", "success")
        return redirect(url_for("inventory"))

    form = {} if product is None else {
        "name": product.name, "category": product.category, "description": product.description, "unit": product.unit,
        "price": f"{product.price:.2f}", "quantity_available": "" if product.quantity_available is None else product.quantity_available,
        "lead_time_days": "" if product.lead_time_days is None else product.lead_time_days, "is_listed": product.is_listed,
    }
    if product is None:
        form = {"category": default_product_category(user), "unit": "each", "is_listed": True}
    return render_template("product_form.html", user=user, product=product, form=form)


@app.route("/inventory/<int:product_id>/toggle", methods=["POST"])
@active_supplier_required
def toggle_product(product_id):
    product = Product.query.filter_by(id=product_id, user_id=get_current_user().id, is_archived=False).first_or_404()
    product.is_listed = not product.is_listed
    DB.session.commit()
    flash(f"“{product.name}” is now {'listed in the catalogue' if product.is_listed else 'hidden from the catalogue'}.", "success")
    return redirect(url_for("inventory"))


@app.route("/inventory/<int:product_id>/delete", methods=["POST"])
@active_supplier_required
def delete_product(product_id):
    product = Product.query.filter_by(id=product_id, user_id=get_current_user().id, is_archived=False).first_or_404()
    if ProductRequest.query.filter_by(product_id=product.id).first():
        # Keep it for the request history, but remove it from the inventory and catalogue.
        product.is_archived = True
        product.is_listed = False
    else:
        DB.session.delete(product)
    DB.session.commit()
    flash(f"“{product.name}” removed from your inventory.", "success")
    return redirect(url_for("inventory"))


# ---- Supplier: requests received ----

@app.route("/requests")
@active_supplier_required
def supplier_requests():
    user = get_current_user()
    status = request.args.get("status", "").strip()
    base = ProductRequest.query.filter_by(supplier_id=user.id)
    query = base
    if status in REQUEST_STATUSES:
        query = query.filter(ProductRequest.status == status)
    else:
        status = ""
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(ProductRequest.created_at.desc()).paginate(page=page, per_page=15, error_out=False)
    return render_template("supplier_requests.html", user=user, requests=pagination.items, pagination=pagination,
                           status_filter=status, status_cards=request_status_cards(base))


@app.route("/requests/<int:request_id>")
@active_supplier_required
def supplier_request_detail(request_id):
    user = get_current_user()
    product_request = ProductRequest.query.filter_by(id=request_id, supplier_id=user.id).first_or_404()
    return render_template("supplier_request_detail.html", user=user, req=product_request)


SUPPLIER_TRANSITIONS = {"accept": ("requested", "accepted"), "decline": ("requested", "declined"), "deliver": ("accepted", "delivered")}


@app.route("/requests/<int:request_id>/respond", methods=["POST"])
@active_supplier_required
def respond_to_request(request_id):
    user = get_current_user()
    product_request = ProductRequest.query.filter_by(id=request_id, supplier_id=user.id).first_or_404()
    detail_url = url_for("supplier_request_detail", request_id=product_request.id)
    action = request.form.get("action", "")
    note = request.form.get("note", "").strip()

    if action not in SUPPLIER_TRANSITIONS or product_request.status != SUPPLIER_TRANSITIONS[action][0]:
        flash("That action isn't available for this request any more.", "error")
        return redirect(detail_url)
    if action == "decline" and not note:
        flash("Please tell Icebolethu Group why you're declining this request.", "error")
        return redirect(detail_url)

    product = product_request.product
    if action == "accept" and product.quantity_available is not None:
        if product.quantity_available < product_request.quantity:
            flash(f"You only have {product.quantity_available} in stock. Update your inventory before accepting.", "error")
            return redirect(detail_url)
        product.quantity_available -= product_request.quantity

    old, new = SUPPLIER_TRANSITIONS[action]
    product_request.status = new
    if note:
        product_request.supplier_note = note
    log_request_event(product_request, action, note=note, from_status=old, to_status=new)
    verb = {"accept": "accepted", "decline": "declined", "deliver": "marked as delivered"}[action]
    notify_request_admin(product_request, f"Request {product_request.reference} {verb}",
                         f"{user.company_name} {verb} your request for {product_request.quantity} × {product_request.product_name}."
                         + (f" Note: {note}" if note else ""))
    DB.session.commit()
    flash(f"Request {product_request.reference} {verb}.", "success")
    return redirect(detail_url)


# ---- Admin: catalogue and requests ----

def catalogue_query():
    return (Product.query.join(User, Product.user_id == User.id)
            .filter(Product.is_listed.is_(True), Product.is_archived.is_(False), User.active.is_(True)))


@app.route("/admin/catalogue")
@admin_required
def admin_catalogue():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    supplier_id = request.args.get("supplier", type=int)
    query = catalogue_query()
    if search:
        like = f"%{search}%"
        query = query.filter(DB.or_(Product.name.ilike(like), Product.description.ilike(like), User.company_name.ilike(like)))
    if category:
        query = query.filter(Product.category == category)
    if supplier_id:
        query = query.filter(Product.user_id == supplier_id)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Product.name).paginate(page=page, per_page=24, error_out=False)

    base = catalogue_query()
    categories = sorted({c for (c,) in base.with_entities(Product.category).distinct() if c})
    suppliers = (User.query.filter(User.id.in_(base.with_entities(Product.user_id).distinct()))
                 .order_by(User.company_name).all())
    page_args = {k: v for k, v in {"search": search, "category": category, "supplier": supplier_id}.items() if v}
    return render_template(
        "admin_catalogue.html", admin=get_current_admin(), products=pagination.items, pagination=pagination,
        search=search, category_filter=category, supplier_filter=supplier_id, categories=categories,
        suppliers=suppliers, page_args=page_args,
    )


@app.route("/admin/catalogue/<int:product_id>/request", methods=["GET", "POST"])
@admin_required
def admin_request_product(product_id):
    admin = get_current_admin()
    product = catalogue_query().filter(Product.id == product_id).first()
    if not product:
        flash("That product is no longer available in the catalogue.", "error")
        return redirect(url_for("admin_catalogue"))

    if request.method == "POST":
        form = read_fields(request.form, ["quantity", "required_by", "delivery_location", "notes"])
        error = None
        quantity = int(form["quantity"]) if form["quantity"].isdigit() else 0
        if quantity < 1:
            error = "Please enter a quantity of at least 1."
        elif product.quantity_available is not None and quantity > product.quantity_available:
            error = f"{product.supplier.company_name} only has {product.quantity_available} available."
        required_by = None
        if not error and form["required_by"]:
            try:
                required_by = datetime.strptime(form["required_by"], "%Y-%m-%d").date()
                if required_by < date.today():
                    error = "The required-by date can't be in the past."
            except ValueError:
                error = "Please enter a valid required-by date."
        if not error and not form["delivery_location"]:
            error = "Please enter a delivery location."
        if error:
            flash(error, "error")
            return render_template("admin_request_new.html", admin=admin, product=product, form=request.form), 400

        product_request = ProductRequest(
            product_id=product.id, supplier_id=product.user_id, admin_id=admin.id,
            product_name=product.name, unit=product.unit, unit_price=product.price, quantity=quantity,
            required_by=required_by, delivery_location=form["delivery_location"][:255], notes=form["notes"][:5000],
        )
        DB.session.add(product_request)
        DB.session.flush()
        log_request_event(product_request, "requested", note=form["notes"], to_status="requested")
        supplier = product.supplier
        DB.session.add(Notification(
            user_id=supplier.id, title=f"New product request {product_request.reference}",
            message=f"Icebolethu Group requested {quantity} × {product.name}"
                    + (f", needed by {required_by.strftime('%d %b %Y')}" if required_by else "") + ".",
        ))
        send_notification_email(
            supplier.email, f"New product request {product_request.reference}",
            f"Icebolethu Group has requested {quantity} × {product.name} ({format_zar(product_request.total)}). "
            "Please accept or decline it in the supplier portal.",
            recipient_name=supplier.contact_name or supplier.company_name,
            action_url=url_for("supplier_request_detail", request_id=product_request.id, _external=True),
            action_label="Respond to request",
        )
        DB.session.commit()
        flash(f"Request {product_request.reference} sent to {supplier.company_name}.", "success")
        return redirect(url_for("admin_request_detail", request_id=product_request.id))

    return render_template("admin_request_new.html", admin=admin, product=product, form={"quantity": 1})


@app.route("/admin/requests")
@admin_required
def admin_requests():
    admin = get_current_admin()
    status = request.args.get("status", "").strip()
    if status not in REQUEST_STATUSES:
        status = ""
    mine = request.args.get("mine") == "1"

    def by_requester(query, only_mine):
        return query.filter(ProductRequest.admin_id == admin.id) if only_mine else query

    def by_status(query, value):
        return query.filter(ProductRequest.status == value) if value else query

    # Slicer cards: each count is what clicking the card would show, given the other slicer.
    status_cards = request_status_cards(by_requester(ProductRequest.query, mine))
    in_status = by_status(ProductRequest.query, status)
    requester_cards = [("", "Everyone's", by_requester(in_status, False).count()),
                       ("1", "Made by me", by_requester(in_status, True).count())]

    query = by_status(by_requester(ProductRequest.query, mine), status)
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(ProductRequest.created_at.desc()).paginate(page=page, per_page=15, error_out=False)
    page_args = {k: v for k, v in {"status": status, "mine": "1" if mine else ""}.items() if v}
    return render_template("admin_requests.html", admin=admin, requests=pagination.items, pagination=pagination,
                           status_filter=status, mine=mine, page_args=page_args,
                           status_cards=status_cards, requester_cards=requester_cards)


def request_status_cards(query):
    """[(status or '', label, count, total value)] for the request slicer cards, 'All' first."""
    value = func.coalesce(func.sum(ProductRequest.unit_price * ProductRequest.quantity), 0)
    rows = {st: (n, Decimal(str(v or 0))) for st, n, v in query.with_entities(
        ProductRequest.status, func.count(ProductRequest.id), value).group_by(ProductRequest.status).all()}
    cards = [("", "All requests", sum(n for n, _ in rows.values()), sum((v for _, v in rows.values()), Decimal("0")))]
    cards += [(st, status_label(st), *rows.get(st, (0, Decimal("0")))) for st in REQUEST_STATUSES]
    return cards


@app.route("/admin/requests/<int:request_id>")
@admin_required
def admin_request_detail(request_id):
    return render_template("admin_request_detail.html", admin=get_current_admin(),
                           req=DB.get_or_404(ProductRequest, request_id))


# A quotation is only issued once the supplier has accepted (and so confirmed price and availability).
QUOTATION_READY_STATUSES = {"accepted", "delivered", "completed"}


def quotation_context(product_request):
    """Everything the printable page and the PDF need, computed once so both always agree."""
    supplier = product_request.supplier
    application = get_user_application(supplier) if supplier else None
    total = product_request.total
    vat_registered = bool(application and (application.vat_number or "").strip())
    # Supplier prices are treated as VAT-inclusive; show the VAT portion when the supplier is VAT registered.
    vat_amount = (total * VAT_RATE / (100 + VAT_RATE)).quantize(Decimal("0.01")) if vat_registered else Decimal("0.00")
    return {
        "req": product_request,
        "supplier": supplier,
        "application": application,
        "company": COMPANY_DETAILS,
        "quote_number": f"QT-{product_request.id:05d}",
        "issued_on": utcnow(),
        "total": total,
        "vat_registered": vat_registered,
        "vat_rate": VAT_RATE,
        "vat_amount": vat_amount,
        "total_excl_vat": total - vat_amount,
        "accepted_on": next((e.created_at for e in product_request.events if e.action == "accept"), None),
    }


def quotation_request_or_redirect(request_id):
    product_request = DB.get_or_404(ProductRequest, request_id)
    if product_request.status not in QUOTATION_READY_STATUSES:
        flash("The quotation becomes available once the supplier has accepted the request.", "error")
        return product_request, redirect(url_for("admin_request_detail", request_id=product_request.id))
    return product_request, None


@app.route("/admin/requests/<int:request_id>/quotation")
@admin_required
def admin_request_quotation(request_id):
    product_request, blocked = quotation_request_or_redirect(request_id)
    if blocked:
        return blocked
    context = quotation_context(product_request)
    log_request_event(product_request, "quotation", note="Quotation opened for printing")
    DB.session.commit()
    return render_template("admin_quotation.html", admin=get_current_admin(), **context)


@app.route("/admin/requests/<int:request_id>/quotation.pdf")
@admin_required
def admin_request_quotation_pdf(request_id):
    from quotation_pdf import build_quotation_pdf  # imported lazily so the app still runs without reportlab

    product_request, blocked = quotation_request_or_redirect(request_id)
    if blocked:
        return blocked
    admin = get_current_admin()
    context = quotation_context(product_request)
    pdf_bytes = build_quotation_pdf(context, generated_by=admin.name or admin.email,
                                    logo_path=os.path.join(STATIC_DIR, "images", "logo.png"))
    log_request_event(product_request, "quotation", note="Quotation downloaded as PDF")
    DB.session.commit()
    supplier_part = secure_filename(product_request.supplier.company_name or "supplier") or "supplier"
    return send_file(io.BytesIO(pdf_bytes), mimetype="application/pdf", as_attachment=True,
                     download_name=f"{context['quote_number']}_{supplier_part}.pdf")


ADMIN_TRANSITIONS = {"complete": ({"delivered"}, "completed"), "cancel": ({"requested", "accepted"}, "cancelled")}


@app.route("/admin/requests/<int:request_id>/update", methods=["POST"])
@admin_required
def admin_update_request(request_id):
    product_request = DB.get_or_404(ProductRequest, request_id)
    detail_url = url_for("admin_request_detail", request_id=product_request.id)
    action = request.form.get("action", "")
    note = request.form.get("note", "").strip()
    if action not in ADMIN_TRANSITIONS or product_request.status not in ADMIN_TRANSITIONS[action][0]:
        flash("That action isn't available for this request any more.", "error")
        return redirect(detail_url)
    if action == "cancel" and not note:
        flash("Please give the supplier a reason for cancelling.", "error")
        return redirect(detail_url)

    old, new = product_request.status, ADMIN_TRANSITIONS[action][1]
    if action == "cancel" and old == "accepted" and product_request.product.quantity_available is not None:
        product_request.product.quantity_available += product_request.quantity  # return reserved stock
    product_request.status = new
    log_request_event(product_request, action, note=note, from_status=old, to_status=new)
    verb = "completed" if action == "complete" else "cancelled"
    supplier = product_request.supplier
    message = f"Icebolethu Group {verb} request {product_request.reference} ({product_request.quantity} × {product_request.product_name})." + (f" Note: {note}" if note else "")
    DB.session.add(Notification(user_id=supplier.id, title=f"Request {product_request.reference} {verb}", message=message))
    send_notification_email(supplier.email, f"Request {product_request.reference} {verb}", message,
                            recipient_name=supplier.contact_name or supplier.company_name,
                            action_url=url_for("supplier_request_detail", request_id=product_request.id, _external=True),
                            action_label="View request")
    DB.session.commit()
    flash(f"Request {product_request.reference} {verb}.", "success")
    return redirect(detail_url)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

@app.errorhandler(413)
def file_too_large(_error):
    flash(f"That upload is too large. The maximum size is {app.config['MAX_CONTENT_LENGTH'] // (1024 * 1024)} MB.", "error")
    return redirect(safe_next_url(urlparse(request.referrer or "").path, url_for("home")))


@app.errorhandler(HTTPException)
def http_error(error):
    if request.path.startswith(("/health", "/db-status")):
        return jsonify({"error": error.name}), error.code
    return render_template("error.html", code=error.code, title=error.name, description=error.description), error.code


@app.errorhandler(OperationalError)
def database_unavailable(error):
    DB.session.rollback()
    app.logger.error("Database unavailable: %s", error.orig if getattr(error, "orig", None) else error)
    return render_template(
        "error.html", code=503, title="Service temporarily unavailable",
        description="We can't reach the database right now. Please try again in a few minutes.",
    ), 503


@app.errorhandler(Exception)
def unhandled_error(error):
    DB.session.rollback()
    app.logger.exception("Unhandled error: %s", error)
    return render_template(
        "error.html", code=500, title="Something went wrong",
        description="An unexpected error occurred. Please try again, and contact us if it keeps happening.",
    ), 500


# ---------------------------------------------------------------------------
# Database setup / lightweight migrations (MySQL)
# ---------------------------------------------------------------------------

def _columns(table):
    return {row[0].lower(): row for row in DB.session.execute(text(f"SHOW COLUMNS FROM {table}")).fetchall()}


def _add_missing_columns(table, definitions):
    existing = _columns(table)
    for column, ddl in definitions.items():
        if column not in existing:
            DB.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
            app.logger.info("Added %s.%s", table, column)


def migrate_mysql_schema():
    """Bring databases created by older versions of the app up to the current model."""
    users = _columns("users")
    if "active" in users and "email_verified" not in users:
        # Very old schema used `active` to mean "email verified".
        DB.session.execute(text("ALTER TABLE users CHANGE COLUMN active email_verified BOOLEAN NOT NULL DEFAULT FALSE"))
    _add_missing_columns("users", {
        "name": "VARCHAR(100) NOT NULL DEFAULT ''",
        "password_hash": "VARCHAR(256) NOT NULL DEFAULT ''",
        "company_name": "VARCHAR(150) NOT NULL DEFAULT ''",
        "contact_name": "VARCHAR(120) NOT NULL DEFAULT ''",
        "phone": "VARCHAR(50) NOT NULL DEFAULT ''",
        "address": "VARCHAR(255) NOT NULL DEFAULT ''",
        "created_at": "DATETIME NULL",
        "email_verified": "BOOLEAN NOT NULL DEFAULT FALSE",
        "active": "BOOLEAN NOT NULL DEFAULT FALSE",
        "supplier_id": "VARCHAR(20) NULL UNIQUE",
        "verification_code_hash": "VARCHAR(256) NULL",
        "verification_code_expires": "DATETIME NULL",
    })
    name_col = _columns("users").get("name")
    if name_col is not None and name_col[4] is None:
        DB.session.execute(text("ALTER TABLE users MODIFY COLUMN name VARCHAR(100) NOT NULL DEFAULT ''"))

    _add_missing_columns("supplier_applications", {
        "assigned_admin_id": "INT NULL",
        "assigned_at": "DATETIME NULL",
        "supplier_category_detail": "VARCHAR(255) NOT NULL DEFAULT ''",
        **{field: f"VARCHAR({SupplierApplication.__table__.c[field].type.length}) DEFAULT ''" for field in PROFILE_FIELDS},
    })

    notifications = _columns("notifications")
    if "admin_id" not in notifications:
        DB.session.execute(text("ALTER TABLE notifications ADD COLUMN admin_id INT NULL"))
        DB.session.execute(text("ALTER TABLE notifications ADD FOREIGN KEY (admin_id) REFERENCES admins(id)"))
    if "user_id" in notifications and notifications["user_id"][2] == "NO":
        DB.session.execute(text("ALTER TABLE notifications MODIFY COLUMN user_id INT NULL"))

    DB.session.commit()


def migrate_legacy_categories():
    """Move applications from old category names onto the merged categories. Safe to re-run."""
    legacy = list(LEGACY_CATEGORY_MAP)
    changed = SupplierApplication.query.filter(SupplierApplication.supplier_category.in_(legacy)).update(
        {SupplierApplication.supplier_category: case(LEGACY_CATEGORY_MAP, value=SupplierApplication.supplier_category)},
        synchronize_session=False,
    )
    rows = SupplierApplicationCategory.query.filter(SupplierApplicationCategory.category.in_(legacy)).all()
    for row in rows:
        merged = LEGACY_CATEGORY_MAP[row.category]
        duplicate = SupplierApplicationCategory.query.filter(
            SupplierApplicationCategory.application_id == row.application_id,
            SupplierApplicationCategory.category == merged,
            SupplierApplicationCategory.id != row.id,
        ).first()
        if duplicate:
            # e.g. an application that had both Hearse and Family Car: keep one row, combine details.
            details = [d for d in (duplicate.category_detail, row.category_detail) if d]
            duplicate.category_detail = "; ".join(dict.fromkeys(details))[:255]
            DB.session.delete(row)
        else:
            row.category = merged
    products = Product.query.filter(Product.category.in_(legacy)).update(
        {Product.category: case(LEGACY_CATEGORY_MAP, value=Product.category)}, synchronize_session=False)
    if changed or rows or products:
        DB.session.commit()
        app.logger.info("Moved %s application(s), %s category row(s) and %s product(s) to merged categories.",
                        changed, len(rows), products)


def init_database():
    with app.app_context():
        try:
            DB.create_all()
            if DB.engine.dialect.name == "mysql":
                migrate_mysql_schema()
            migrate_legacy_categories()
        except Exception as exc:  # noqa: BLE001 — let the app boot so /health still answers
            DB.session.rollback()
            app.logger.warning("Database initialization skipped: %s", exc)


# ---------------------------------------------------------------------------
# CLI commands:  flask --app app <command>
# ---------------------------------------------------------------------------

@app.cli.command("init-db")
def init_db_command():
    """Create tables and apply schema upgrades."""
    init_database()
    click.echo("Database initialised.")


@app.cli.command("create-admin")
@click.option("--email", prompt=True)
@click.option("--name", prompt=True, default="")
@click.password_option()
def create_admin_command(email, name, password):
    """Create an admin account, or reset the password of an existing one."""
    email = normalize_email(email)
    admin = find_admin_by_email(email)
    if admin:
        admin.set_password(password)
        if name:
            admin.name = name
        click.echo(f"Updated admin {email}.")
    else:
        admin = Admin(email=email, name=name)
        admin.set_password(password)
        DB.session.add(admin)
        click.echo(f"Created admin {email}.")
    DB.session.commit()


@app.cli.command("release-application")
@click.argument("application_id", type=int)
@click.option("--reason", prompt=True, help="Why the assignment is being released (recorded in the audit trail).")
def release_application_command(application_id, reason):
    """Release an application's assignment, e.g. when the assigned admin has left. Recorded as a system event."""
    application = DB.session.get(SupplierApplication, application_id)
    if not application:
        raise click.ClickException(f"No application with id {application_id}.")
    owner = application.assigned_admin
    if not owner:
        click.echo("That application isn't assigned to anyone.")
        return
    application.assigned_admin_id = None
    application.assigned_at = None
    with app.test_request_context():
        log_event(application, "unassigned", summary=f"Released from {owner.name or owner.email} by system administrator",
                  changes=[{"field": "Assigned to", "old": owner.name or owner.email, "new": ""},
                           {"field": "Reason", "old": "", "new": reason}], actor=False)
    DB.session.commit()
    click.echo(f"Released application {application_id} from {owner.email}.")


DEMO_EMAIL_DOMAIN = "demo-supplier.example"  # reserved .example TLD: can never receive real email

DEMO_COMPANIES = [
    ("Ubuntu Caskets", "Caskets and Tombstones"), ("Sizwe Funeral Catering", "Catering"), ("Eternal Rest Cold Rooms", "Body Storage, Cold-Room and other Burial Services"),
    ("Masakhane Tents & Décor", "Tents, Draping and Décor"), ("Thembeka Floral Tributes", "Flowers, crosses and plaques"), ("Khanyisa Hearse Hire", "Funeral Vehicles (Hearse / Family Car)"),
    ("Imbali Tombstones", "Caskets and Tombstones"), ("Siyabonga Livestock", "Livestock"), ("Clean-Go Mobile Toilets", "Mobile toilet"),
    ("Dignity Lowering Systems", "Lowering Device"), ("Zenzele Consulting", "Consulting"), ("Lethabo AV & Media", "ICT Equipment, Media and Electronic Devices"),
    ("Royal Oak Coffins", "Caskets and Tombstones"), ("Mama Thandi's Kitchen", "Catering"), ("Peaceful Journey Transport", "Funeral Vehicles (Hearse / Family Car)"),
    ("Marquee Masters SA", "Tents, Draping and Décor"), ("Granite & Memorial Works", "Caskets and Tombstones"), ("Golden Petals Florists", "Flowers, crosses and plaques"),
    ("Nkosi Cattle Traders", "Livestock"), ("Sanitech Event Hire", "Mobile toilet"), ("Heritage Draping Co", "Tents, Draping and Décor"),
    ("Amandla Catering Services", "Catering"), ("Serenity Body Storage", "Body Storage, Cold-Room and other Burial Services"), ("Vukani Funeral Consultants", "Consulting"),
    ("Kwanele Casket Makers", "Caskets and Tombstones"), ("Phila Sound & Screens", "ICT Equipment, Media and Electronic Devices"), ("Bayede Luxury Cars", "Funeral Vehicles (Hearse / Family Car)"),
    ("Ithemba Memorial Stones", "Caskets and Tombstones"), ("Siphesihle Event Tents", "Tents, Draping and Décor"), ("Mzansi Lowering Devices", "Lowering Device"),
]
DEMO_PEOPLE = ["Thandiwe Mkhize", "Sipho Ndlovu", "Lerato Mokoena", "Bongani Zulu", "Nomvula Dlamini", "Kagiso Molefe",
               "Zanele Khumalo", "Themba Nkosi", "Palesa Mahlangu", "Mandla Sithole", "Ayanda Cele", "Refilwe Masilo",
               "Lindiwe Ngcobo", "Tshepo Mabaso", "Nokuthula Shabalala", "Sibusiso Mthembu", "Precious Baloyi", "Vusi Mathebula",
               "Busisiwe Nxumalo", "Karabo Letsoalo", "Mpho Radebe", "Ntombi Gumede", "Andile Hadebe", "Kgomotso Moloi",
               "Siyabonga Buthelezi", "Naledi Phiri", "Lwazi Mkhabela", "Dineo Sebola", "Xolani Zwane", "Hlengiwe Mbatha"]
DEMO_CITIES = {"KwaZulu-Natal": ["Durban", "Pietermaritzburg", "Richards Bay"], "Gauteng": ["Johannesburg", "Pretoria", "Soweto"],
               "Eastern Cape": ["Gqeberha", "East London", "Mthatha"], "Western Cape": ["Cape Town", "Paarl"],
               "Limpopo": ["Polokwane", "Thohoyandou"], "Mpumalanga": ["Mbombela", "eMalahleni"], "Free State": ["Bloemfontein"],
               "North West": ["Rustenburg", "Mahikeng"], "Northern Cape": ["Kimberley"]}
DEMO_PRODUCTS = {
    "Caskets and Tombstones": [("Pine casket, standard", 4500, "each"), ("Oak casket, premium", 12800, "each"), ("Child casket, white", 2900, "each"), ("Casket with viewing glass", 7600, "each"),
                               ("Granite headstone, single", 9800, "each"), ("Granite headstone, double", 16500, "each"), ("Marble book memorial", 7200, "each")],
    "Catering": [("Funeral catering, 100 guests", 18500, "per event"), ("Funeral catering, 250 guests", 39000, "per event"), ("Tea & refreshments, 50 guests", 3200, "per event")],
    "Body Storage, Cold-Room and other Burial Services": [("Cold-room storage", 450, "per day"), ("Body transport (local)", 1800, "per service"), ("Mortuary preparation", 2500, "per service")],
    "Tents, Draping and Décor": [("Marquee tent 10x20m", 6200, "per event"), ("Stretch tent 15x20m", 8900, "per event"), ("White draping & décor set", 2400, "per event"), ("Chairs with covers (100)", 1500, "per event")],
    "Flowers, crosses and plaques": [("Casket spray, roses", 1450, "each"), ("Wreath, mixed flowers", 850, "each"), ("Engraved brass plaque", 650, "each"), ("Wooden cross", 480, "each")],
    "Funeral Vehicles (Hearse / Family Car)": [("Hearse with driver", 3500, "per day"), ("Family car (7-seater)", 2200, "per day"), ("Luxury family car", 3800, "per day")],
    "Livestock": [("Cow (ceremonial)", 14000, "each"), ("Goat", 2200, "each"), ("Sheep", 2600, "each")],
    "Mobile toilet": [("VIP mobile toilet", 950, "per day"), ("Standard mobile toilet", 550, "per day"), ("Hand-wash station", 350, "per day")],
    "Lowering Device": [("Lowering device hire", 1200, "per service"), ("Grave dressing set", 900, "per service")],
    "Consulting": [("Funeral planning consult", 1500, "per hour"), ("Cultural ceremony advisory", 2800, "per service")],
    "ICT Equipment, Media and Electronic Devices": [("PA sound system", 2500, "per event"), ("LED screen & live stream", 6500, "per event"), ("Photography & video", 4200, "per event")],
}
DEMO_COMMENTS = {
    "approved": ["All documents verified. Welcome to the Icebolethu supplier panel.", "Approved: compliance documents in order."],
    "declined": ["B-BBEE certificate has expired and the bank letter is older than 3 months.", "Company registration details do not match CIPC records."],
    "returned_for_update": ["Please upload a bank confirmation letter that is not older than 3 months.", "Tax number does not match the SARS certificate. Please correct and resubmit."],
}


def demo_sa_id(rng, birth_year):
    """A syntactically valid (but fictitious) South African ID number."""
    base = f"{birth_year % 100:02d}{rng.randint(1, 12):02d}{rng.randint(1, 28):02d}{rng.randint(0, 9999):04d}08"
    return next(base + str(d) for d in range(10) if luhn_valid(base + str(d)))


DEMO_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            b"4 0 obj<</Length 66>>stream\nBT /F1 20 Tf 72 760 Td (DEMO DOCUMENT - not a real record) Tj ET\nendstream endobj\n"
            b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def remove_demo_data():
    demo_users = User.query.filter(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}")).all()
    user_ids = [u.id for u in demo_users]
    if not user_ids:
        return 0
    app_ids = [a.id for a in SupplierApplication.query.filter(SupplierApplication.email.like(f"%@{DEMO_EMAIL_DOMAIN}"))]
    request_ids = [r.id for r in ProductRequest.query.filter(ProductRequest.supplier_id.in_(user_ids))]
    if request_ids:
        ProductRequestEvent.query.filter(ProductRequestEvent.request_id.in_(request_ids)).delete(synchronize_session=False)
        ProductRequest.query.filter(ProductRequest.id.in_(request_ids)).delete(synchronize_session=False)
    Product.query.filter(Product.user_id.in_(user_ids)).delete(synchronize_session=False)
    if app_ids:
        Notification.query.filter(Notification.related_application_id.in_(app_ids)).delete(synchronize_session=False)
        ApplicationEvent.query.filter(ApplicationEvent.application_id.in_(app_ids)).delete(synchronize_session=False)
        SupplierApplicationCategory.query.filter(SupplierApplicationCategory.application_id.in_(app_ids)).delete(synchronize_session=False)
        SupplierDirector.query.filter(SupplierDirector.application_id.in_(app_ids)).delete(synchronize_session=False)
    for doc in SupplierDocument.query.filter(SupplierDocument.user_id.in_(user_ids)):
        remove_upload(doc.file_path)
    SupplierDocument.query.filter(SupplierDocument.user_id.in_(user_ids)).delete(synchronize_session=False)
    if app_ids:
        SupplierApplication.query.filter(SupplierApplication.id.in_(app_ids)).delete(synchronize_session=False)
    Notification.query.filter(Notification.user_id.in_(user_ids)).delete(synchronize_session=False)
    User.query.filter(User.id.in_(user_ids)).delete(synchronize_session=False)
    DB.session.commit()
    for uid in user_ids:
        folder = os.path.join(UPLOAD_ROOT, str(uid))
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
    return len(user_ids)


@app.cli.command("seed-demo")
@click.option("--suppliers", "count", default=30, show_default=True, help="Number of demo suppliers.")
@click.option("--months", default=6, show_default=True, help="Spread registrations and requests over this many months.")
@click.option("--requests", "request_count", default=60, show_default=True, help="Number of demo product requests.")
@click.option("--remove", is_flag=True, help="Delete all demo data instead of creating it.")
@click.option("--seed", default=2026, show_default=True, help="Random seed, for repeatable demo data.")
def seed_demo_command(count, months, request_count, remove, seed):
    """Create (or --remove) realistic demo suppliers, applications, inventory and requests.

    Demo suppliers use @demo-supplier.example addresses (undeliverable), password Password123!
    """
    import random

    if remove:
        click.echo(f"Removed {remove_demo_data()} demo suppliers and everything linked to them.")
        return
    if User.query.filter(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}")).first():
        raise click.ClickException("Demo data already exists. Run `flask --app app seed-demo --remove` first to replace it.")
    admins = Admin.query.order_by(Admin.id).all()
    if not admins:
        raise click.ClickException("Create an admin first: flask --app app create-admin")

    rng = random.Random(seed)
    now = utcnow()
    period_start = now - timedelta(days=30 * months)
    status_pool = (["approved"] * 14 + ["pending_review"] * 5 + ["under_review"] * 4 + ["submitted"] * 2
                   + ["returned_for_update"] * 3 + ["declined"] * 2)
    rng.shuffle(status_pool)
    password_hash = generate_password_hash("Password123!")

    def event(application, when, actor, action, summary="", from_status=None, to_status=None, changes=None):
        DB.session.add(ApplicationEvent(
            application_id=application.id, created_at=when,
            actor_type="admin" if isinstance(actor, Admin) else "supplier", actor_id=actor.id,
            actor_name=(actor.name if isinstance(actor, Admin) else actor.contact_name) or actor.email, actor_email=actor.email,
            action=action, summary=summary or EVENT_LABELS.get(action, action), from_status=from_status, to_status=to_status,
            changes_json=json.dumps(changes) if changes else None, ip_address="192.0.2.10",
        ))

    created_users, approved = [], []
    for i in range(min(count, len(DEMO_COMPANIES))):
        company, category = DEMO_COMPANIES[i]
        person = DEMO_PEOPLE[i % len(DEMO_PEOPLE)]
        slug = re.sub(r"[^a-z0-9]+", "", company.lower())
        email = f"{slug}@{DEMO_EMAIL_DOMAIN}"
        province = rng.choice(list(DEMO_CITIES))
        city = rng.choice(DEMO_CITIES[province])
        registered = period_start + timedelta(days=int(i * (30 * months - 7) / max(count, 1)), hours=rng.randint(7, 18), minutes=rng.randint(0, 59))
        phone = f"0{rng.choice([6, 7, 8])}{rng.randint(10000000, 99999999)}"
        status = status_pool[i % len(status_pool)]

        user = User(supplier_id=generate_supplier_id(), name=person, email=email, password_hash=password_hash,
                    company_name=company, contact_name=person, phone=phone, address=f"{rng.randint(1, 250)} Main Road, {city}",
                    email_verified=True, active=(status == "approved"), created_at=registered)
        DB.session.add(user)
        DB.session.flush()
        created_users.append(user)

        submitted_at = registered + timedelta(days=rng.randint(0, 3), hours=rng.randint(1, 6))
        vat = f"4{rng.randint(100000000, 999999999)}" if rng.random() < 0.6 else ""
        application = SupplierApplication(
            company_name=company, contact_name=person, email=email, phone=phone, company_profile="",
            documents_status="complete", status="pending_review", created_at=submitted_at, updated_at=submitted_at,
            registered_vendor_name=f"{company} (Pty) Ltd", trading_name=company if rng.random() < 0.3 else "",
            business_registration_number=f"20{rng.randint(10, 25)}/{rng.randint(100000, 999999)}/07",
            vat_number=vat, tax_number=f"9{rng.randint(100000000, 999999999)}",
            physical_address=user.address.split(",")[0], city=city, province=province, postal_code=f"{rng.randint(1000, 9999)}",
            website=f"www.{slug}.example" if rng.random() < 0.5 else "",
            primary_contact_person=person, contact_person_role=rng.choice(["Director", "Owner", "Managing Director", "Operations Manager"]),
            contact_number=phone, email_address=email,
        )
        details = {category: rng.choice(["Fleet of 3 vehicles", "Mercedes-Benz fleet", "Full AV crew"])} if category in get_specify_categories() else {}
        replace_categories(application, [category], details)
        owner_surname = person.split()[-1]
        replace_directors(application, [
            {"initials_surname": f"{person[0]}. {owner_surname}", "id_number": demo_sa_id(rng, rng.randint(1965, 1995)), "role": "Director", "nationality": SOUTH_AFRICA},
            *([{"initials_surname": f"{rng.choice('ABKLMNPST')}. {owner_surname}", "id_number": demo_sa_id(rng, rng.randint(1970, 1998)),
                "role": "Shareholder", "nationality": SOUTH_AFRICA}] if rng.random() < 0.5 else []),
        ])
        DB.session.add(application)
        DB.session.flush()

        # Documents: tiny placeholder PDFs; incomplete applications miss one or two.
        doc_types = [COMPANY_PROFILE_DOC] + REQUIRED_DOCUMENTS
        missing = set(rng.sample(REQUIRED_DOCUMENTS, rng.randint(1, 2))) if status in {"returned_for_update", "declined"} and rng.random() < 0.7 else set()
        folder = os.path.join(UPLOAD_ROOT, str(user.id))
        os.makedirs(folder, exist_ok=True)
        for doc_type in doc_types:
            if doc_type in missing:
                continue
            filename = f"{int(submitted_at.timestamp())}_{secrets.token_hex(3)}_{secure_filename(doc_type)[:40]}.pdf"
            with open(os.path.join(folder, filename), "wb") as fh:
                fh.write(DEMO_PDF)
            DB.session.add(SupplierDocument(user_id=user.id, application_id=application.id, document_type=doc_type,
                                            file_path=f"uploads/{user.id}/{filename}", original_filename=f"{doc_type}.pdf",
                                            uploaded_at=submitted_at))
        application.documents_status = "pending" if missing else "complete"

        event(application, submitted_at, user, "submitted", summary=f"Submitted with {len(doc_types) - len(missing)} document(s)", to_status="pending_review")
        when = submitted_at
        if status != "submitted" and not (status == "pending_review" and rng.random() < 0.5):
            admin = rng.choice(admins)
            when = submitted_at + timedelta(days=rng.randint(1, 4), hours=rng.randint(1, 5))
            application.assigned_admin_id, application.assigned_at = admin.id, when
            event(application, when, admin, "assigned", summary=f"Assigned to {admin.name or admin.email}",
                  changes=[{"field": "Assigned to", "old": "", "new": admin.name or admin.email}])
            for doc_type in rng.sample(doc_types, 3):
                when += timedelta(minutes=rng.randint(2, 20))
                event(application, when, admin, "document_viewed", summary=f"Viewed {doc_type}")
            previous = "pending_review"
            path = {"under_review": ["under_review"], "approved": ["under_review", "approved"], "declined": ["under_review", "declined"],
                    "returned_for_update": ["under_review", "returned_for_update"]}.get(status, [])
            for step in path:
                when += timedelta(days=rng.randint(1, 5), hours=rng.randint(1, 6))
                comment = rng.choice(DEMO_COMMENTS[step]) if step in DEMO_COMMENTS else ""
                event(application, when, admin, "status_changed", summary=f"{status_label(previous)} → {status_label(step)}",
                      from_status=previous, to_status=step,
                      changes=[{"field": "Review comments", "old": "", "new": comment}] if comment else None)
                if comment:
                    application.review_comments = comment
                DB.session.add(Notification(user_id=user.id, title="Application Status Updated", created_at=when, is_read=rng.random() < 0.6,
                                            message=f"Your application for {company} has been updated to: {status_label(step)}.",
                                            related_application_id=application.id))
                previous = step
            if status == "approved":
                event(application, when, admin, "account_activated", summary="Supplier account activated automatically on approval",
                      changes=[{"field": "Account", "old": "Inactive", "new": "Active"}])
                approved.append((user, category, when))
        application.status = status
        application.updated_at = min(when, now)

    # Inventory for approved suppliers.
    products = []
    for user, category, approved_at in approved:
        for name, price, unit in rng.sample(DEMO_PRODUCTS[category], min(len(DEMO_PRODUCTS[category]), rng.randint(2, 4))):
            product = Product(user_id=user.id, name=name, category=category, unit=unit,
                              description=f"{name} supplied by {user.company_name}, {rng.choice(['delivered', 'available'])} across {rng.choice(['the metro', 'the province', 'KZN', 'Gauteng'])}.",
                              price=Decimal(price) * Decimal(rng.choice(["0.9", "1", "1", "1.1", "1.15"])),
                              quantity_available=rng.choice([None, None, 0, 2, 5, 10, 25]) if unit == "each" else None,
                              lead_time_days=rng.randint(1, 7), is_listed=rng.random() < 0.9,
                              created_at=approved_at + timedelta(days=rng.randint(0, 5)))
            product.price = product.price.quantize(Decimal("1"))
            DB.session.add(product)
            products.append((product, approved_at))
    DB.session.flush()

    # Product requests spread over the period after each supplier was approved.
    request_status_pool = ["completed"] * 8 + ["delivered"] * 3 + ["accepted"] * 3 + ["requested"] * 3 + ["declined", "cancelled"]
    locations = ["Icebolethu Durban branch, 12 Smith St", "Icebolethu Pietermaritzburg branch", "Icebolethu Johannesburg office",
                 "Family home, Umlazi", "Community hall, KwaMashu", "Gravesite, Chesterville cemetery"]
    created_requests = 0
    for _ in range(request_count if products else 0):
        product, approved_at = rng.choice(products)
        start_at = max(approved_at + timedelta(days=1), period_start)
        if start_at >= now:
            continue
        created = start_at + timedelta(seconds=rng.randint(0, int((now - start_at).total_seconds())))
        admin = rng.choice(admins)
        status = rng.choice(request_status_pool)
        product_request = ProductRequest(
            product_id=product.id, supplier_id=product.user_id, admin_id=admin.id, product_name=product.name, unit=product.unit,
            unit_price=product.price, quantity=rng.randint(1, 3 if product.unit == "each" else 2),
            required_by=(created + timedelta(days=rng.randint(3, 14))).date(), delivery_location=rng.choice(locations),
            notes=rng.choice(["", "Service on Saturday morning.", "Please confirm delivery time with the family.", "Urgent: needed by Friday."]),
            status=status, created_at=created, updated_at=created,
        )
        DB.session.add(product_request)
        DB.session.flush()
        supplier = product_request.supplier

        def req_event(when, actor, action, note="", frm=None, to=None):
            DB.session.add(ProductRequestEvent(
                request_id=product_request.id, created_at=when, actor_type="admin" if isinstance(actor, Admin) else "supplier",
                actor_name=(actor.name if isinstance(actor, Admin) else actor.contact_name) or actor.email,
                action=action, note=note, from_status=frm, to_status=to))

        req_event(created, admin, "requested", product_request.notes, to="requested")
        when = created
        steps = {"accepted": [("accept", "requested", "accepted")], "declined": [("decline", "requested", "declined")],
                 "delivered": [("accept", "requested", "accepted"), ("deliver", "accepted", "delivered")],
                 "completed": [("accept", "requested", "accepted"), ("deliver", "accepted", "delivered"), ("complete", "delivered", "completed")],
                 "cancelled": [("accept", "requested", "accepted"), ("cancel", "accepted", "cancelled")]}.get(status, [])
        for action, frm, to in steps:
            when = min(when + timedelta(hours=rng.randint(2, 48)), now)
            actor = admin if action in {"complete", "cancel"} else supplier
            note = {"accept": rng.choice(["", "Confirmed, will deliver on time.", "Available, delivery Friday morning."]),
                    "decline": "Fully booked on that date, sorry.", "deliver": rng.choice(["Delivered and signed for.", "Set up on site."]),
                    "cancel": "Family changed the service date.", "complete": ""}[action]
            if action == "decline":
                product_request.supplier_note = note
            elif note and action in {"accept", "deliver"}:
                product_request.supplier_note = note
            req_event(when, actor, action, note, frm, to)
        if status in {"completed", "delivered", "accepted"} and rng.random() < 0.6:
            req_event(min(when + timedelta(hours=1), now), admin, "quotation", "Quotation downloaded as PDF")
        product_request.updated_at = when
        created_requests += 1

    DB.session.commit()
    click.echo(f"Created {len(created_users)} demo suppliers ({len(approved)} approved, {len(products)} products) "
               f"and {created_requests} product requests over the last {months} months.")
    click.echo(f"Demo supplier logins: <company>@{DEMO_EMAIL_DOMAIN} / Password123!   Remove with: flask --app app seed-demo --remove")


init_database()


if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG"), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")))
