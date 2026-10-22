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
from sqlalchemy import case, false, func, literal, null, select, text, union_all
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

# Certificate expiry checks run in the background every N hours (0 turns them off, e.g. when cron
# runs `flask --app app check-certificates` instead). Links in those emails use PUBLIC_BASE_URL.
CERT_CHECK_INTERVAL_HOURS = float(os.getenv("CERT_CHECK_INTERVAL_HOURS", "6"))
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")

# Supplier registration certificate, issued on approval. A signature image, if wanted, goes in
# static/images/certificate_signature.png (transparent PNG).
CERTIFICATE_SIGNATORY_NAME = os.getenv("CERTIFICATE_SIGNATORY_NAME", "")
CERTIFICATE_SIGNATORY_TITLE = os.getenv("CERTIFICATE_SIGNATORY_TITLE", "Chief Financial Officer")
CERTIFICATE_FOOTER = os.getenv(
    "CERTIFICATE_FOOTER", "Icebolethu Group · Call Centre 031 208 9102 · supplier_onboarding@icebolethu.co.za")
CERTIFICATE_VALID_DAYS = 365


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
# Certificates that carry an issue and expiry date. Both dates are mandatory whenever these are uploaded.
DATED_DOCUMENTS = {
    "SARS VAT Certificate": "SARS VAT certificate",
    "Valid B-BBEE certificate, letter from Accountant or Sworn Affidavit": "B-BBEE certificate, accountant's letter or affidavit",
}
EXPIRY_WARNING_DAYS = 30
# Reminders go out this many days before expiry (largest first); expiry itself deactivates the account.
EXPIRY_REMINDER_DAYS = (30, 7)
# Sensible bounds for those dates (also stop browsers accepting 5- or 6-digit years).
EARLIEST_ISSUE_DATE = date(1990, 1, 1)
MAX_VALIDITY_YEARS = 20


def latest_expiry_date(today=None):
    today = today or date.today()
    return today.replace(year=today.year + MAX_VALIDITY_YEARS, day=28 if today.month == 2 and today.day == 29 else today.day)
COMPANY_PROFILE_DOC = "Company Profile"
OTHER_DOCS = "Other Supporting Documents"

UPLOAD_SLOTS = {f"document_file_{i}": doc for i, doc in enumerate(REQUIRED_DOCUMENTS, start=1)}
UPLOAD_SLOTS["document_file_other"] = OTHER_DOCS
UPLOAD_SLOTS["document_file_company_profile"] = COMPANY_PROFILE_DOC

STATUSES = ["draft", "submitted", "pending_review", "under_review", "approved", "on_file", "declined", "returned_for_update"]
DOCUMENT_STATUSES = ["pending", "complete"]
EDITABLE_STATUSES = {"draft", "submitted", "pending_review", "under_review", "returned_for_update", "declined"}
# "On file": everything checked and in order, but no supplier account yet. Kept for when the services are needed.
ON_FILE_MESSAGE = ("Your application has been received and all your documents and information are in order. "
                   "We will keep your details on file and contact you when we need your services.")

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
# Bump when templates/terms_content.html changes materially; each acceptance records the version agreed to.
TERMS_VERSION = "2026-10"
TERMS_REQUIRED_MESSAGE = "Please accept our Terms & Conditions."
# Bump when templates/application_terms_content.html changes materially; each application records the version accepted.
APPLICATION_TERMS_VERSION = "2026-10"
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
    # True while the supplier account is deactivated because a certificate expired; renewing restores it.
    expiry_suspended = DB.Column(DB.Boolean, nullable=False, default=False)
    # Set each time the application is approved; the registration certificate is dated from it.
    approved_at = DB.Column(DB.DateTime, nullable=True)
    # Supplier Application Terms accepted when the application was submitted.
    terms_version = DB.Column(DB.String(20), nullable=True)
    terms_accepted_at = DB.Column(DB.DateTime, nullable=True)
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
    issue_date = DB.Column(DB.Date, nullable=True)  # set for DATED_DOCUMENTS only
    expiry_date = DB.Column(DB.Date, nullable=True)
    # Last expiry notice sent for these dates: "" (none), "30", "7" (days-before reminders) or "expired".
    expiry_notice = DB.Column(DB.String(10), nullable=False, default="")

    @property
    def expiry_state(self):
        """'expired', 'expiring' (within EXPIRY_WARNING_DAYS), 'valid', or None when there is no expiry date."""
        if not self.expiry_date:
            return None
        days_left = (self.expiry_date - date.today()).days
        if days_left < 0:
            return "expired"
        return "expiring" if days_left <= EXPIRY_WARNING_DAYS else "valid"


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
    """Retired: product requests were removed from the portal (orders are placed outside it).

    The table is kept so existing rows, and their links to products and suppliers, stay valid.
    """
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
    """Retired with ProductRequest; kept for existing rows."""
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


class AuditLog(DB.Model):
    """Append-only log of sign-ins, account security events and ICT admin actions, for the user audit."""
    __tablename__ = "audit_logs"

    id = DB.Column(DB.Integer, primary_key=True)
    created_at = DB.Column(DB.DateTime, default=utcnow, nullable=False, index=True)
    actor_type = DB.Column(DB.String(20), nullable=False, default="system")  # supplier | admin | system
    actor_id = DB.Column(DB.Integer, nullable=True)
    actor_name = DB.Column(DB.String(150), nullable=False, default="")
    actor_email = DB.Column(DB.String(120), nullable=False, default="", index=True)
    action = DB.Column(DB.String(40), nullable=False, index=True)
    detail = DB.Column(DB.String(255), nullable=False, default="")
    ip_address = DB.Column(DB.String(64), nullable=False, default="")


class EmailTemplate(DB.Model):
    """An ICT admin's edited version of one of the EMAIL_TEMPLATES; without a row the default text is used."""
    __tablename__ = "email_templates"

    key = DB.Column(DB.String(60), primary_key=True)
    subject = DB.Column(DB.String(200), nullable=False)
    body = DB.Column(DB.Text, nullable=False)
    updated_at = DB.Column(DB.DateTime, default=utcnow, onupdate=utcnow)
    updated_by = DB.Column(DB.String(120), nullable=False, default="")


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
    terms_version = DB.Column(DB.String(20), nullable=True)
    terms_accepted_at = DB.Column(DB.DateTime, nullable=True)
    created_at = DB.Column(DB.DateTime, default=utcnow)

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def accept_terms(self):
        self.terms_version = TERMS_VERSION
        self.terms_accepted_at = utcnow()

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


# Admin roles and the sections of the admin portal each one can open.
ADMIN_ROLES = {
    "super": "Procurement Team Lead",
    "applications": "Procurement Officer",
    "reports": "Procurement Manager",
    "ict": "ICT Admin",
}
ROLE_SECTIONS = {
    "super": {"dashboard", "applications", "suppliers", "catalogue", "reports"},
    "applications": {"applications", "suppliers"},
    "reports": {"reports"},
    # Everything except the dashboard, plus the ICT tools (user audit, email templates, deleting records).
    "ict": {"applications", "suppliers", "catalogue", "reports", "ict"},
}
# Roles that can do what a Procurement Team Lead can (assign applications to others).
FULL_ACCESS_ROLES = {"super", "ict"}
# Roles that receive application notifications (ICT can open applications but isn't part of the review workflow).
NOTIFIED_ROLES = ["super", "applications"]
# Where each section starts, in sidebar order; an admin lands on the first one they can open.
SECTION_HOMES = [
    ("dashboard", "admin_dashboard"),
    ("applications", "admin_applications"),
    ("suppliers", "admin_suppliers"),
    ("catalogue", "admin_catalogue"),
    ("reports", "admin_reports"),
    ("ict", "admin_user_audit"),
]


class Admin(DB.Model):
    __tablename__ = "admins"

    id = DB.Column(DB.Integer, primary_key=True)
    email = DB.Column(DB.String(120), unique=True, nullable=False)
    password_hash = DB.Column(DB.String(256), nullable=False)
    name = DB.Column(DB.String(100), nullable=False, default="")
    role = DB.Column(DB.String(20), nullable=False, default="super")
    created_at = DB.Column(DB.DateTime, default=utcnow)

    @property
    def role_label(self):
        return ADMIN_ROLES.get(self.role, "Admin")

    @property
    def has_full_access(self):
        return self.role in FULL_ACCESS_ROLES

    def can(self, section):
        return section in ROLE_SECTIONS.get(self.role, set())

    @property
    def home_endpoint(self):
        return next((endpoint for section, endpoint in SECTION_HOMES if self.can(section)), "admin_logout")

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


# Emails to suppliers that ICT admins can edit (subject and message). Each {placeholder} is filled in when the
# email is sent; `placeholders` maps each one to (what it is, an example for the preview). The same text is used
# for the in-portal notification. Emails to admins stay in the code.
EMAIL_TEMPLATES = {
    "verification_code": {
        "name": "Verification code",
        "description": "Sent when a supplier signs up or logs in.",
        "subject": "Your IGSP Verification Code",
        "body": "Your verification code is {code}. It expires in 10 minutes. "
                "If you did not try to sign in, you can ignore this email.",
        "placeholders": {"code": ("The 6-digit code", "482915")},
    },
    "password_reset": {
        "name": "Password reset",
        "description": "Sent when a supplier asks to reset their password. The email has a reset button.",
        "subject": "Reset your IGSP password",
        "body": "We received a request to reset your password. The link below is valid for one hour. "
                "If you didn't ask for this, you can ignore this email.",
        "placeholders": {},
    },
    "application_received": {
        "name": "Application received",
        "description": "Sent when a supplier submits their application.",
        "subject": "Application Received",
        "body": "Thank you — your supplier application has been received and is awaiting review. "
                "We'll notify you when its status changes.",
        "placeholders": {"company_name": ("The supplier's company name", "Acme Caskets")},
    },
    "application_status_updated": {
        "name": "Application status changed",
        "description": "Sent when an admin moves an application to under review, returned for update or declined.",
        "subject": "Application Status Updated",
        "body": "Your application for {company_name} has been updated to: {status}.",
        "placeholders": {"company_name": ("The supplier's company name", "Acme Caskets"),
                         "status": ("The new status", "Returned for update")},
    },
    "application_on_file": {
        "name": "Application kept on file",
        "description": "Sent when an admin accepts an application and keeps it on file.",
        "subject": "Application Received – On File",
        "body": "Thank you for applying to become a supplier for {company_name}. " + ON_FILE_MESSAGE,
        "placeholders": {"company_name": ("The supplier's company name", "Acme Caskets")},
    },
    "application_approved": {
        "name": "Application approved (welcome)",
        "description": "Sent when an application is approved. The email links to the supplier's registration certificate.",
        "subject": "Welcome to Icebolethu Group",
        "body": "Congratulations! Your application has been approved and {company_name} is now registered "
                "as an Approved Supplier of Icebolethu Group. Your supplier account is active: sign in to list the "
                "products and services you offer and keep your VAT and B-BBEE certificates up to date. "
                "Your Certificate of Supplier Registration is ready to view, print or save as PDF (valid until {valid_until}).",
        "placeholders": {"company_name": ("The supplier's company name", "Acme Caskets"),
                         "valid_until": ("Date the registration certificate expires", "22 Oct 2027")},
    },
    "review_comments_added": {
        "name": "Review comments added",
        "description": "Sent when an admin adds or changes the comments to the supplier.",
        "subject": "Review Comments Added",
        "body": "An administrator added review comments to your application: {comments}",
        "placeholders": {"comments": ("The admin's comments", "Please upload a bank letter not older than 3 months.")},
    },
    "account_deactivated_decision": {
        "name": "Account deactivated (declined or on file)",
        "description": "Sent when an active supplier's application is declined or put on file.",
        "subject": "Account Deactivated",
        "body": "Your supplier account has been deactivated because your application was {reason}.",
        "placeholders": {"reason": ("\"declined\" or \"put on file\"", "declined")},
    },
    "account_status_changed": {
        "name": "Account activated or deactivated by an admin",
        "description": "Sent when an admin switches a supplier account on or off.",
        "subject": "Account Status Updated",
        "body": "Your supplier account has been {state} by the administrator.",
        "placeholders": {"state": ("\"activated\" or \"deactivated\"", "deactivated")},
    },
    "certificate_expiring": {
        "name": "Certificate expiring soon",
        "description": "Sent 30 and 7 days before a VAT or B-BBEE certificate expires.",
        "subject": "Certificate expiring soon",
        "body": "Your {certificate} for {company_name} expires {when} ({expiry_date}). "
                "Please upload the renewed certificate in the supplier portal.{consequence}",
        "placeholders": {"certificate": ("Which certificate", "SARS VAT certificate"),
                         "company_name": ("The supplier's company name", "Acme Caskets"),
                         "when": ("\"in 7 days\", \"today\" etc.", "in 7 days"),
                         "expiry_date": ("The expiry date", "29 Oct 2026"),
                         "consequence": ("A warning that the account will be deactivated (only for active accounts)",
                                         " If it expires before you do, your supplier account will be deactivated until it is renewed.")},
    },
    "certificate_expired_deactivated": {
        "name": "Certificate expired – account deactivated",
        "description": "Sent when a certificate expires and the supplier's account is deactivated.",
        "subject": "Certificate expired – account deactivated",
        "body": "Your {certificate} for {company_name} expired on {expiry_date}. "
                "Your supplier account has been deactivated until you upload the renewed certificate.",
        "placeholders": {"certificate": ("Which certificate", "SARS VAT certificate"),
                         "company_name": ("The supplier's company name", "Acme Caskets"),
                         "expiry_date": ("The expiry date", "22 Oct 2026")},
    },
    "certificate_expired": {
        "name": "Certificate expired (account not active)",
        "description": "Sent when a certificate expires for a supplier whose account isn't active.",
        "subject": "Certificate expired",
        "body": "Your {certificate} for {company_name} expired on {expiry_date}. "
                "Please upload the renewed certificate to keep your application up to date.",
        "placeholders": {"certificate": ("Which certificate", "SARS VAT certificate"),
                         "company_name": ("The supplier's company name", "Acme Caskets"),
                         "expiry_date": ("The expiry date", "22 Oct 2026")},
    },
    "account_reactivated": {
        "name": "Account reactivated after renewal",
        "description": "Sent when renewed certificates reactivate a supplier's account.",
        "subject": "Account reactivated",
        "body": "Thank you for uploading your renewed certificate. Your supplier account is active again.",
        "placeholders": {},
    },
}
PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")


def fill_placeholders(text, values):
    """Replace {name} with values[name]; anything else in braces is left as typed."""
    return PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), text)


def email_text(key, **values):
    """(subject, message) for an editable email: the ICT-edited version if there is one, else the default."""
    template = EMAIL_TEMPLATES[key]
    saved = DB.session.get(EmailTemplate, key)
    subject, body = (saved.subject, saved.body) if saved else (template["subject"], template["body"])
    return fill_placeholders(subject, values), fill_placeholders(body, values)


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
    subject, message = email_text("verification_code", code=code)
    body = render_email(subject, message, "Supplier")
    try:
        _deliver(to_email, subject, body, "html")
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


def notify_supplier(supplier, title, message, application=None, action_url=None, action_label=None):
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
        action_url=action_url or url_for("dashboard", _external=True),
        action_label=action_label or "Open your dashboard",
    )


def notify_admins(title, message, application=None):
    action_url = (
        url_for("admin_application_detail", application_id=application.id, _external=True)
        if application else None
    )
    for admin in Admin.query.filter(Admin.role.in_(NOTIFIED_ROLES)):
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


def admin_section(section):
    """Limit an admin view to the roles that can open `section` (stack under @admin_required)."""
    def decorator(view_func):
        @wraps(view_func)
        def wrapper(*args, **kwargs):
            admin = get_current_admin()
            if not admin.can(section):
                flash("You don't have access to that page.", "error")
                return redirect(url_for(admin.home_endpoint))
            return view_func(*args, **kwargs)
        return wrapper
    return decorator


def super_admin_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not get_current_admin().has_full_access:
            flash("Only a Procurement Team Lead or ICT Admin can do that.", "error")
            return redirect(url_for(get_current_admin().home_endpoint))
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
    "certificate_reminder": "Certificate expiry reminder sent",
    "certificate_expired": "Certificate expired",
    "certificates_renewed": "Certificates renewed by supplier",
    "registration_certificate_issued": "Supplier registration certificate issued",
    "director_removed": "Director removed by ICT",
    "history_exported": "History exported",
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


# Admin activity that isn't tied to one application (sign-ins, exports, ICT actions). Everything admins do on an
# application is in ApplicationEvent; the user audit shows both together, for admins only.
AUDIT_ACTIONS = {
    "admin_login": "Admin signed in",
    "admin_login_failed": "Admin sign-in failed",
    "admin_logout": "Admin signed out",
    "application_deleted": "Application deleted",
    "director_deleted": "Director deleted",
    "email_template_updated": "Email template edited",
    "email_template_reset": "Email template reset to default",
    "report_exported": "Report exported",
    "audit_exported": "User audit exported",
}


def audit(action, detail="", actor=None, actor_type=None, email=""):
    """Add one user-audit entry for an admin (the caller commits). `actor` defaults to the signed-in admin;
    for a failed sign-in pass `actor_type="admin"` and the `email` that was typed instead."""
    if actor is None and actor_type is None:
        actor = get_current_admin()
    if isinstance(actor, Admin):
        actor_type, name = "admin", actor.name
    elif isinstance(actor, User):
        actor_type, name = "supplier", actor.contact_name or actor.company_name
    else:
        name = ""
    DB.session.add(AuditLog(
        actor_type=actor_type or "system",
        actor_id=getattr(actor, "id", None),
        actor_name=(name or "")[:150],
        actor_email=(getattr(actor, "email", None) or email or "")[:120],
        action=action,
        detail=(detail or "")[:255],
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
        "static_url": static_url,
        "COUNTRIES": COUNTRIES,
        "normalize_nationality": normalize_nationality,
        "csrf_token": csrf_token,
        "status_label": status_label,
        "unread_notifications": unread,
        "supplier_application": supplier_application,
        "PRODUCT_UNITS": PRODUCT_UNITS,
        "EDITABLE_STATUSES": EDITABLE_STATUSES,
        "DATED_DOCUMENTS": DATED_DOCUMENTS,
        "EXPIRY_REMINDER_DAYS": EXPIRY_REMINDER_DAYS,
        "TODAY": date.today(),
        "EARLIEST_ISSUE_DATE": EARLIEST_ISSUE_DATE,
        "LATEST_EXPIRY_DATE": latest_expiry_date(),
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


def parse_document_dates(form, slots, documents, uploads, uploads_only=False):
    """Read the issue and expiry dates entered for dated documents (VAT and B-BBEE certificates).

    The date fields are named after the upload slot (``<slot>_issue_date`` / ``<slot>_expiry_date``). Dates are
    required for every dated document being uploaded now or already on file (only those being uploaded when
    `uploads_only`). Returns ({document_type: (issue, expiry)}, error).
    """
    on_file = {doc_type for doc_type, _ in uploads}
    if not uploads_only:
        on_file |= {doc.document_type for doc in documents}
    today = date.today()
    dates = {}
    for field, doc_type in slots.items():
        if doc_type not in DATED_DOCUMENTS or doc_type not in on_file:
            continue
        name = DATED_DOCUMENTS[doc_type]
        parsed = {}
        for part, label in (("issue_date", "issue"), ("expiry_date", "expiry")):
            raw = (form.get(f"{field}_{part}") or "").strip()
            if not raw:
                return None, f"Please enter the {label} date of your {name}."
            try:
                parsed[part] = datetime.strptime(raw, "%Y-%m-%d").date()
            except ValueError:
                return None, f"The {label} date of your {name} isn't a valid date; please use a 4-digit year."
        issue, expiry = parsed["issue_date"], parsed["expiry_date"]
        if issue < EARLIEST_ISSUE_DATE:
            return None, f"Please check the issue date of your {name}; it can't be before {EARLIEST_ISSUE_DATE:%Y}."
        if expiry > latest_expiry_date(today):
            return None, (f"Please check the expiry date of your {name}; it can't be more than "
                          f"{MAX_VALIDITY_YEARS} years from today.")
        if issue > today:
            return None, f"The issue date of your {name} can't be in the future."
        if expiry <= issue:
            return None, f"The expiry date of your {name} must be after its issue date."
        if expiry < today:
            return None, f"Your {name} expired on {expiry.strftime('%d %b %Y')}. Please upload a current, valid one."
        dates[doc_type] = (issue, expiry)
    return dates, None


def apply_document_dates(documents, dates):
    """Store parsed dates on the matching documents; returns [change] entries for the audit trail."""
    changes = []
    for document in documents:
        if document.document_type not in dates:
            continue
        issue, expiry = dates[document.document_type]
        if (document.issue_date, document.expiry_date) != (issue, expiry):
            old = (f"{document.issue_date:%d %b %Y} – {document.expiry_date:%d %b %Y}"
                   if document.issue_date and document.expiry_date else "")
            changes.append({"field": f"Dates: {document.document_type}", "old": old,
                            "new": f"{issue:%d %b %Y} – {expiry:%d %b %Y}"})
            document.issue_date, document.expiry_date = issue, expiry
            document.expiry_notice = ""
    return changes


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


# A director, shareholder or owner should be linked to one supplier only. Applications that break this still go
# through, but are flagged to admins. Any application that has been submitted (whatever its outcome since) counts;
# unsubmitted drafts don't.
def director_conflicts(directors, email):
    """Directors (dicts with id_number) already on another supplier's application.

    Returns [(director, other_application, other_director), ...]. The applicant's own applications don't count.
    """
    by_id = {d["id_number"]: d for d in directors}
    if not by_id:
        return []
    rows = (DB.session.query(SupplierDirector, SupplierApplication)
            .join(SupplierApplication, SupplierDirector.application_id == SupplierApplication.id)
            .filter(SupplierApplication.status != "draft", SupplierDirector.id_number.in_(by_id),
                    func.lower(SupplierApplication.email) != normalize_email(email))
            .order_by(SupplierApplication.created_at).all())
    return [(by_id[other_d.id_number], other, other_d) for other_d, other in rows]


def director_matches(applications):
    """For the admin view: directors of `applications` who were already on another supplier's earlier application.

    Only the later application is flagged; the first one stands.
    Returns {application_id: [(director, other_application, other_director)]}.
    """
    matches = {}
    for application in applications:
        directors = [{"id_number": d.id_number, "obj": d} for d in application.directors]
        found = [(d["obj"], other, other_d) for d, other, other_d in director_conflicts(directors, application.email)
                 if other.id < application.id]
        if found:
            matches[application.id] = found
    return matches


def flag_director_matches(application):
    """Tell admins (once) that a submitted application lists a director already on another supplier."""
    found = director_matches([application]).get(application.id)
    if not found:
        return
    title = f"Director match: {application.company_name}"
    message = (f"{application.company_name} ({application.email}) lists "
               + "; ".join(f"{d.initials_surname} ({d.role}, {d.id_number}), already {other_d.role} at "
                           f"{other.company_name}" for d, other, other_d in found)
               + ". The application can only be declined.")
    if not Notification.query.filter_by(title=title, message=message).first():
        notify_admins(title, message, application)


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


def static_url(filename):
    """url_for('static') plus ?v=<modified time>, so browsers don't keep running an old copy after a change."""
    try:
        version = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        version = 0
    return url_for("static", filename=filename, v=version)


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
        if not request.form.get("accept_terms"):
            flash(TERMS_REQUIRED_MESSAGE, "error")
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
        user.accept_terms()
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
        if not request.form.get("accept_terms"):
            return render_template("login.html", login_error=TERMS_REQUIRED_MESSAGE)
        if login_limiter.is_blocked(limiter_key):
            return render_template("login.html", login_error="Too many login attempts. Please wait 15 minutes and try again.")

        user = find_user_by_email(email)
        if not user or not user.check_password(password):
            login_limiter.hit(limiter_key)
            return render_template("login.html", login_error="Invalid email or password.")

        login_limiter.reset(limiter_key)
        user.accept_terms()
        if not start_email_verification(user):
            session.pop("pending_user_id", None)
            return render_template("login.html", login_error="We couldn't send your verification code. Please try again shortly.")
        return redirect(url_for("verify_email"))

    return render_template("login.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/application-terms")
def application_terms():
    return render_template("terms.html", page_title="Supplier Application Terms and Conditions",
                           content_template="application_terms_content.html", version=APPLICATION_TERMS_VERSION)


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
                    *email_text("password_reset"),
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
        dates, date_error = parse_document_dates(request.form, slots, pending_documents(user), uploads)
        if date_error:
            flash(date_error + " Nothing was uploaded; please choose the file again.", "error")
            return redirect(url_for("onboarding_documents"))

        for doc_type, file_storage in uploads:
            store_document(user.id, None, doc_type, file_storage)
        DB.session.flush()
        apply_document_dates(pending_documents(user), dates)
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
        if not request.form.get("accept_terms"):
            flash("Please accept the Terms & Conditions.", "error")
            return redirect(url_for("onboarding_review"))

        application = SupplierApplication(
            company_name=user.company_name,
            contact_name=user.contact_name,
            email=user.email,
            phone=user.phone,
            company_profile="",
            documents_status="complete",
            status="pending_review",
            terms_version=APPLICATION_TERMS_VERSION,
            terms_accepted_at=utcnow(),
            **{field: draft.get(field, "") for field in PROFILE_FIELDS},
        )
        replace_categories(application, draft["supplier_categories"], draft.get("supplier_category_details", {}))
        replace_directors(application, draft["directors"])
        DB.session.add(application)
        DB.session.flush()

        for document in documents:
            document.application_id = application.id

        log_event(application, "submitted", to_status="pending_review",
                  summary=f"Submitted with {len(documents)} document(s); accepted Application Terms {APPLICATION_TERMS_VERSION}")
        notify_admins(
            "New Supplier Application",
            f"New application submitted by {user.company_name} ({user.contact_name}) for {application.supplier_category}.",
            application,
        )
        notify_supplier(user, *email_text("application_received", company_name=application.company_name), application)
        flag_director_matches(application)
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


def certificate_alerts(application):
    """Dated certificates that are expired or expiring soon, for the supplier's dashboard."""
    if not application:
        return []
    documents = SupplierDocument.query.filter(SupplierDocument.application_id == application.id,
                                              SupplierDocument.document_type.in_(list(DATED_DOCUMENTS))).all()
    return sorted((doc for doc in documents if doc.expiry_state in {"expired", "expiring"}), key=lambda d: d.expiry_date)


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
        certificate_alerts=certificate_alerts(latest),
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
        dates, date_error = parse_document_dates(request.form, UPLOAD_SLOTS, documents, uploads)
        if date_error:
            flash(date_error + " Nothing was saved; please choose any files again.", "error")
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
        date_changes = apply_document_dates(all_docs, dates)
        application.documents_status = "pending" if missing_required_documents(all_docs) else "complete"

        resubmitted = application.status in {"returned_for_update", "declined"}
        if resubmitted:
            application.status = "pending_review"

        changes = diff_snapshots(before, application_snapshot(application))
        changes += [{"field": f"Document: {doc_type}", "old": previous_files.get(doc_type, ""), "new": fs.filename}
                    for doc_type, fs in uploads]
        changes += date_changes
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
        flag_director_matches(application)
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
        if not admin or not admin.can("applications") or not application:
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
        return redirect(url_for(get_current_admin().home_endpoint))

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
            audit("admin_login_failed", "Wrong password" if admin else "No admin account with this email",
                  actor=admin, actor_type="admin", email=email)
            DB.session.commit()
            return render_template("admin_login.html", step="options", login_error="Invalid email or password.")

        login_limiter.reset(limiter_key)
        audit("admin_login", "Email and password", actor=admin)
        DB.session.commit()
        session["admin_id"] = admin.id
        session.permanent = True
        return redirect(url_for(admin.home_endpoint))

    return render_template("admin_login.html", step="start")


@app.route("/admin/login/options")
def admin_login_options():
    if get_current_admin():
        return redirect(url_for(get_current_admin().home_endpoint))
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
        audit("admin_login_failed", "Microsoft account has no admin account", actor_type="admin", email=email)
        DB.session.commit()
        flash("No admin account found for this Microsoft account.", "error")
        return redirect(url_for("admin_login_options"))

    audit("admin_login", "Microsoft", actor=admin)
    DB.session.commit()
    session["admin_id"] = admin.id
    session.permanent = True
    flash(f"Welcome, {admin.name or admin.email}", "success")
    return redirect(url_for(admin.home_endpoint))


@app.route("/admin/logout")
def admin_logout():
    admin = get_current_admin()
    if admin:
        audit("admin_logout", actor=admin)
        DB.session.commit()
    session.pop("admin_id", None)
    return redirect(url_for("admin_login"))


AWAITING_REVIEW_STATUSES = ["submitted", "pending_review", "under_review"]


@app.route("/admin")
@admin_required
def admin_dashboard():
    if not get_current_admin().can("dashboard"):
        return redirect(url_for(get_current_admin().home_endpoint))
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
@admin_section("suppliers")
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
@admin_section("suppliers")
def toggle_supplier(supplier_id):
    supplier = DB.get_or_404(User, supplier_id)
    supplier.active = not supplier.active
    state = "activated" if supplier.active else "deactivated"
    supplier_application = get_user_application(supplier)
    if supplier_application:
        supplier_application.expiry_suspended = False
        log_event(supplier_application, f"account_{state}", summary=f"Supplier account {state}",
                  changes=[{"field": "Account", "old": "Inactive" if supplier.active else "Active",
                            "new": "Active" if supplier.active else "Inactive"}])
    notify_supplier(supplier, *email_text("account_status_changed", state=state))
    DB.session.commit()
    flash(f"Supplier {state} successfully.", "success")
    return redirect(safe_next_url(request.form.get("next"), url_for("admin_suppliers")))


@app.route("/admin/applications")
@admin_required
@admin_section("applications")
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
        flagged_ids=set(director_matches(pagination.items)),
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
@admin_section("applications")
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


def application_admins():
    """Admins a super admin can hand an application to."""
    return Admin.query.filter_by(role="applications").order_by(Admin.name, Admin.email).all()


@app.route("/admin/applications/<int:application_id>/assign-to", methods=["POST"])
@admin_required
@admin_section("applications")
@super_admin_required
def assign_application_to(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    detail_url = url_for("admin_application_detail", application_id=application.id)
    assignee = DB.session.get(Admin, request.form.get("admin_id", type=int) or 0)
    if not assignee or assignee.role != "applications":
        flash("Choose a Procurement Officer to assign this application to.", "error")
        return redirect(detail_url)

    owner = application.assigned_admin
    if owner and owner.id == assignee.id:
        flash(f"This application is already assigned to {assignee.name or assignee.email}.", "info")
        return redirect(detail_url)

    me = get_current_admin()
    previous = owner.name or owner.email if owner else ""
    new = assignee.name or assignee.email
    application.assigned_admin_id = assignee.id
    application.assigned_at = utcnow()
    log_event(application, "assigned", summary=f"Assigned to {new} by {me.name or me.email}",
              changes=[{"field": "Assigned to", "old": previous, "new": new}])
    title = "Application assigned to you"
    message = f"{me.name or me.email} assigned {application.company_name}'s application to you."
    DB.session.add(Notification(admin_id=assignee.id, title=title, message=message,
                                related_application_id=application.id))
    send_notification_email(assignee.email, title, message, recipient_name=assignee.name or "Admin",
                            application=application, action_url=url_for("admin_application_detail", application_id=application.id, _external=True),
                            action_label="Review application")
    DB.session.commit()
    flash(f"{application.company_name} is now assigned to {new}.", "success")
    return redirect(detail_url)


@app.route("/admin/applications/<int:application_id>/release", methods=["POST"])
@admin_required
@admin_section("applications")
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
@admin_section("applications")
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
        director_flags=director_matches([application]).get(application.id, []),
        history_count=len(application_history(application)),
        assignable_admins=application_admins() if get_current_admin().has_full_access else [],
    )


@app.route("/admin/applications/<int:application_id>/history")
@admin_required
@admin_section("applications")
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
@admin_section("applications")
def admin_application_history_export(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    log_event(application, "history_exported", summary="History exported to CSV")
    DB.session.commit()
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
@admin_section("applications")
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
    if new_status != "declined" and director_matches([application]):
        flash("This application can only be declined: a director, shareholder or owner is already linked to another supplier.", "error")
        return redirect(url_for("admin_application_detail", application_id=application.id))
    if new_status in {"declined", "returned_for_update"} and not request.form.get("review_comments", "").strip():
        flash("Please add a comment explaining to the supplier why the application is "
              f"{'declined' if new_status == 'declined' else 'being returned'}.", "error")
        return redirect(url_for("admin_application_detail", application_id=application.id))

    if new_status == "on_file":
        new_doc_status = "complete"  # being put on file means the documents were checked and are in order

    old_doc_status = application.documents_status
    if new_status == "approved" and old_status != "approved":
        application.approved_at = utcnow()
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
            if application.status == "on_file":
                notify_supplier(supplier, *email_text("application_on_file", company_name=application.company_name),
                                application)
            elif application.status != "approved":  # approval sends the welcome email below instead
                notify_supplier(supplier, *email_text("application_status_updated", company_name=application.company_name,
                                                      status=status_label(application.status)), application)
            if application.status != "approved" or not supplier.active:
                application.expiry_suspended = False  # an admin decision replaces any automatic suspension
            if application.status == "approved":
                if not supplier.active:
                    supplier.active = True
                    log_event(application, "account_activated", summary="Supplier account activated automatically on approval",
                              changes=[{"field": "Account", "old": "Inactive", "new": "Active"}])
                valid_until = certificate_details(application, supplier)["valid_until"].strftime("%d %b %Y")
                log_event(application, "registration_certificate_issued", summary=f"Valid until {valid_until}")
                notify_supplier(
                    supplier, *email_text("application_approved", company_name=application.company_name, valid_until=valid_until),
                    application, action_url=url_for("supplier_registration_certificate", _external=True),
                    action_label="View your supplier certificate",
                )
            elif application.status in {"declined", "on_file"} and supplier.active:
                reason = "declined" if application.status == "declined" else "put on file"
                supplier.active = False
                log_event(application, "account_deactivated", summary=f"Supplier account deactivated automatically ({reason})",
                          changes=[{"field": "Account", "old": "Active", "new": "Inactive"}])
                notify_supplier(supplier, *email_text("account_deactivated_decision", reason=reason), application)
        if application.review_comments and application.review_comments != old_comments:
            notify_supplier(supplier, *email_text("review_comments_added", comments=application.review_comments), application)

    DB.session.commit()
    flash("Application updated successfully.", "success")
    return redirect(url_for("admin_application_detail", application_id=application.id))


@app.route("/admin/applications/<int:application_id>/documents/<int:document_id>/replace", methods=["POST"])
@admin_required
@admin_section("applications")
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
    dates, date_error = parse_document_dates(request.form, {"document_file": document.document_type}, [document],
                                             [(document.document_type, file_storage)])
    if date_error:
        flash(date_error, "error")
        return redirect(detail_url)

    changes = [{"field": document.document_type, "old": document.original_filename, "new": file_storage.filename}]
    changes += apply_document_dates([document], dates)
    log_event(application, "document_replaced", summary=f"Replaced {document.document_type}", changes=changes)
    new_path = save_upload(file_storage, document.user_id)
    remove_upload(document.file_path)
    document.file_path = new_path
    document.original_filename = file_storage.filename
    document.uploaded_at = utcnow()
    if document.document_type in DATED_DOCUMENTS:
        restore_after_renewal(application, get_application_supplier(application))
    DB.session.commit()

    flash("Document replaced successfully.", "success")
    return redirect(detail_url)


@app.route("/admin/applications/<int:application_id>/download-all")
@admin_required
@admin_section("applications")
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
@admin_section("reports")
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
    ("suppliers", "Suppliers"),
    ("inventory", "Inventory"),
    ("activity", "Admin activity"),
]
REPORT_PERIODS = [("30d", "Last 30 days"), ("90d", "Last 90 days"), ("12m", "Last 12 months"),
                  ("ytd", "This year"), ("all", "All time")]


def report_period(args):
    """The report window: a preset period, or a custom From/To date range (which wins when given).

    Returns a dict with `period` ("custom" for a date range), `start` and `end` (exclusive; None = open),
    the cleaned `date_from`/`date_to`, and `args` to carry the window in links.
    """
    dates = report_filters(args)
    date_from, date_to = dates["date_from"], dates["date_to"]
    if date_from or date_to:
        return {
            "period": "custom",
            "start": datetime.strptime(date_from, "%Y-%m-%d") if date_from else None,
            "end": datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1) if date_to else None,
            "date_from": date_from, "date_to": date_to,
            "args": {k: v for k, v in (("date_from", date_from), ("date_to", date_to)) if v},
        }
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
    return {"period": period, "start": start, "end": None, "date_from": "", "date_to": "", "args": {"period": period}}


def monthly_series(pairs, start, end=None):
    """pairs: iterable of (datetime, value). Returns (labels, values) with every month in range, gaps as 0."""
    totals = defaultdict(lambda: Decimal("0"))
    for when, value in pairs:
        if when:
            totals[when.strftime("%Y-%m")] += Decimal(value)
    if not totals:
        return [], []
    first = start.strftime("%Y-%m") if start else min(totals)
    last = (end - timedelta(days=1)).strftime("%Y-%m") if end else utcnow().strftime("%Y-%m")
    months = month_range(min(first, min(totals)), max(last, max(totals)))
    return months, [float(totals.get(m, 0)) for m in months]


def top_n(counter, n=10):
    items = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    return [k for k, _ in items], [float(v) for _, v in items]


def cell(text, href=None, badge=None, num=False, mono=False, money=None):
    return {"text": text, "href": href, "badge": badge, "num": num or money is not None, "mono": mono,
            "money": money}


def suppliers_report(start, end=None):
    query = User.query
    if start:
        query = query.filter(User.created_at >= start)
    if end:
        query = query.filter(User.created_at < end)
    suppliers = query.order_by(User.created_at.desc()).all()
    apps = users_by_email_applications(suppliers)
    ids = [u.id for u in suppliers]
    product_counts = dict(DB.session.query(Product.user_id, func.count()).filter(
        Product.user_id.in_(ids), Product.is_archived.is_(False)).group_by(Product.user_id).all()) if ids else {}

    by_category, by_province = defaultdict(int), defaultdict(int)
    for u in suppliers:
        application = apps.get(normalize_email(u.email))
        if application:
            for c in application.category_names:
                by_category[c] += 1
            by_province[application.province or "Not given"] += 1
    months, monthly = monthly_series(((u.created_at, 1) for u in suppliers), start, end)
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
        "columns": ["Supplier", "Supplier ID", "Registered", "Application", "Account", "Products"],
        "rows": [[
            cell(u.company_name, href=(url_for("admin_application_detail", application_id=apps[normalize_email(u.email)].id)
                                       if normalize_email(u.email) in apps else None)),
            cell(u.supplier_id or "—", mono=True),
            cell(u.created_at.strftime("%d %b %Y") if u.created_at else ""),
            (cell(status_label(apps[normalize_email(u.email)].status), badge=apps[normalize_email(u.email)].status)
             if normalize_email(u.email) in apps else cell("Not submitted")),
            cell("Active" if u.active else "Inactive", badge="active" if u.active else "inactive"),
            cell(str(product_counts.get(u.id, 0)), num=True),
        ] for u in suppliers],
    }


def users_by_email_applications(users):
    emails = [normalize_email(u.email) for u in users]
    if not emails:
        return {}
    return {normalize_email(a.email): a for a in SupplierApplication.query.filter(func.lower(SupplierApplication.email).in_(emails))}


def inventory_report(_start, _end=None):
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
            ("Listed products", len(listed), "visible in the catalogue"),
            ("Hidden", len(live) - len(listed), "in inventory, not listed"),
            ("Out of stock", len(out_of_stock), "listed but unavailable"),
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


ACTIVITY_LABELS = EVENT_LABELS


def activity_report(start, end=None):
    app_events = ApplicationEvent.query.filter(ApplicationEvent.actor_type == "admin")
    if start:
        app_events = app_events.filter(ApplicationEvent.created_at >= start)
    if end:
        app_events = app_events.filter(ApplicationEvent.created_at < end)
    events = [("application", e) for e in app_events.all()]
    events.sort(key=lambda pair: pair[1].created_at or datetime.min, reverse=True)

    def count(*actions):
        return sum(1 for _, e in events if e.action in actions)

    per_admin, per_action = defaultdict(int), defaultdict(int)
    for _, e in events:
        per_admin[e.actor_name or "Unknown"] += 1
        per_action[ACTIVITY_LABELS.get(e.action, e.action.replace("_", " ").capitalize())] += 1
    months, monthly = monthly_series(((e.created_at, 1) for _, e in events), start, end)
    applications = {a.id: a for a in SupplierApplication.query.filter(
        SupplierApplication.id.in_({e.application_id for kind, e in events if kind == "application"})).all()} if events else {}

    def subject(kind, e):
        a = applications.get(e.application_id)
        return cell(a.company_name if a else f"Application {e.application_id}",
                    href=url_for("admin_application_history", application_id=e.application_id))

    return {
        "kpis": [
            ("Admin actions", len(events), "recorded in the audit trail"),
            ("Decisions", count("status_changed"), "application status changes"),
            ("Assignments", count("assigned", "unassigned"), "assigned or unassigned"),
            ("Documents opened", count("document_viewed", "documents_downloaded"), "views and ZIP downloads"),
            ("Admins active", len(per_admin), "with at least one action"),
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


REPORT_BUILDERS = {"suppliers": suppliers_report,
                   "inventory": inventory_report, "activity": activity_report}
REPORT_DESCRIPTIONS = {
    "suppliers": "Supplier registrations, status and activity",
    "inventory": "What approved suppliers currently offer — a live snapshot",
    "activity": "What each admin has done, from the audit trail",
}


@app.route("/admin/reports/<kind>")
@admin_required
@admin_section("reports")
def admin_report(kind):
    if kind == "applications":
        return redirect(url_for("admin_reports", **request.args))
    if kind not in REPORT_BUILDERS:
        abort(404)
    window = report_period(request.args)
    report = REPORT_BUILDERS[kind](window["start"], window["end"])
    return render_template(
        "admin_report.html", admin=get_current_admin(), kind=kind, report=report,
        title=dict(REPORT_KINDS)[kind], description=REPORT_DESCRIPTIONS[kind],
        period=window["period"], periods=REPORT_PERIODS, report_kinds=REPORT_KINDS,
        date_from=window["date_from"], date_to=window["date_to"], range_args=window["args"],
    )


@app.route("/admin/reports/<kind>/export")
@admin_required
@admin_section("reports")
def admin_report_export(kind):
    if kind not in REPORT_BUILDERS:
        abort(404)
    window = report_period(request.args)
    report = REPORT_BUILDERS[kind](window["start"], window["end"])
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(report["columns"])
    for row in report["rows"]:
        writer.writerow([f"{c['money']:.2f}" if c.get("money") is not None else c["text"] for c in row])
    period = (f"{window['date_from'] or 'start'}_to_{window['date_to'] or 'today'}"
              if window["period"] == "custom" else window["period"])
    filename = f"{kind}_report_{period}_{datetime.now().strftime('%Y%m%d')}.csv"
    audit("report_exported", f"{dict(REPORT_KINDS).get(kind, kind)} report ({period})")
    DB.session.commit()
    return Response("﻿" + output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/admin/reports/export")
@admin_required
@admin_section("reports")
def admin_reports_export():
    applications = filtered_applications_query(report_filters(request.args)).order_by(SupplierApplication.created_at.asc()).all()
    audit("report_exported", f"Applications export ({len(applications)} rows)")
    DB.session.commit()
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
# Inventory (approved suppliers) and catalogue (admins)
# ---------------------------------------------------------------------------

def active_supplier_required(view_func):
    """Inventory is only for suppliers whose account is active (application approved)."""
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
        # Old product requests still reference it, so archive instead of deleting.
        product.is_archived = True
        product.is_listed = False
    else:
        DB.session.delete(product)
    DB.session.commit()
    flash(f"“{product.name}” removed from your inventory.", "success")
    return redirect(url_for("inventory"))


# ---- Admin: catalogue ----

def catalogue_query():
    return (Product.query.join(User, Product.user_id == User.id)
            .filter(Product.is_listed.is_(True), Product.is_archived.is_(False), User.active.is_(True)))


def supplier_locations_query():
    """(user id, city, province) for suppliers, taken from their approved application."""
    return (DB.session.query(User.id, SupplierApplication.city, SupplierApplication.province)
            .join(SupplierApplication, func.lower(SupplierApplication.email) == func.lower(User.email))
            .filter(SupplierApplication.status == "approved"))


@app.route("/admin/catalogue")
@admin_required
@admin_section("catalogue")
def admin_catalogue():
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    supplier_id = request.args.get("supplier", type=int)
    province = request.args.get("province", "").strip()
    query = catalogue_query()
    if search:
        like = f"%{search}%"
        query = query.filter(DB.or_(Product.name.ilike(like), Product.description.ilike(like), User.company_name.ilike(like)))
    if category:
        query = query.filter(Product.category == category)
    if supplier_id:
        query = query.filter(Product.user_id == supplier_id)
    if province:
        in_province = supplier_locations_query().filter(SupplierApplication.province == province)
        query = query.filter(User.id.in_(in_province.with_entities(User.id)))
    page = request.args.get("page", 1, type=int)
    pagination = query.order_by(Product.name).paginate(page=page, per_page=24, error_out=False)

    base = catalogue_query()
    base_user_ids = base.with_entities(Product.user_id).distinct()
    categories = sorted({c for (c,) in base.with_entities(Product.category).distinct() if c})
    suppliers = User.query.filter(User.id.in_(base_user_ids)).order_by(User.company_name).all()
    supplier_locations = supplier_locations_query().filter(User.id.in_(base_user_ids)).all()
    provinces = [p for p in PROVINCES if any(row.province == p for row in supplier_locations)]
    locations = {row.id: ", ".join(part for part in (row.city, row.province) if part) for row in supplier_locations}
    page_args = {k: v for k, v in {"search": search, "category": category, "supplier": supplier_id,
                                   "province": province}.items() if v}
    return render_template(
        "admin_catalogue.html", admin=get_current_admin(), products=pagination.items, pagination=pagination,
        search=search, category_filter=category, supplier_filter=supplier_id, categories=categories,
        suppliers=suppliers, province_filter=province, provinces=provinces, locations=locations, page_args=page_args,
    )


# ---------------------------------------------------------------------------
# ICT admin: user audit, email templates, deleting applications and directors
# ---------------------------------------------------------------------------

# What admins do on applications (recorded in each application's history) that belongs in the user audit.
ADMIN_EVENT_ACTIONS = ["assigned", "unassigned", "status_changed", "review_updated", "document_viewed",
                       "document_replaced", "documents_downloaded", "history_exported", "account_activated",
                       "account_deactivated", "registration_certificate_issued", "director_removed"]
ADMIN_ACTIVITY_LABELS = {**AUDIT_ACTIONS, **{action: EVENT_LABELS[action] for action in ADMIN_EVENT_ACTIONS}}
AUDIT_PER_PAGE = 50


class ListPagination:
    """The bits of Flask-SQLAlchemy's Pagination that the pagination macro uses, for a hand-built query."""

    def __init__(self, items, page, per_page, total):
        self.items, self.page, self.per_page, self.total = items, page, per_page, total
        self.pages = max(1, -(-total // per_page))
        self.has_prev, self.has_next = page > 1, page < self.pages
        self.prev_num, self.next_num = page - 1, page + 1

    def iter_pages(self, left_edge=1, right_edge=1, left_current=2, right_current=2):
        last = 0
        for number in range(1, self.pages + 1):
            if (number <= left_edge or number > self.pages - right_edge
                    or self.page - left_current <= number <= self.page + right_current):
                if last + 1 != number:
                    yield None
                yield number
                last = number


def audit_filters(args):
    dates = report_filters(args)
    return {
        "search": args.get("search", "").strip(),
        "admin": args.get("admin", type=int) or "",
        "action": args.get("action", "") if args.get("action") in ADMIN_ACTIVITY_LABELS else "",
        "date_from": dates["date_from"],
        "date_to": dates["date_to"],
    }


def admin_activity_query(filters):
    """Everything admins have done, newest first: sign-ins, exports and ICT actions (AuditLog) together with their
    actions on applications (ApplicationEvent). Supplier and system activity is left out."""
    sign_ins = (select(AuditLog.created_at.label("created_at"), AuditLog.actor_id.label("actor_id"),
                       AuditLog.actor_name.label("actor_name"), AuditLog.actor_email.label("actor_email"),
                       AuditLog.action.label("action"), AuditLog.detail.label("detail"),
                       AuditLog.ip_address.label("ip_address"), null().label("application_id"),
                       literal("").label("company_name"))
                .where(AuditLog.actor_type == "admin"))
    on_applications = (select(ApplicationEvent.created_at, ApplicationEvent.actor_id, ApplicationEvent.actor_name,
                              ApplicationEvent.actor_email, ApplicationEvent.action, ApplicationEvent.summary,
                              ApplicationEvent.ip_address, ApplicationEvent.application_id, SupplierApplication.company_name)
                       .join(SupplierApplication, ApplicationEvent.application_id == SupplierApplication.id)
                       .where(ApplicationEvent.actor_type == "admin", ApplicationEvent.action.in_(ADMIN_EVENT_ACTIONS)))
    if filters["search"]:
        like = f"%{filters['search']}%"
        sign_ins = sign_ins.where(DB.or_(AuditLog.actor_email.ilike(like), AuditLog.actor_name.ilike(like),
                                         AuditLog.detail.ilike(like), AuditLog.ip_address.ilike(like)))
        on_applications = on_applications.where(DB.or_(
            ApplicationEvent.actor_email.ilike(like), ApplicationEvent.actor_name.ilike(like),
            ApplicationEvent.summary.ilike(like), ApplicationEvent.ip_address.ilike(like),
            SupplierApplication.company_name.ilike(like)))
    if filters["admin"]:
        sign_ins = sign_ins.where(AuditLog.actor_id == filters["admin"])
        on_applications = on_applications.where(ApplicationEvent.actor_id == filters["admin"])
    if filters["action"]:
        sign_ins = sign_ins.where(AuditLog.action == filters["action"] if filters["action"] in AUDIT_ACTIONS else false())
        on_applications = on_applications.where(ApplicationEvent.action == filters["action"])
    if filters["date_from"]:
        start = datetime.strptime(filters["date_from"], "%Y-%m-%d")
        sign_ins = sign_ins.where(AuditLog.created_at >= start)
        on_applications = on_applications.where(ApplicationEvent.created_at >= start)
    if filters["date_to"]:
        end = datetime.strptime(filters["date_to"], "%Y-%m-%d") + timedelta(days=1)
        sign_ins = sign_ins.where(AuditLog.created_at < end)
        on_applications = on_applications.where(ApplicationEvent.created_at < end)
    return union_all(sign_ins, on_applications).subquery()


def admin_activity_rows(filters, limit, offset=0):
    activity = admin_activity_query(filters)
    rows = DB.session.execute(select(activity).order_by(activity.c.created_at.desc()).limit(limit).offset(offset)).all()
    total = DB.session.execute(select(func.count()).select_from(activity)).scalar()
    return rows, total


@app.route("/admin/ict/audit")
@admin_required
@admin_section("ict")
def admin_user_audit():
    view = "accounts" if request.args.get("view") == "accounts" else "activity"
    filters = audit_filters(request.args)
    page_args = {k: v for k, v in filters.items() if v}
    page = max(1, request.args.get("page", 1, type=int))
    rows, total = admin_activity_rows(filters, AUDIT_PER_PAGE, (page - 1) * AUDIT_PER_PAGE)
    pagination = ListPagination(rows, page, AUDIT_PER_PAGE, total)

    role_order = list(ADMIN_ROLES)
    admins = sorted(Admin.query.all(), key=lambda a: (role_order.index(a.role) if a.role in role_order else 99,
                                                      (a.name or a.email).lower()))
    accounts = []
    if view == "accounts":
        last_login = dict(DB.session.query(AuditLog.actor_id, func.max(AuditLog.created_at))
                          .filter(AuditLog.action == "admin_login").group_by(AuditLog.actor_id))
        actions = dict(DB.session.query(ApplicationEvent.actor_id, func.count())
                       .filter(ApplicationEvent.actor_type == "admin", ApplicationEvent.action.in_(ADMIN_EVENT_ACTIONS))
                       .group_by(ApplicationEvent.actor_id))
        accounts = [(a, last_login.get(a.id), actions.get(a.id, 0)) for a in admins]
    selected_admin = next((a for a in admins if a.id == filters["admin"]), None)
    return render_template(
        "admin_user_audit.html", admin=get_current_admin(), view=view, entries=rows, pagination=pagination,
        filters=filters, page_args=page_args, activity_labels=ADMIN_ACTIVITY_LABELS, accounts=accounts,
        admins=admins, selected_admin=selected_admin,
    )


@app.route("/admin/ict/audit/export")
@admin_required
@admin_section("ict")
def admin_user_audit_export():
    filters = audit_filters(request.args)
    rows, _ = admin_activity_rows(filters, 50000)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Date & time (UTC)", "Admin", "Email", "Action", "Application", "Detail", "IP address"])
    for row in rows:
        writer.writerow([row.created_at.strftime("%Y-%m-%d %H:%M:%S") if row.created_at else "", row.actor_name,
                         row.actor_email, ADMIN_ACTIVITY_LABELS.get(row.action, row.action),
                         f"{row.company_name} (#{row.application_id})" if row.application_id else "",
                         row.detail, row.ip_address])
    audit("audit_exported", f"{len(rows)} rows")
    DB.session.commit()
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename=user_audit_{date.today().isoformat()}.csv"})


@app.route("/admin/ict/emails")
@admin_required
@admin_section("ict")
def admin_email_templates():
    saved = {t.key: t for t in EmailTemplate.query.all()}
    return render_template("admin_email_templates.html", admin=get_current_admin(), templates=EMAIL_TEMPLATES, saved=saved)


def unknown_placeholders(key, *texts):
    allowed = set(EMAIL_TEMPLATES[key]["placeholders"])
    return sorted({name for text in texts for name in PLACEHOLDER_RE.findall(text)} - allowed)


@app.route("/admin/ict/emails/<key>", methods=["GET", "POST"])
@admin_required
@admin_section("ict")
def admin_email_template_edit(key):
    if key not in EMAIL_TEMPLATES:
        abort(404)
    template = EMAIL_TEMPLATES[key]
    saved = DB.session.get(EmailTemplate, key)
    subject = saved.subject if saved else template["subject"]
    body = saved.body if saved else template["body"]

    if request.method == "POST":
        subject = request.form.get("subject", "").strip()
        body = request.form.get("body", "").strip()
        error = None
        if not subject or not body:
            error = "Please enter both a subject and a message."
        elif len(subject) > 200:
            error = "The subject can be at most 200 characters."
        elif unknown_placeholders(key, subject, body):
            unknown = ", ".join("{" + name + "}" for name in unknown_placeholders(key, subject, body))
            error = f"This email can't use {unknown}. Use only the placeholders listed."
        if error:
            flash(error, "error")
        else:
            me = get_current_admin()
            if (subject, body) == (template["subject"], template["body"]):
                if saved:
                    DB.session.delete(saved)
            elif saved:
                saved.subject, saved.body, saved.updated_by = subject, body, me.email
                saved.updated_at = utcnow()
            else:
                DB.session.add(EmailTemplate(key=key, subject=subject, body=body, updated_by=me.email))
            audit("email_template_updated", template["name"])
            DB.session.commit()
            flash(f"“{template['name']}” saved. New emails use this text straight away.", "success")
            return redirect(url_for("admin_email_template_edit", key=key))

    samples = {name: example for name, (_, example) in template["placeholders"].items()}
    preview = render_email(fill_placeholders(subject, samples), fill_placeholders(body, samples), "Jane Dube")
    return render_template(
        "admin_email_template_edit.html", admin=get_current_admin(), key=key, template=template, saved=saved,
        subject=subject, body=body, preview=preview,
    )


@app.route("/admin/ict/emails/<key>/reset", methods=["POST"])
@admin_required
@admin_section("ict")
def admin_email_template_reset(key):
    if key not in EMAIL_TEMPLATES:
        abort(404)
    saved = DB.session.get(EmailTemplate, key)
    if saved:
        DB.session.delete(saved)
        audit("email_template_reset", EMAIL_TEMPLATES[key]["name"])
        DB.session.commit()
    flash(f"“{EMAIL_TEMPLATES[key]['name']}” is back to the default text.", "success")
    return redirect(url_for("admin_email_template_edit", key=key))


@app.route("/admin/ict/applications/<int:application_id>/delete", methods=["POST"])
@admin_required
@admin_section("ict")
def admin_delete_application(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    if request.form.get("confirm_name", "").strip().lower() != (application.company_name or "").strip().lower():
        flash("Type the company name exactly as shown to confirm the deletion.", "error")
        return redirect(url_for("admin_application_detail", application_id=application.id))

    supplier = get_application_supplier(application)
    documents = SupplierDocument.query.filter_by(application_id=application.id).all()
    detail = (f"{application.company_name} (#{application.id}, {status_label(application.status)}, {application.email}): "
              f"{len(documents)} document(s), {len(application.directors)} director(s)")
    for document in documents:
        remove_upload(document.file_path)
        DB.session.delete(document)
    ApplicationEvent.query.filter_by(application_id=application.id).delete()
    Notification.query.filter_by(related_application_id=application.id).update({"related_application_id": None})
    DB.session.delete(application)  # directors and categories go with it (cascade)
    DB.session.flush()
    # A supplier with no application left can't stay active (their products would stay in the catalogue).
    if supplier and supplier.active and not supplier_applications_query(supplier).first():
        supplier.active = False
        detail += "; supplier account deactivated"
    audit("application_deleted", detail)
    DB.session.commit()
    flash(f"{application.company_name}'s application was deleted.", "success")
    return redirect(url_for("admin_applications"))


@app.route("/admin/ict/applications/<int:application_id>/directors/<int:director_id>/delete", methods=["POST"])
@admin_required
@admin_section("ict")
def admin_delete_director(application_id, director_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    director = DB.session.get(SupplierDirector, director_id)
    if not director or director.application_id != application.id:
        abort(404)
    who = f"{director.initials_surname} ({director.role}, {director.id_number})"
    DB.session.delete(director)
    log_event(application, "director_removed", summary=f"Removed {director.initials_surname}",
              changes=[{"field": "Director / shareholder", "old": who, "new": ""}])
    audit("director_deleted", f"{who} from {application.company_name} (#{application.id})")
    DB.session.commit()
    flash(f"{director.initials_surname} was removed from {application.company_name}'s application.", "success")
    return redirect(url_for("admin_application_detail", application_id=application.id))


# ---------------------------------------------------------------------------
# Certificate expiry: reminders, deactivation and renewal
# ---------------------------------------------------------------------------

# Applications whose certificates we watch (not drafts, and not declined ones).
EXPIRY_WATCHED_STATUSES = {"submitted", "pending_review", "under_review", "returned_for_update", "approved", "on_file"}
_NOTICE_RANK = {"": 0, **{str(days): i for i, days in enumerate(EXPIRY_REMINDER_DAYS, start=1)},
                "expired": len(EXPIRY_REMINDER_DAYS) + 1}


def due_expiry_notice(document, today):
    """The notice this document should have had by now ("30", "7", "expired") or "" for none."""
    days_left = (document.expiry_date - today).days
    if days_left < 0:
        return "expired"
    for days in sorted(EXPIRY_REMINDER_DAYS):
        if days_left <= days:
            return str(days)
    return ""


def claim_expiry_notice(document, notice):
    """Atomically move a document to `notice`; False if another worker already did. Keeps notices one-off."""
    claimed = SupplierDocument.query.filter_by(id=document.id, expiry_notice=document.expiry_notice).update(
        {SupplierDocument.expiry_notice: notice}, synchronize_session=False)
    return claimed == 1


def check_certificate_expiry(today=None):
    """Send due expiry reminders and deactivate suppliers whose certificates have expired. Safe to re-run.

    Returns (reminders sent, accounts deactivated).
    """
    today = today or date.today()
    reminders = deactivated = 0
    documents = (SupplierDocument.query.join(SupplierApplication, SupplierDocument.application_id == SupplierApplication.id)
                 .filter(SupplierDocument.document_type.in_(list(DATED_DOCUMENTS)),
                         SupplierDocument.expiry_date.isnot(None),
                         SupplierApplication.status.in_(EXPIRY_WATCHED_STATUSES))
                 .all())
    for document in documents:
        notice = due_expiry_notice(document, today)
        if _NOTICE_RANK[notice] <= _NOTICE_RANK.get(document.expiry_notice, 0) or not claim_expiry_notice(document, notice):
            continue
        application = DB.session.get(SupplierApplication, document.application_id)
        supplier = get_application_supplier(application)
        name = DATED_DOCUMENTS[document.document_type]
        expires = document.expiry_date.strftime("%d %b %Y")
        renew_url = url_for("supplier_certificates", _external=True)

        if notice != "expired":
            days_left = (document.expiry_date - today).days
            when = "today" if days_left == 0 else f"in {days_left} day{'' if days_left == 1 else 's'}"
            consequence = (" If it expires before you do, your supplier account will be deactivated until it is renewed."
                           if supplier and supplier.active else "")
            if supplier:
                notify_supplier(supplier, *email_text("certificate_expiring", certificate=name, company_name=application.company_name,
                                                      when=when, expiry_date=expires, consequence=consequence),
                                application, action_url=renew_url, action_label="Upload renewed certificate")
            log_event(application, "certificate_reminder", summary=f"{document.document_type}: expires {expires} ({notice}-day reminder)",
                      actor="system")
            reminders += 1
            continue

        was_active = bool(supplier and supplier.active)
        if was_active:
            supplier.active = False
            application.expiry_suspended = True
            deactivated += 1
            log_event(application, "account_deactivated", summary=f"Supplier account deactivated: {document.document_type} expired",
                      changes=[{"field": "Account", "old": "Active", "new": "Inactive"}], actor="system")
        log_event(application, "certificate_expired", summary=f"{document.document_type} expired on {expires}", actor="system")
        if supplier:
            notify_supplier(
                supplier, *email_text("certificate_expired_deactivated" if was_active else "certificate_expired",
                                      certificate=name, company_name=application.company_name, expiry_date=expires),
                application, action_url=renew_url, action_label="Upload renewed certificate")
        notify_admins(f"Certificate expired: {application.company_name}",
                      f"{application.company_name}'s {name} expired on {expires}."
                      + (" Their supplier account was deactivated automatically." if was_active else ""), application)
    DB.session.commit()
    return reminders, deactivated


def restore_after_renewal(application, supplier):
    """Reactivate an account that was deactivated only because a certificate expired, once none are expired."""
    if not (application.expiry_suspended and supplier and application.status == "approved"):
        return False
    certificates = SupplierDocument.query.filter(SupplierDocument.application_id == application.id,
                                                 SupplierDocument.document_type.in_(list(DATED_DOCUMENTS))).all()
    if any(doc.expiry_state == "expired" for doc in certificates):
        return False
    application.expiry_suspended = False
    supplier.active = True
    log_event(application, "account_activated", summary="Supplier account reactivated after certificate renewal",
              changes=[{"field": "Account", "old": "Inactive", "new": "Active"}])
    notify_supplier(supplier, *email_text("account_reactivated"), application)
    return True


def run_certificate_checks():
    """Run the expiry check outside a request (scheduler or CLI), with links built from PUBLIC_BASE_URL."""
    with app.test_request_context(base_url=PUBLIC_BASE_URL or "http://localhost:5000"):
        try:
            reminders, deactivated = check_certificate_expiry()
            if reminders or deactivated:
                app.logger.info("Certificate check: %s reminder(s), %s account(s) deactivated", reminders, deactivated)
            return reminders, deactivated
        except Exception as exc:  # noqa: BLE001 — never let the background loop die
            DB.session.rollback()
            app.logger.error("Certificate check failed: %s", exc)
            return 0, 0


_certificate_scheduler_started = False


@app.before_request
def start_certificate_scheduler():
    """Start the background expiry check once per web process (not for CLI commands or tests)."""
    global _certificate_scheduler_started
    if _certificate_scheduler_started or CERT_CHECK_INTERVAL_HOURS <= 0 or app.config.get("TESTING"):
        return
    _certificate_scheduler_started = True
    if not PUBLIC_BASE_URL:
        app.logger.warning("PUBLIC_BASE_URL is not set; links in certificate expiry emails will point at localhost.")

    def loop():
        time.sleep(60)  # let the app finish starting
        while True:
            run_certificate_checks()
            time.sleep(CERT_CHECK_INTERVAL_HOURS * 3600)

    threading.Thread(target=loop, name="certificate-expiry-check", daemon=True).start()


@app.cli.command("check-certificates")
def check_certificates_command():
    """Send certificate expiry reminders and deactivate suppliers with expired certificates."""
    reminders, deactivated = run_certificate_checks()
    click.echo(f"{reminders} reminder(s) sent, {deactivated} account(s) deactivated.")


# ---------------------------------------------------------------------------
# Supplier registration certificate
# ---------------------------------------------------------------------------

def approval_date(application):
    """When the application was approved. Older approvals predate approved_at, so use the audit trail."""
    if application.approved_at:
        return application.approved_at
    event = (ApplicationEvent.query.filter_by(application_id=application.id, to_status="approved")
             .order_by(ApplicationEvent.created_at.desc()).first())
    return event.created_at if event else (application.updated_at or application.created_at or utcnow())


def certificate_details(application, supplier):
    registered = approval_date(application).date()
    rows = [(ac.category, ac.category_detail) for ac in application.application_categories if ac.category]
    if not rows:
        rows = [(application.supplier_category, application.supplier_category_detail)]
    return {
        "company_name": application.registered_vendor_name or application.company_name,
        "supplier_number": supplier.supplier_id if supplier and supplier.supplier_id else "—",
        "services": " / ".join(category for category, _ in rows if category),
        "service_details": " / ".join(detail for _, detail in rows if detail),
        "registered": registered,
        "valid_until": registered + timedelta(days=CERTIFICATE_VALID_DAYS),
        "signatory_name": CERTIFICATE_SIGNATORY_NAME,
        "signatory_title": CERTIFICATE_SIGNATORY_TITLE,
        "footer": CERTIFICATE_FOOTER,
        "has_signature": os.path.exists(os.path.join(app.static_folder, "images", "certificate_signature.png")),
    }


def render_certificate(application, supplier, back_url):
    details = certificate_details(application, supplier)
    filename = secure_filename(f"{details['company_name']} supplier certificate") or "supplier_certificate"
    return render_template("registration_certificate.html", cert=details, back_url=back_url, filename=filename)


@app.route("/certificate")
@login_required
def supplier_registration_certificate():
    user = get_current_user()
    application = get_user_application(user)
    if not application or application.status != "approved":
        flash("Your supplier certificate is available once your application has been approved.", "error")
        return redirect(url_for("dashboard"))
    return render_certificate(application, user, url_for("supplier_certificates"))


@app.route("/admin/applications/<int:application_id>/certificate")
@admin_required
@admin_section("applications")
def admin_registration_certificate(application_id):
    application = DB.get_or_404(SupplierApplication, application_id)
    detail_url = url_for("admin_application_detail", application_id=application.id)
    if application.status != "approved":
        flash("A supplier certificate is only issued for approved applications.", "error")
        return redirect(detail_url)
    return render_certificate(application, get_application_supplier(application), detail_url)


@app.route("/certificates", methods=["GET", "POST"])
@login_required
def supplier_certificates():
    """Upload renewed VAT / B-BBEE certificates at any time, including after approval."""
    user = get_current_user()
    application = get_user_application(user)
    if not application or application.status == "draft":
        flash("Submit your supplier application first.", "error")
        return redirect(url_for("dashboard"))
    slots = {field: doc_type for field, doc_type in UPLOAD_SLOTS.items() if doc_type in DATED_DOCUMENTS}
    documents = SupplierDocument.query.filter(SupplierDocument.application_id == application.id,
                                              SupplierDocument.document_type.in_(list(DATED_DOCUMENTS))).all()

    if request.method == "POST":
        try:
            uploads = collect_uploads(request.files, slots)
        except UploadError as exc:
            flash(str(exc), "error")
            return redirect(url_for("supplier_certificates"))
        if not uploads:
            flash("Please choose the renewed certificate (PDF) to upload.", "error")
            return redirect(url_for("supplier_certificates"))
        dates, date_error = parse_document_dates(request.form, slots, documents, uploads, uploads_only=True)
        if date_error:
            flash(date_error + " Nothing was uploaded; please choose the file again.", "error")
            return redirect(url_for("supplier_certificates"))

        previous = {doc.document_type: doc.original_filename for doc in documents}
        for doc_type, file_storage in uploads:
            store_document(user.id, application.id, doc_type, file_storage)
        DB.session.flush()
        documents = SupplierDocument.query.filter(SupplierDocument.application_id == application.id,
                                                  SupplierDocument.document_type.in_(list(DATED_DOCUMENTS))).all()
        changes = [{"field": f"Document: {doc_type}", "old": previous.get(doc_type, ""), "new": fs.filename}
                   for doc_type, fs in uploads]
        changes += apply_document_dates(documents, dates)
        all_docs = SupplierDocument.query.filter_by(application_id=application.id).all()
        application.documents_status = "pending" if missing_required_documents(all_docs) else "complete"
        log_event(application, "certificates_renewed",
                  summary="Renewed: " + ", ".join(DATED_DOCUMENTS[doc_type] for doc_type, _ in uploads), changes=changes)
        notify_admins(f"Certificate renewed: {application.company_name}",
                      f"{application.company_name} uploaded a renewed "
                      + " and ".join(DATED_DOCUMENTS[doc_type] for doc_type, _ in uploads) + ".", application)
        reactivated = restore_after_renewal(application, user)
        DB.session.commit()
        flash("Thank you, your renewed certificate has been uploaded."
              + (" Your supplier account is active again." if reactivated else ""), "success")
        return redirect(url_for("supplier_certificates"))

    registration = certificate_details(application, user) if application.status == "approved" else None
    return render_template("supplier_certificates.html", user=user, application=application, registration=registration,
                           documents={doc.document_type: doc for doc in documents}, slots=slots)


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
        "terms_version": "VARCHAR(20) NULL",
        "terms_accepted_at": "DATETIME NULL",
    })
    name_col = _columns("users").get("name")
    if name_col is not None and name_col[4] is None:
        DB.session.execute(text("ALTER TABLE users MODIFY COLUMN name VARCHAR(100) NOT NULL DEFAULT ''"))

    _add_missing_columns("admins", {"role": "VARCHAR(20) NOT NULL DEFAULT 'super'"})

    _add_missing_columns("supplier_applications", {
        "assigned_admin_id": "INT NULL",
        "assigned_at": "DATETIME NULL",
        "supplier_category_detail": "VARCHAR(255) NOT NULL DEFAULT ''",
        "expiry_suspended": "BOOLEAN NOT NULL DEFAULT FALSE",
        "approved_at": "DATETIME NULL",
        "terms_version": "VARCHAR(20) NULL",
        "terms_accepted_at": "DATETIME NULL",
        **{field: f"VARCHAR({SupplierApplication.__table__.c[field].type.length}) DEFAULT ''" for field in PROFILE_FIELDS},
    })

    _add_missing_columns("supplier_documents", {"issue_date": "DATE NULL", "expiry_date": "DATE NULL",
                                                "expiry_notice": "VARCHAR(10) NOT NULL DEFAULT ''"})

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
# Cebelihle: the portal's built-in assistant, for suppliers and admins
# ---------------------------------------------------------------------------
# Rule-based: each question is matched by keywords against a fixed knowledge base about the portal (no AI service).
# Answers about "my" records are looked up for whoever is signed in: a supplier only ever sees their own account,
# and an admin only gets answers for the sections their role can open.

CHAT_MAX_MESSAGE_CHARS = 500
CONTACT_DETAILS = "the Call Centre on 031 208 9102 or supplier_onboarding@icebolethu.co.za"
REFER = f"The procurement team will need to help you with that. Please contact {CONTACT_DETAILS}."
chat_limiter = RateLimiter(limit=60, window_seconds=10 * 60)


def _fmt_date(value):
    return value.strftime("%d %b %Y") if value else ""


def _bullets(items):
    return "\n".join(f"- {item}" for item in items)


def _chat_words(text):
    text = (text or "").lower().replace("b-bbee", "bbbee").replace("'", "").replace("’", "")
    return " " + re.sub(r"[^a-z0-9#]+", " ", text).strip() + " "


class Intent:
    """One thing Cebelihle knows. `answer` is text, or a function of the signed-in user/admin returning text."""

    def __init__(self, key, keywords, answer, section=None, suggestions=(), with_message=False):
        self.key = key
        self.with_message = with_message  # answer(who, message) instead of answer(who)
        self.keywords = [_chat_words(k).strip() for k in keywords]
        self.answer = answer
        self.section = section          # admin section the role needs, if any
        self.suggestions = list(suggestions)

    def score(self, words):
        # Longer phrases are more specific, so they count for more.
        return sum(len(k.split()) for k in self.keywords if f" {k} " in words)

    def reply(self, who, message=""):
        if not callable(self.answer):
            return self.answer
        return self.answer(who, message) if self.with_message else self.answer(who)


# ---- Supplier answers that read the supplier's own records ----

def _supplier_application(user):
    return get_user_application(user)


def _answer_my_status(user):
    application = _supplier_application(user)
    if not application:
        return ("You haven't submitted an application yet. Start at /onboarding/profile: it takes 4 steps "
                "(company profile, category, documents, review).")
    status = application.status
    lines = [f"Your application for **{application.company_name}** is **{status_label(status)}** "
             f"(submitted {_fmt_date(application.created_at)})."]
    next_steps = {
        "submitted": "It's waiting for the procurement team to review it. You'll be notified when the status changes.",
        "pending_review": "It's waiting for the procurement team to review it. You'll be notified when the status changes.",
        "under_review": "The procurement team is reviewing it now. You'll be notified when there's a decision.",
        "returned_for_update": f"Please fix what the comments ask for, then save and resubmit at /applications/{application.id}/edit.",
        "approved": "Congratulations, you're an approved supplier. Keep your certificates up to date and list your products at /inventory.",
        "on_file": "Your documents and information are in order. We'll keep your details on file and contact you when your services are needed.",
        "declined": f"If your circumstances change, you can edit and resubmit at /applications/{application.id}/edit.",
    }
    lines.append(next_steps.get(status, ""))
    if application.review_comments and status in {"returned_for_update", "declined", "on_file", "under_review", "approved"}:
        lines.append(f"Comments from the procurement team: “{application.review_comments}”")
    return "\n".join(line for line in lines if line)


def _answer_my_documents(user):
    application = _supplier_application(user)
    documents = documents_for_application(application, user) if application else pending_documents(user)
    missing = missing_required_documents(documents)
    if not missing:
        return ("All 7 required documents are uploaded. "
                + (f"You can replace a document at /applications/{application.id}/edit." if application
                   else "You can submit your application at /onboarding/review."))
    where = f"/applications/{application.id}/edit" if application else "/onboarding/documents"
    return f"You still need to upload {len(missing)} required document(s):\n{_bullets(missing)}\nUpload them at {where} (PDF only)."


def _answer_my_certificates(user):
    application = _supplier_application(user)
    documents = [d for d in (documents_for_application(application, user) if application else [])
                 if d.document_type in DATED_DOCUMENTS and d.expiry_date]
    general = ("Reminders are sent 30 and 7 days before your SARS VAT or B-BBEE certificate expires. If one expires, "
               "your account is deactivated until you upload the renewed certificate at /certificates, which "
               "reactivates it automatically.")
    if not documents:
        return general
    state_words = {"expired": "**expired**", "expiring": "expires soon", "valid": "valid"}
    rows = [f"{DATED_DOCUMENTS[d.document_type][:1].upper()}{DATED_DOCUMENTS[d.document_type][1:]}: {state_words.get(d.expiry_state, '')} "
            f"(expiry date {_fmt_date(d.expiry_date)})" for d in documents]
    return "Your certificates:\n" + _bullets(rows) + "\n" + general


def _answer_my_account(user):
    application = _supplier_application(user)
    if user.active:
        return (f"Your supplier account ({user.supplier_id}) is **active**. You can manage your products at /inventory "
                "and view your registration certificate at /certificate.")
    if application and application.expiry_suspended:
        return ("Your account is **inactive** because a certificate expired. Upload the renewed certificate at "
                "/certificates and your account will be reactivated automatically.")
    if application and application.status in {"submitted", "pending_review", "under_review", "returned_for_update"}:
        return "Your account becomes active once your application is approved. " + _answer_my_status(user)
    if not application:
        return "Your account becomes active once your application is approved. Start your application at /onboarding/profile."
    return f"Your supplier account is **inactive**. {REFER}"


def _answer_registration_certificate(user):
    application = _supplier_application(user)
    if application and application.status == "approved":
        valid = certificate_details(application, user)["valid_until"]
        return (f"Your Certificate of Supplier Registration is at /certificate (valid until {_fmt_date(valid)}). "
                "You can print it or save it as a PDF.")
    return ("The Certificate of Supplier Registration is issued when your application is approved. "
            "It's valid for 12 months and appears at /certificate.")


def _answer_my_products(user):
    if not user.active:
        return "Your inventory becomes available once your application is approved and your account is active."
    total = Product.query.filter_by(user_id=user.id, is_archived=False).count()
    listed = Product.query.filter_by(user_id=user.id, is_archived=False, is_listed=True).count()
    return (f"You have {total} product(s) in your inventory, {listed} of them listed for the procurement team. "
            "Add or update products, prices, stock and lead times at /inventory.")


def _answer_why_decision(user):
    application = _supplier_application(user)
    comments = (f"The comments from the procurement team are: “{application.review_comments}” "
                if application and application.review_comments else "")
    return comments + f"For more detail about a decision, {REFER[0].lower()}{REFER[1:]}"


# ---- Admin answers that read live data (only for roles that can open the section) ----

def _answer_admin_counts(_admin):
    counts = dict(DB.session.query(SupplierApplication.status, func.count()).group_by(SupplierApplication.status).all())
    unassigned = SupplierApplication.query.filter(SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES),
                                                  SupplierApplication.assigned_admin_id.is_(None)).count()
    rows = [f"{status_label(s)}: {counts.get(s, 0)}" for s in STATUSES if s != "draft"]
    return ("Applications by status:\n" + _bullets(rows)
            + f"\n**{sum(counts.get(s, 0) for s in AWAITING_REVIEW_STATUSES)}** are awaiting review, "
              f"{unassigned} of them unassigned. Open them at /admin/applications.")


def _answer_admin_expiring(_admin):
    cutoff = date.today() + timedelta(days=EXPIRY_WARNING_DAYS)
    rows = (DB.session.query(SupplierDocument, SupplierApplication)
            .join(SupplierApplication, SupplierDocument.application_id == SupplierApplication.id)
            .filter(SupplierDocument.document_type.in_(list(DATED_DOCUMENTS)), SupplierDocument.expiry_date.isnot(None),
                    SupplierDocument.expiry_date <= cutoff, SupplierApplication.status.in_(list(EXPIRY_WATCHED_STATUSES)))
            .order_by(SupplierDocument.expiry_date).limit(15).all())
    if not rows:
        return f"No VAT or B-BBEE certificates have expired or expire in the next {EXPIRY_WARNING_DAYS} days."
    items = [f"{a.company_name} (#{a.id}): {DATED_DOCUMENTS[d.document_type]} "
             f"{'expired' if d.expiry_state == 'expired' else 'expires'} {_fmt_date(d.expiry_date)}" for d, a in rows]
    return f"Expired or expiring within {EXPIRY_WARNING_DAYS} days:\n" + _bullets(items)


def _answer_admin_suppliers(_admin):
    active = User.query.filter_by(active=True).count()
    return (f"There are **{active}** active supplier accounts out of {User.query.count()} registered. "
            "See and filter them at /admin/suppliers.")


def admin_application_reply(application_id):
    application = DB.session.get(SupplierApplication, application_id)
    if not application:
        return f"There's no application #{application_id}."
    owner = application.assigned_admin
    flagged = director_matches([application]).get(application.id)
    lines = [f"**{application.company_name}** (#{application.id}) is **{status_label(application.status)}**, "
             f"submitted {_fmt_date(application.created_at)}.",
             f"Category: {', '.join(application.category_names) or '—'}",
             f"Assigned to: {(owner.name or owner.email) if owner else 'nobody yet'}",
             f"Documents: {(application.documents_status or 'pending').title()}"]
    if flagged:
        lines.append("**Director match**: someone on this application is already on another supplier, so it can only be declined.")
    lines.append(f"Open it at /admin/applications/{application.id}")
    return "\n".join(lines)


# Words people use for each supplier category, so "do you need caterers?" finds Catering.
CATEGORY_WORDS = {
    "Catering": ["catering", "caterer", "caterers", "food", "meals"],
    "Body Storage, Cold-Room and other Burial Services": ["body storage", "cold room", "coldroom", "mortuary", "burial services"],
    "Consulting": ["consulting", "consultant", "consultants", "consultancy"],
    "Caskets and Tombstones": ["casket", "caskets", "coffin", "coffins", "tombstone", "tombstones"],
    "Livestock": ["livestock", "cattle", "cows", "goats", "sheep"],
    "Flowers, crosses and plaques": ["flowers", "florist", "crosses", "plaques", "wreath", "wreaths"],
    "ICT Equipment, Media and Electronic Devices": ["ict", "computers", "laptops", "electronics", "it equipment", "media"],
    "Tents, Draping and Décor": ["tents", "tent", "draping", "decor", "decoration", "chairs"],
    "Funeral Vehicles (Hearse / Family Car)": ["hearse", "hearses", "family car", "funeral vehicles", "vehicles", "cars"],
    "Lowering Device": ["lowering device", "lowering devices"],
    "Mobile toilet": ["mobile toilet", "mobile toilets", "toilets", "portable toilets"],
}
CATEGORY_KEYWORDS = sorted({word for words in CATEGORY_WORDS.values() for word in words})


def _answer_category_question(_user, message):
    words = _chat_words(message)
    found = [value for value, keys in CATEGORY_WORDS.items() if any(f" {_chat_words(k).strip()} " in words for k in keys)]
    if not found:
        return "Supplier categories:\n" + _bullets(c["value"] for c in SUPPLIER_CATEGORIES)
    groups = {c["value"]: c["group"].title() for c in SUPPLIER_CATEGORIES}
    names = " and ".join(f"**{value}** ({groups.get(value, '')})" for value in found)
    return (f"Yes, {names} {'is a supplier category' if len(found) == 1 else 'are supplier categories'}. Choose it on "
            "step 2 of your application (/onboarding/category). If what you supply isn't listed, choose **Other** "
            "and describe it.")


def _answer_my_application_page(user):
    application = _supplier_application(user)
    if not application:
        return "You haven't submitted an application yet. Start at /onboarding/profile."
    return (f"Your submitted application, with all its details and documents, is at /applications/{application.id}. "
            "All your applications are listed at /apps.")


def _answer_admin_my_queue(admin):
    rows = (SupplierApplication.query.filter(SupplierApplication.assigned_admin_id == admin.id,
                                             SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES + ["returned_for_update"]))
            .order_by(SupplierApplication.created_at).limit(15).all())
    if not rows:
        return "You have no open applications assigned to you. Unassigned ones are on /admin/applications."
    return (f"You have {len(rows)} open application(s) assigned to you:\n"
            + _bullets(f"{a.company_name} (#{a.id}): {status_label(a.status)}" for a in rows))


def _answer_admin_unassigned(_admin):
    rows = (SupplierApplication.query.filter(SupplierApplication.status.in_(AWAITING_REVIEW_STATUSES),
                                             SupplierApplication.assigned_admin_id.is_(None))
            .order_by(SupplierApplication.created_at).limit(15).all())
    if not rows:
        return "Every application awaiting review is assigned."
    return ("Awaiting review and unassigned (oldest first):\n"
            + _bullets(f"{a.company_name} (#{a.id}), submitted {_fmt_date(a.created_at)}" for a in rows))


def _answer_admin_recent(_admin):
    since = utcnow() - timedelta(days=30)
    submitted = SupplierApplication.query.filter(SupplierApplication.status != "draft",
                                                 SupplierApplication.created_at >= since).count()
    approved = SupplierApplication.query.filter(SupplierApplication.approved_at >= since).count()
    signups = User.query.filter(User.created_at >= since).count()
    return (f"In the last 30 days: **{submitted}** applications submitted, **{approved}** approved and **{signups}** "
            "new supplier sign-ups. For other periods, see /admin/reports.")


def _answer_admin_catalogue_count(_admin):
    products = catalogue_query().count()
    suppliers = catalogue_query().with_entities(Product.user_id).distinct().count()
    return f"The catalogue has **{products}** listed products from {suppliers} active supplier(s). Browse it at /admin/catalogue."


SUPPLIER_SUGGESTIONS = ["What's my application status?", "Which documents are missing?", "When do my certificates expire?",
                        "How do I add products?"]
ADMIN_SUGGESTIONS = ["How many applications are waiting?", "How do I assign an application?", "What is a director match?",
                     "What can each role do?"]

SUPPLIER_INTENTS = [
    Intent("greeting", ["hi", "hii", "hie", "hai", "hey", "hello", "helo", "hallo", "hola", "yo", "sawubona", "molo", "dumela",
                        "heita", "howzit", "unjani", "yebo", "hi there", "good day", "good morning", "good afternoon",
                        "good evening", "morning", "afternoon", "how are you", "how are you doing", "help"],
           "Hi, I'm Cebelihle, how can I help you?", suggestions=SUPPLIER_SUGGESTIONS),
    Intent("thanks", ["thanks", "thank you", "ngiyabonga", "enkosi", "great", "perfect"],
           "You're welcome! Is there anything else I can help with?"),
    Intent("my_status", ["status", "my application", "application status", "progress", "where is my application",
                         "approved yet", "any update", "been reviewed", "outcome", "result"],
           _answer_my_status, suggestions=["Which documents are missing?", "How long does a review take?"]),
    Intent("missing_documents", ["missing", "outstanding", "documents missing", "documents are missing",
                                 "which documents are missing", "any documents missing", "still need", "not uploaded",
                                 "what documents do i need", "my documents"], _answer_my_documents),
    Intent("required_documents", ["required documents", "which documents", "what documents", "documents required",
                                  "documents needed", "list of documents", "what must i upload"],
           "These 7 documents are required (PDF only):\n" + _bullets(REQUIRED_DOCUMENTS)
           + "\nYou can also add a company profile and other supporting documents. Upload them on step 3, /onboarding/documents.",
           suggestions=["Which documents are missing?", "Where do I get the declaration form?"]),
    Intent("certificates", ["certificate", "expire", "expiry", "expired", "expires", "renew", "renewal", "vat certificate",
                            "bbbee", "bee certificate", "certificate dates", "reminder"],
           _answer_my_certificates, suggestions=["Is my account active?"]),
    Intent("account", ["account active", "account inactive", "deactivated", "inactive", "is my account",
                       "account status", "reactivate", "activate my account", "account disabled", "suspended"],
           _answer_my_account),
    Intent("registration_certificate", ["my certificate", "registration certificate", "certificate of registration", "supplier certificate",
                                        "download certificate", "print certificate", "proof of registration"],
           _answer_registration_certificate),
    Intent("how_to_apply", ["how do i apply", "how to apply", "apply", "register as supplier", "start application",
                            "steps", "onboarding", "become a supplier", "new application"],
           "Applying takes 4 steps:\n"
           "- **Company profile** (/onboarding/profile): company details, address, contact person and every director, "
           "shareholder and owner, plus a company profile PDF or website.\n"
           "- **Category** (/onboarding/category): what you supply.\n"
           "- **Documents** (/onboarding/documents): the 7 required PDFs.\n"
           "- **Review** (/onboarding/review): check everything, accept the Terms & Conditions and submit.",
           suggestions=["Which documents are required?", "What categories are there?"]),
    Intent("edit_application", ["edit", "edit my application", "change my application", "update my application", "resubmit", "correct",
                                "fix my application", "make changes", "replace document", "upload again"],
           "You can edit your application while it's submitted, pending review, under review, returned for update or "
           "declined: open it from /apps and choose Edit. If it was returned or declined, saving resubmits it for "
           "review. Once it's approved or on file, the details are locked. " + REFER),
    Intent("categories", ["category", "categories", "what do you buy", "goods", "services", "rentals", "what can i supply"],
           "Supplier categories:\n" + _bullets(c["value"] for c in SUPPLIER_CATEGORIES)
           + "\nYou choose one on step 2, /onboarding/category."),
    Intent("inventory", ["inventory", "products", "add products", "add a product", "list products", "prices", "price",
                         "stock", "catalogue", "catalog", "lead time", "my products"],
           _answer_my_products, suggestions=["How do orders work?"]),
    Intent("orders", ["order", "orders", "get an order", "get orders", "get work", "purchase order", "payment", "paid", "invoice", "contract", "tender",
                      "quote", "quotation", "business from you", "when will i get work"],
           "Orders aren't placed in the portal. Icebolethu's procurement team uses your listed inventory to compare "
           "suppliers and contacts you directly when they need your goods or services. For orders, payments, invoices "
           f"or contracts, please contact {CONTACT_DETAILS}."),
    Intent("password", ["password", "forgot password", "reset password", "change password", "cant log in",
                        "can't log in", "cannot login", "login", "log in", "sign in"],
           "To reset a forgotten password, use /forgot-password: we email you a link that works for one hour. "
           "If you're signed in, change your password at /settings. Never share your password with anyone, "
           "including Icebolethu staff."),
    Intent("verification_code", ["verification code", "code", "otp", "didnt get the code", "no code", "code expired",
                                 "one time pin"],
           "Every time you sign in, we email you a 6-digit code that's valid for 10 minutes. If it hasn't arrived, "
           "check your spam folder and use **Resend code** (you can resend after 30 seconds). After 5 wrong codes "
           "you'll need to log in again."),
    Intent("contact_details", ["contact details", "update my details", "change phone", "change email", "phone number",
                               "contact person", "settings"],
           "Update your phone number and contact details at /settings. To change your login email, " + REFER[0].lower()
           + REFER[1:]),
    Intent("contact", ["contact", "speak to someone", "call", "phone you", "email you", "human", "person", "agent",
                       "support", "complain", "complaint"],
           f"You can reach the supplier onboarding team at {CONTACT_DETAILS}."),
    Intent("terms", ["terms", "conditions", "terms and conditions", "t&c", "ts and cs"],
           "There are two sets of terms: the Portal Terms (/terms), which you accept when you sign up or log in, and "
           "the Supplier Application Terms (/application-terms), which you accept before you submit an application."),
    Intent("declaration", ["declaration", "declaration form", "declaration of interest", "conflict of interest"],
           "The Declaration of Interest form is one of the 7 required documents. Download it on the documents step "
           "(/onboarding/documents), fill it in and sign it, then upload it as a PDF. Declare any relationship with "
           "Icebolethu Group or its staff."),
    Intent("file_format", ["pdf", "file type", "format", "file size", "too big", "upload error", "upload failed",
                           "word document", "jpg", "photo", "scan"],
           "Documents must be PDF files of up to 25 MB each. If you have a photo or Word document, save or scan it as a "
           "PDF first. Make sure it's clear and complete."),
    Intent("directors", ["director", "directors", "shareholder", "owner", "id number", "already linked",
                         "another supplier", "linked to another"],
           "List every director, shareholder and owner on step 1 with their ID or passport number. A person may only "
           "be part of one supplier on the portal. If you have a question about a director or shareholder, "
           + REFER[0].lower() + REFER[1:]),
    Intent("review_time", ["how long", "when will", "timeline", "how many days", "waiting", "taking long", "still pending"],
           "Each application is reviewed by the procurement team and there's no fixed timeline. You'll get an email "
           f"and a notification as soon as the status changes. If you've been waiting a long time, contact {CONTACT_DETAILS}."),
    Intent("why_decision", ["why declined", "why was i declined", "why was my application declined",
                            "why was my application rejected", "application declined", "application rejected", "why rejected", "rejected", "declined", "reason",
                            "why returned", "why on file", "unsuccessful", "appeal", "dispute"], _answer_why_decision),
    Intent("statuses", ["what does pending review mean", "what does on file mean", "on file", "returned for update",
                        "under review", "pending review", "meaning", "what does it mean"],
           "What the statuses mean:\n"
           "- **Pending review**: waiting for the procurement team.\n"
           "- **Under review**: being reviewed now.\n"
           "- **Returned for update**: fix what the comments ask for and resubmit.\n"
           "- **Approved**: you're a registered supplier, your account is active.\n"
           "- **On file**: everything is in order; we'll contact you when your services are needed.\n"
           "- **Declined**: not successful; you may edit and resubmit if things change."),
    Intent("notifications", ["notification", "notifications", "emails", "didnt get an email", "no email", "messages"],
           "All updates about your application are emailed to you and shown at /notifications. Check your spam folder "
           "if you're missing emails from the portal."),
    Intent("sensitive_banking", ["bank", "banking", "bank details", "bank account", "change bank"],
           "For your security, banking details can't be discussed or changed here. Upload a new bank confirmation "
           f"letter (not older than 3 months) only when asked, and contact {CONTACT_DETAILS} to change banking details."),
    Intent("sensitive_privacy", ["popia", "delete my data", "my information", "privacy", "personal information",
                                 "close my account", "delete my account"],
           f"For privacy or data requests, please contact {CONTACT_DETAILS}. You can read how we use your information "
           "in the Portal Terms at /terms."),
    Intent("sensitive_security", ["hacked", "fraud", "scam", "someone else", "suspicious", "phishing", "stolen"],
           "If you think your account has been used by someone else, change your password at /settings straight away "
           f"and contact {CONTACT_DETAILS}. Icebolethu staff will never ask for your password or verification code."),
    Intent("other_suppliers", ["other suppliers", "competitor", "who else", "other companies", "how many suppliers"],
           "I can only help with your own account, so I can't share information about other suppliers."),
    # ---- Frequently asked questions ----
    Intent("bot_identity", ["who are you", "are you a robot", "are you human", "are you a bot", "your name",
                            "what is cebelihle", "who is cebelihle", "bot"],
           "I'm Cebelihle, the Supplier Portal's assistant. I answer common questions about the portal and about your "
           f"own application and account. For anything I can't help with, contact {CONTACT_DETAILS}."),
    Intent("what_can_you_do", ["what can you do", "what can you help", "how can you help", "options", "menu",
                               "what do you know"],
           "I can help with:\n- Your application status, missing documents and certificate dates\n- Applying, editing "
           "and resubmitting\n- Required documents and what counts as each one\n- Your account, password and "
           "verification codes\n- Your inventory and how orders work\n- Who to contact for anything else",
           suggestions=SUPPLIER_SUGGESTIONS),
    Intent("bye", ["bye", "goodbye", "see you", "thats all", "that is all", "no thanks", "cheers"],
           "Goodbye! Come back any time you have a question about the portal."),
    Intent("cost", ["cost", "fee", "fees", "free", "pay to register", "registration fee", "how much does it cost",
                    "charge", "pay anything", "have to pay", "do i pay", "pay", "payment to register"],
           "There's no fee to register or apply on the Supplier Portal. Icebolethu Group will never ask you to pay to "
           f"be registered or approved. If anyone asks you for money in Icebolethu's name, please report it to {CONTACT_DETAILS}."),
    Intent("about_icebolethu", ["who is icebolethu", "about icebolethu", "what does icebolethu do", "icebolethu group",
                                "about the company", "what is igsp", "what is this portal"],
           "Icebolethu Group is a South African funeral services and financial services provider (FSP45714). The "
           "Icebolethu Group Supplier Portal (IGSP) is where businesses register to supply goods and services to the group."),
    Intent("office_address", ["office", "head office", "where are you", "physical address of icebolethu", "visit you",
                              "your address", "branch", "johannesburg office", "durban office"],
           "Icebolethu Group's offices:\n- **Head office**: 277 Umbilo Road, Congella, Durban, 4013\n- **Johannesburg**: "
           f"387 Surrey Avenue, Ferndale, Randburg, 2194\nFor supplier questions, contact {CONTACT_DETAILS}."),
    Intent("vat_registration", ["vat", "vat registered", "not vat registered", "no vat", "dont have vat",
                                "vat number", "vat vendor"],
           "The SARS VAT certificate is one of the 7 required documents, and you enter its issue and expiry dates "
           f"when you upload it. If your business isn't VAT registered, please contact {CONTACT_DETAILS} before you apply."),
    Intent("bbbee", ["bbbee certificate", "no bbbee", "dont have bbbee", "affidavit", "sworn affidavit",
                     "accountant letter", "letter from accountant", "bee status", "bbbee level", "bee level",
                     "dont have a bbbee", "no bbbee certificate", "without bbbee"],
           "For B-BBEE you can upload any one of these: a valid B-BBEE certificate, a letter from your accountant, or "
           "a sworn affidavit. Enter its issue and expiry dates when you upload it, and renew it before it expires."),
    Intent("certified_id", ["certified", "certify", "certified copy", "certified copies", "certified id", "id copies",
                            "id copy", "commissioner of oaths"],
           "Upload certified copies of the ID documents of **all** directors as one PDF. Copies can be certified at a "
           "police station or by a commissioner of oaths. Directors who aren't South African can use their passport."),
    Intent("foreign_directors", ["foreign", "foreigner", "is a foreigner", "foreign director", "foreign national", "not south african", "passport",
                                 "nationality", "non south african"],
           "Directors, shareholders and owners who aren't South African can be listed with their passport number and "
           "nationality on step 1, and you upload certified copies of their passports with the ID documents."),
    Intent("proof_of_address", ["proof of address", "proof of residence", "utility bill", "municipal account",
                                "company address proof", "residential address", "lease agreement"],
           "Upload a document that proves the company's address, such as a municipal or utility account or a lease "
           "agreement. It must not be older than 3 months."),
    Intent("bank_letter", ["bank letter", "bank confirmation", "confirmation of bank account", "stamped letter",
                           "bank confirmation letter"],
           "Upload a letter from your bank confirming the account is in your business's name. It must not be older "
           "than 3 months. For your security, I can't discuss banking details: to change them, contact "
           f"{CONTACT_DETAILS}."),
    Intent("cipc", ["cipc", "company registration document", "registration document", "cor14", "cor 14",
                    "sole proprietor", "not a registered company", "registered company", "pty ltd", "close corporation"],
           "The CIPC company registration document is one of the 7 required documents, and the registration number "
           f"goes on step 1. If your business isn't registered with CIPC, please contact {CONTACT_DETAILS} before you apply."),
    Intent("company_profile", ["company profile", "website", "no website", "profile document", "brochure"],
           "On step 1 you either upload your company profile as a PDF or enter your company website. You need at "
           "least one of the two."),
    Intent("one_category", ["more than one category", "multiple categories", "two categories", "several categories",
                            "change category", "change my category", "wrong category"],
           "Each application has one supplier category. If yours isn't listed, choose **Other** and describe it. To "
           "change the category, edit your application from /apps while it can still be edited."),
    Intent("save_progress", ["save", "continue later", "save my progress", "come back later", "lost my information",
                             "lost my details", "draft", "start over", "information disappeared"],
           "Documents you upload are saved straight away. Details you type on the application steps are kept while "
           "you're signed in (up to 8 hours), but signing out clears them, so try to finish and submit in one sitting."),
    Intent("multiple_accounts", ["second account", "another account", "two accounts", "multiple accounts",
                                 "more than one application", "second application", "another company", "two companies"],
           "Each account can hold one supplier application. To register a different company, sign up with a "
           "different email address. Remember that a director, shareholder or owner may only be part of one supplier."),
    Intent("users_per_company", ["add a user", "another user", "colleague", "share login", "multiple users",
                                 "staff access", "my staff", "share my account", "colleague have a login",
                                 "have a login", "own login", "second login"],
           "Each supplier has one login. Don't share your password; if the person who manages your account changes, "
           f"contact {CONTACT_DETAILS} to change the login email."),
    Intent("supplier_id", ["supplier id", "supplier number", "vendor number", "igsp number", "my supplier id",
                           "reference number"],
           lambda user: f"Your supplier ID is **{user.supplier_id}**. It's shown on your dashboard and on your "
                        "registration certificate. Quote it when you contact us."),
    Intent("after_approval", ["after approval", "what happens next", "next steps", "now what", "i am approved",
                              "what now", "i was approved", "been approved"],
           "Once you're approved:\n- Your account is activated and your Certificate of Supplier Registration is ready "
           "at /certificate\n- List your products and services with prices at /inventory\n- Keep your VAT and "
           "B-BBEE certificates up to date at /certificates\n- The procurement team contacts you directly when they "
           "need your goods or services"),
    Intent("certificate_validity", ["valid for", "certificate valid", "is the certificate valid",
                                    "how long is the certificate", "12 months", "certificate validity", "renew registration",
                                    "registration expires", "annual", "every year"],
           "The Certificate of Supplier Registration is valid for 12 months from approval. For questions about "
           f"renewing your registration, contact {CONTACT_DETAILS}."),
    Intent("hide_product", ["hide", "remove product", "delete product", "unlist", "out of stock", "no longer supply",
                            "stop selling", "remove a product"],
           "At /inventory you can hide a product from the procurement team (it stays in your inventory), delete it, "
           "or set its quantity to 0 to show it's out of stock."),
    Intent("who_sees_products", ["who can see my products", "who sees my products", "public", "visible",
                                 "customers see", "who sees my prices", "can other suppliers see"],
           "Only Icebolethu Group's procurement staff can see the products you list. They aren't public, and other "
           "suppliers can't see them."),
    Intent("data_safety", ["who can see my documents", "documents safe", "is my information safe", "data safe",
                           "secure", "confidential", "who sees my documents"],
           "Your documents are only available to authorised Icebolethu staff; they're never public, and every time "
           "staff open one it's recorded. Your information is handled under POPIA, as explained in the Portal Terms "
           "(/terms)."),
    Intent("vat_prices", ["vat inclusive", "include vat", "including vat", "excl vat", "excluding vat",
                          "prices include", "currency", "rand"],
           "Enter prices in South African Rand. Say clearly in the product description whether the price includes VAT."),
    Intent("units", ["unit", "units", "per day", "per hour", "per kg", "per event", "per litre"],
           "You can price each product " + ", ".join(PRODUCT_UNITS) + "."),
    Intent("too_many_attempts", ["too many", "too many attempts", "locked", "locked out", "blocked", "try again later",
                                 "15 minutes", "wait 15"],
           "After 10 wrong sign-in attempts, or 5 password reset requests, you need to wait 15 minutes before trying "
           "again. If you've forgotten your password, use /forgot-password."),
    Intent("session_expired", ["session expired", "logged out", "kicked out", "keeps logging me out", "timeout",
                               "timed out"],
           "You stay signed in for up to 8 hours. If you see “Your session expired”, refresh the page and try again; "
           "you may need to sign in again."),
    Intent("reset_email", ["reset email", "reset link", "link expired", "didnt get reset", "reset not working",
                           "no reset email"],
           "Password reset links work for one hour. Check your spam folder, and if the link has expired, request a "
           "new one at /forgot-password."),
    Intent("device_support", ["on my phone", "use my phone", "my phone", "smartphone", "mobile", "cellphone", "tablet", "browser", "download app", "an app",
                              "not loading", "page not working", "website not working", "error", "something went wrong"],
           "The portal works in any up-to-date web browser on a phone, tablet or computer; there's no app to "
           "install. If a page isn't working, refresh it or try another browser. If it still fails, contact "
           f"{CONTACT_DETAILS} and say what you were doing."),
    Intent("withdraw", ["withdraw", "withdraw my application", "cancel my application", "cancel application", "stop my application",
                        "delete my application", "remove my application"],
           f"To withdraw or cancel your application, please contact {CONTACT_DETAILS}."),
    Intent("reapply", ["reapply", "apply again", "second chance", "after declined", "after being declined"],
           "If your application was declined, you can edit it and resubmit it for review from /apps when your "
           "circumstances change."),
    Intent("language", ["isizulu", "zulu", "help in", "in isizulu", "in zulu", "other language", "afrikaans", "language", "xhosa", "sesotho"],
           f"The portal is in English. If you'd like help in another language, please contact {CONTACT_DETAILS}."),
    Intent("notifications_read", ["mark as read", "unread", "bell", "notification bell"],
           "The bell at the top shows new notifications. Open /notifications to read them all."),
    Intent("dashboard", ["dashboard", "home page", "where do i start", "main page"],
           "Your dashboard (/dashboard) shows your application status, next steps, certificate alerts and shortcuts "
           "to everything else."),
    Intent("provinces", ["provinces", "which areas", "outside kzn", "nationwide", "where must i be based",
                         "johannesburg", "cape town", "other province"],
           "Businesses from any of South Africa's 9 provinces can register. Your city and province help the "
           "procurement team find suppliers close to where they need them."),
    Intent("category_question", CATEGORY_KEYWORDS, _answer_category_question, with_message=True),
    Intent("signup", ["sign up", "signup", "create an account", "create account", "register an account",
                      "open an account", "new account", "how do i register"],
           "Create your account at /signup with your company name, contact name, phone number, email and a password, "
           "and accept the Portal Terms. We email you a 6-digit code to confirm your email, then you can start your "
           "application at /onboarding/profile."),
    Intent("password_rules", ["password rules", "password requirements", "strong password", "password must",
                              "password not accepted", "weak password"],
           "Choose a strong password you don't use anywhere else. If the portal doesn't accept it, the message on the "
           "page says what's missing."),
    Intent("wrong_email", ["wrong email", "typo in my email", "mistake in my email", "entered the wrong email",
                           "change my login email", "change login email"],
           f"Your login email can't be changed in the portal. Please contact {CONTACT_DETAILS} and we'll help."),
    Intent("company_name_change", ["change company name", "company name changed", "new company name", "rename company",
                                   "change trading name", "change registration number"],
           "While your application can still be edited, change your company details at /apps (choose Edit). After "
           f"approval, company details are locked: please contact {CONTACT_DETAILS}."),
    Intent("certificate_dates", ["issue date", "expiry date", "which date", "what date", "dates on the certificate",
                                 "no expiry date", "date of issue"],
           "When you upload your SARS VAT certificate and your B-BBEE certificate, letter or affidavit, enter the issue "
           "date and the expiry date as they appear on the document. If it doesn't show an expiry date, contact "
           f"{CONTACT_DETAILS} for help."),
    Intent("bbbee_expired", ["bbbee expired", "bbbee has expired", "new bbbee not ready", "waiting for bbbee",
                             "waiting for new certificate", "renewal not ready"],
           "Upload the renewed B-BBEE certificate, accountant's letter or sworn affidavit at /certificates as soon as "
           "you have it; your account is reactivated automatically. If you're waiting for a new one, contact "
           f"{CONTACT_DETAILS}."),
    Intent("id_number_invalid", ["id number invalid", "invalid id", "id not accepted", "id number not accepted",
                                 "checksum", "13 digits", "id number error", "passport number error", "id number is not accepted",
                                 "not accepted", "id is wrong", "id number is invalid"],
           "South African ID numbers must be the 13-digit number exactly as on the ID, starting with the date of birth "
           "(YYMMDD). For someone who isn't South African, choose their nationality first and enter their passport "
           "number (6–20 letters or digits)."),
    Intent("add_directors", ["how many directors", "add a director", "add another director", "more directors",
                             "remove a director", "remove director", "change directors", "add shareholder"],
           "On step 1 you can add a row for every director, shareholder and owner, and remove rows you don't need. "
           "After you've submitted, you can change them by editing your application from /apps while it can still be "
           f"edited. After approval, contact {CONTACT_DETAILS}."),
    Intent("view_application", ["view my application", "see my application", "my submitted application",
                                "where is my application", "open my application", "print my application",
                                "my applications"], _answer_my_application_page),
    Intent("multiple_files", ["more than one file", "multiple files", "two files", "several pages", "combine",
                              "merge pdf", "one pdf", "many pages", "separate files"],
           "Each document slot takes one PDF. If a document has several pages or files (for example the ID copies of "
           "all directors), combine them into one PDF before uploading. Use **Other supporting documents** for "
           "anything extra."),
    Intent("make_pdf", ["how do i make a pdf", "convert to pdf", "save as pdf", "scan to pdf", "how to scan",
                        "create a pdf", "turn into pdf", "no scanner"],
           "To make a PDF: in Word or most apps choose **Save as** or **Print** and pick PDF; on a phone, a scanner "
           "app (or the scan option in your phone's notes or files app) saves photos of documents as a PDF."),
    Intent("tax_number", ["tax number", "income tax", "tax reference", "tax pin", "tax clearance", "tax compliance",
                          "sars pin"],
           "Enter your company's tax number on step 1. The SARS VAT certificate is the SARS document you upload. For "
           f"other tax compliance questions, contact {CONTACT_DETAILS}."),
    Intent("primary_contact", ["primary contact", "contact person be different", "who should be the contact",
                               "different contact", "someone else as contact",
                               "contact persons role"],
           "On step 1, give the person Icebolethu should contact about your business, their role and their contact "
           "number and email. It can be someone other than the person who signed up."),
    Intent("conflict_family", ["family member works", "relative works", "work at icebolethu", "employee of icebolethu",
                               "i work for icebolethu", "know someone at icebolethu", "related to"],
           "Declare any relationship between your business (or its directors and owners) and Icebolethu Group or its "
           "staff on the Declaration of Interest form. Declaring it doesn't automatically disqualify you."),
    Intent("gifts", ["gift", "gifts", "bribe", "kickback", "pay someone", "speed up my application", "fast track"],
           "Icebolethu Group doesn't accept gifts, payments or favours in exchange for approval or orders, and "
           "applications can't be fast-tracked. If anyone asks you for this, please report it to "
           f"{CONTACT_DETAILS}."),
    Intent("insurance", ["insurance", "public liability", "licence", "license", "permit", "health certificate",
                         "roadworthy"],
           "The portal asks for the 7 documents listed on step 3, and you can add any other licences or certificates "
           "under **Other supporting documents**. For anything specific your category needs, contact "
           f"{CONTACT_DETAILS}."),
    Intent("delivery", ["delivery", "deliver", "lead time", "how fast", "turnaround", "collection"],
           "For each product in your inventory you can set a lead time: how many days you need to supply it. Delivery "
           "arrangements are agreed with the procurement team when they place an order."),
    Intent("edit_product", ["edit product", "change price", "change a price", "change the price", "change my price", "update price", "update stock", "change a product",
                            "edit a product", "update my products"],
           "Open /inventory and choose **Edit** on the product to change its name, description, price, unit, "
           "quantity or lead time."),
    Intent("stock_quantity", ["quantity", "what quantity", "quantity for services", "how many in stock", "services quantity", "not tracked", "unlimited"],
           "Quantity is optional. Leave it blank for services or anything you don't count (it shows as “not "
           "tracked”), or enter how many you have; 0 shows as out of stock."),
    Intent("what_is_bbbee", ["what is bbbee", "what is bee", "bbbee meaning", "broad based"],
           "B-BBEE (Broad-Based Black Economic Empowerment) is South Africa's framework for measuring a business's "
           "empowerment status. The portal asks for a valid B-BBEE certificate, an accountant's letter or a sworn affidavit."),
    Intent("what_is_cipc", ["what is cipc", "cipc meaning"],
           "CIPC is the Companies and Intellectual Property Commission, where South African companies are registered. "
           "Your CIPC registration document shows your company's registration details."),
    Intent("hours", ["hours", "open", "opening hours", "working hours", "weekend", "public holiday", "after hours"],
           "The portal is available 24/7, so you can apply and update your details any time. For the Call Centre, "
           "phone 031 208 9102 or email supplier_onboarding@icebolethu.co.za."),
    Intent("whatsapp", ["whatsapp", "sms", "social media", "facebook", "telephone number", "email address for"],
           f"You can reach the supplier onboarding team at {CONTACT_DETAILS}."),
]

ADMIN_INTENTS = [
    Intent("greeting", ["hi", "hii", "hie", "hai", "hey", "hello", "helo", "hallo", "hola", "yo", "sawubona", "molo", "dumela",
                        "heita", "howzit", "unjani", "yebo", "hi there", "good day", "good morning", "good afternoon",
                        "good evening", "morning", "afternoon", "how are you", "how are you doing", "help"],
           "Hi, I'm Cebelihle, how can I help you?", suggestions=ADMIN_SUGGESTIONS),
    Intent("thanks", ["thanks", "thank you", "ngiyabonga", "great", "perfect"], "You're welcome!"),
    Intent("counts", ["how many", "count", "numbers", "waiting", "awaiting review", "pending", "unassigned", "backlog",
                      "summary", "overview", "statistics", "stats"], _answer_admin_counts, section="applications"),
    Intent("expiring", ["expiring", "expire", "expired", "expiry", "certificates", "renewal", "renew"],
           _answer_admin_expiring, section="applications"),
    Intent("active_suppliers", ["active suppliers", "suppliers active", "how many suppliers", "supplier accounts"],
           _answer_admin_suppliers, section="suppliers"),
    Intent("assign", ["assign", "assigned", "take an application", "unassign", "release", "hand over", "reassign"],
           "Open the application and click **Assign to me**. Only the assigned admin can record a decision. "
           "To hand it back, use **Unassign**. A Procurement Team Lead or ICT Admin can also assign an application "
           "to any Procurement Officer from the application page.", section="applications"),
    Intent("decide", ["approve", "decline", "return", "on file", "decision", "review an application", "reject",
                      "comment", "comments", "under review"],
           "On an application assigned to you, use the **Review decision** panel:\n"
           "- **Approve**: activates the supplier account and issues their registration certificate.\n"
           "- **Accept & keep on file**: everything is in order but the services aren't needed yet.\n"
           "- **Mark under review**, **Return to supplier** (needs a comment) or **Decline** (needs a comment).\n"
           "The supplier is emailed about every change, and it's all recorded in the History.", section="applications"),
    Intent("director_match", ["director match", "director", "shareholder", "linked to another", "already linked",
                              "duplicate", "decline only", "greyed out", "can only be declined"],
           "A **Director match** means a director, shareholder or owner on the application is already on another "
           "supplier's earlier application. The supplier can still submit, but admins are notified, the application "
           "is flagged, and it can only be declined (the other buttons are greyed out). The decline comment is "
           "pre-filled for you. An ICT Admin can delete a wrongly captured director.", section="applications"),
    Intent("documents", ["document", "documents", "view document", "replace document", "download all", "zip", "pdf"],
           "On the application page you can open each PDF (every view is logged in the History), replace a document "
           "on behalf of the supplier, and download all documents as one ZIP with **Download all**.", section="applications"),
    Intent("history", ["history", "audit trail", "who changed", "log", "changes"],
           "Every application has a **History** button showing who did what and when, including document views, "
           "with a CSV export. Sign-ins and ICT actions are in the User audit (ICT Admins only).", section="applications"),
    Intent("suppliers", ["supplier account", "deactivate", "activate", "suppliers page", "find a supplier", "suppliers"],
           "Use /admin/suppliers to find suppliers and filter by account and application status. The assigned admin "
           "can activate or deactivate a supplier's account from the application page; the supplier is notified.",
           section="suppliers"),
    Intent("catalogue", ["catalogue", "catalog", "products", "prices", "compare prices", "find a product", "location",
                         "province", "find", "looking for", "who sells", "who supplies", "where can i get", "cheapest",
                         *[p.lower() for p in PROVINCES]],
           "The catalogue (/admin/catalogue) lists products from active approved suppliers. Filter by category, "
           "supplier and province, compare prices, and contact the supplier directly by phone or email to order.",
           section="catalogue"),
    Intent("reports", ["report", "reports", "export", "csv", "date range", "excel"],
           "Reports (/admin/reports) cover applications, suppliers, inventory and activity. Pick a period or a custom "
           "date range, and export any report to CSV.", section="reports"),
    Intent("user_audit", ["user audit", "audit", "sign ins", "failed login", "login history", "who logged in"],
           "The User audit (/admin/ict/audit) shows everything admins do: sign-ins and failed sign-ins, work on "
           "applications (assigning, decisions, comments, document views and downloads), exports and ICT actions, "
           "with IP addresses. Pick an admin to see all their activity, and export it to CSV. Supplier activity "
           "isn't included.",
           section="ict"),
    Intent("email_templates", ["email template", "email templates", "edit email", "change email text", "wording"],
           "Edit the emails sent to suppliers at /admin/ict/emails. Use only the placeholders listed for each email, "
           "check the preview, and use **Reset to default text** to undo.", section="ict"),
    Intent("delete", ["delete application", "delete director", "remove director", "remove application", "delete"],
           "ICT Admins can delete a director (bin icon in the directors table) or a whole application (the "
           "**Delete application** card; type the company name to confirm) from the application page. Deleting an "
           "application removes its documents and history and can't be undone.", section="ict"),
    Intent("roles", ["roles", "role", "permissions", "access", "what can i do", "team lead", "officer", "manager", "ict"],
           "Admin roles:\n"
           "- **Procurement Team Lead**: everything, and assigns applications to officers.\n"
           "- **Procurement Officer**: applications and suppliers.\n"
           "- **Procurement Manager**: reports only.\n"
           "- **ICT Admin**: everything except the dashboard, plus user audit, email templates and deleting records."),
    Intent("new_admin", ["new admin", "add admin", "create admin", "add a user", "new user", "change role"],
           "Admin accounts are created by IT with the command `flask --app app create-admin` (with --role super, "
           "applications, reports or ict). Roles can be changed with `flask --app app set-admin-role`."),
    Intent("supplier_process", ["how does the supplier apply", "supplier steps", "onboarding", "required documents",
                                "what documents"],
           "Suppliers sign up, then apply in 4 steps: company profile (with directors), category, the 7 required PDF "
           "documents, and review (accepting the Supplier Application Terms). VAT and B-BBEE certificates carry "
           "expiry dates; reminders go out 30 and 7 days before, and expiry deactivates the account until renewed."),
    # ---- Frequently asked questions ----
    Intent("bot_identity", ["who are you", "are you a robot", "are you a bot", "your name", "what is cebelihle", "bot"],
           "I'm Cebelihle, the portal's built-in assistant. I explain how the admin portal works and can look up "
           "applications by number, counts and expiring certificates for the sections your role can open."),
    Intent("what_can_you_do", ["what can you do", "what can you help", "how can you help", "options", "menu"],
           "Ask me things like:\n- “application 12”\n- “How many applications are waiting?”\n- “Which certificates "
           "are expiring?”\n- “How do I assign an application?”\n- “What is a director match?”\n- “What can each "
           "role do?”", suggestions=ADMIN_SUGGESTIONS),
    Intent("bye", ["bye", "goodbye", "thats all", "no thanks", "cheers"], "Goodbye!"),
    Intent("admin_password", ["admin password", "forgot my password", "change my password", "reset my password",
                              "password"],
           "Admin passwords are reset by IT with `flask --app app create-admin --email <your email>` (it resets the "
           "password of an existing admin). If your Microsoft account uses the same email, you can also sign in with "
           "Microsoft."),
    Intent("microsoft_login", ["microsoft", "sso", "office 365", "single sign on", "outlook login"],
           "On the admin sign-in page choose **Sign in with Microsoft**. It works when your Microsoft account email "
           "matches your admin account."),
    Intent("supplier_password", ["supplier password", "supplier forgot", "supplier cant log in", "supplier locked out",
                                 "supplier login", "supplier cannot sign in", "supplier code"],
           "Suppliers reset their own password at /forgot-password; admins can't see or set supplier passwords. Every "
           "supplier sign-in needs a 6-digit code emailed to them (valid 10 minutes). After 10 wrong attempts they "
           "must wait 15 minutes."),
    Intent("supplier_emails", ["supplier didnt get email", "emails not sending", "email not received",
                               "notification email", "supplier not getting emails", "not getting emails",
                               "getting emails", "not receiving emails"],
           "Suppliers are emailed automatically at every status change and also see each update at /notifications in "
           "the portal. Ask them to check their spam folder; if no emails are going out at all, contact IT."),
    Intent("reopen_approved", ["change approved", "edit approved application", "reopen", "supplier wants to update",
                               "unlock", "let supplier edit", "let a supplier edit", "edit an approved", "approved application"],
           "Approved and on-file applications are locked for suppliers. To let a supplier make changes, the assigned "
           "admin uses **Return to supplier** with a comment saying what to update; their resubmission comes back as "
           "pending review.", section="applications"),
    Intent("reactivate_supplier", ["reactivate", "reactivate supplier", "reactivate a supplier", "supplier inactive", "account deactivated",
                                   "certificate expired supplier"],
           "A supplier deactivated because a certificate expired is reactivated automatically when they upload the "
           "renewed certificate. Otherwise the assigned admin can activate the account from the **Supplier account** "
           "card on the application page.", section="suppliers"),
    Intent("registration_cert_admin", ["registration certificate", "supplier certificate", "print certificate",
                                       "certificate of registration"],
           "For an approved application, open it and click **Certificate** at the top to view or print the "
           "Certificate of Supplier Registration (valid 12 months from approval).", section="applications"),
    Intent("notifications_admin", ["notifications", "bell", "alerts", "who gets notified", "notified"],
           "The bell at the top opens /admin/notifications. Procurement Team Leads and Procurement Officers are "
           "notified (and emailed) about new, updated and resubmitted applications, director matches and certificate "
           "expiries. ICT Admins and Procurement Managers aren't."),
    Intent("search_filter", ["search", "filter", "find an application", "find application", "look up an application"],
           "Use the search box and the status and assignment filters on /admin/applications, or just ask me "
           "“application <number>”.", section="applications"),
    Intent("expiry_process", ["how does expiry work", "expiry reminders", "automatic deactivation", "expiry check",
                              "how often", "when are reminders sent"],
           "The portal checks VAT and B-BBEE certificate dates automatically every few hours. Suppliers are reminded "
           "30 and 7 days before expiry; on expiry their account is deactivated and admins are notified. Uploading "
           "the renewed certificate reactivates the account automatically."),
    Intent("dashboard_admin", ["dashboard", "home", "overview page"],
           "The dashboard (/admin) gives Procurement Team Leads an overview of applications and suppliers. Other "
           "roles start on the first page their role can open.", section="dashboard"),
    Intent("terms_admin", ["terms", "terms accepted", "supplier accept", "terms did", "application terms", "terms version", "terms and conditions"],
           "Suppliers accept the Portal Terms (/terms) when they sign up and log in, and the Supplier Application "
           "Terms (/application-terms) before submitting. The application page shows which version they accepted and when."),
    Intent("director_false_positive", ["wrong director", "director mistake", "false match", "captured wrongly",
                                       "director entered wrong", "remove the match"],
           "If a director match is caused by a wrongly captured director, an ICT Admin can delete that director from "
           "the application page; the flag then clears. Otherwise the application can only be declined."),
    Intent("place_order", ["place an order", "order", "buy", "purchase", "contact supplier", "contact a supplier"],
           "Orders aren't placed in the portal. Find the item in /admin/catalogue and contact the supplier directly "
           "using the phone number or email on the product card.", section="catalogue"),
    Intent("on_file_admin", ["what is on file", "keep on file", "on file meaning", "accept and keep on file"],
           "**Accept & keep on file** means the documents and information are in order but the services aren't "
           "needed yet. The supplier is told we'll contact them when they're needed, and their account stays inactive.",
           section="applications"),
    Intent("my_queue", ["my applications", "assigned to me", "my queue", "my work", "what must i do",
                        "my open applications", "what is on my plate"], _answer_admin_my_queue, section="applications"),
    Intent("unassigned_list", ["unassigned applications", "list unassigned", "which applications are unassigned",
                               "who needs to be assigned", "not assigned"], _answer_admin_unassigned, section="applications"),
    Intent("recent_activity", ["this month", "last 30 days", "recently", "new applications", "how many approved",
                               "approved this month", "new suppliers", "sign ups"], _answer_admin_recent,
           section="applications"),
    Intent("catalogue_count", ["how many products", "products in the catalogue", "catalogue size", "number of products"],
           _answer_admin_catalogue_count, section="catalogue"),
    Intent("cant_decide", ["cant approve", "cannot approve", "buttons disabled", "cant decide", "cant change status",
                           "why cant i", "not allowed", "no decision panel"],
           "You can only record a decision on an application assigned to you. If it's assigned to you and only "
           "**Decline** works, the application has a director match. If the page isn't open to you at all, your role "
           "doesn't include applications.", section="applications"),
    Intent("save_comments", ["save comments", "comment only", "add a comment", "without changing status", "note"],
           "Type in **Comments to supplier** and click **Save comments only** to keep the status as it is. The "
           "supplier is notified of new comments, so only write what they should see.", section="applications"),
    Intent("documents_status", ["documents status", "documents pending", "documents complete", "document status"],
           "**Documents** on the decision panel records whether you've checked the documents: Pending or Complete. "
           "Keeping an application on file marks them Complete automatically.", section="applications"),
    Intent("status_meanings", ["under review mean", "pending review mean", "what does under review",
                               "what does pending review", "pending review", "under review", "submitted status", "what does pending mean",
                               "status meaning", "statuses"],
           "Statuses:\n- **Pending review**: submitted and waiting for an admin\n- **Under review**: an admin is "
           "working on it\n- **Returned for update**: sent back to the supplier with comments\n- **Approved**: "
           "registered supplier, account active\n- **On file**: in order but not needed yet\n- **Declined**: not "
           "successful; the supplier may edit and resubmit"),
    Intent("someone_elses", ["assigned to someone else", "someone else has it", "colleague is away", "on leave",
                             "left the company", "take over"],
           "Only the assigned admin can unassign themselves. A Procurement Team Lead or ICT Admin can reassign it to "
           "another officer from the application page, and IT can release it with `flask --app app "
           "release-application <id>` if the admin has left.", section="applications"),
    Intent("what_changed", ["what changed", "see changes", "supplier updated", "before and after", "compare changes"],
           "Open the application's **History**: every supplier edit shows each changed field before and after, and "
           "you can export it to CSV.", section="applications"),
    Intent("contact_supplier", ["contact the supplier", "supplier phone", "supplier email", "call the supplier",
                                "supplier details", "supplier contact"],
           "The supplier's contact person, phone and email are on the application page under **Contact**, and on each "
           "product card in the catalogue. Ask me “application <number>” to find the application.", section="applications"),
    Intent("supplier_id_admin", ["supplier id", "igsp number", "supplier number"],
           "Each supplier gets an ID like IGSP1234 when they sign up. It's shown on the application page, the "
           "suppliers list and their registration certificate."),
    Intent("delete_supplier", ["delete supplier", "delete a supplier", "remove supplier", "remove a supplier", "delete a supplier account", "remove supplier account"],
           "Supplier logins can't be deleted in the portal. To stop a supplier, deactivate their account; an ICT Admin "
           "can delete an application if it was captured in error."),
    Intent("run_expiry_check", ["run expiry check", "run the expiry check", "run expiry", "check now", "check certificates now", "check-certificates", "manual check"],
           "The certificate check runs automatically every few hours. IT can run it straight away with "
           "`flask --app app check-certificates`."),
    Intent("report_types", ["which reports", "types of reports", "inventory report", "activity report",
                            "suppliers report", "applications report"],
           "Reports at /admin/reports:\n- **Applications**: submissions and outcomes\n- **Suppliers**: registrations "
           "and accounts\n- **Inventory**: listed products\n- **Admin activity**: what admins have been doing\nEach can be "
           "filtered by period or date range and exported to CSV.", section="reports"),
]


def _best_intent(intents, words):
    best, best_score = None, 0
    for intent in intents:
        score = intent.score(words)
        if score > best_score:
            best, best_score = intent, score
    return best


def supplier_chat_reply(user, message):
    intent = _best_intent(SUPPLIER_INTENTS, _chat_words(message))
    if not intent:
        return ("Sorry, I didn't quite get that. I can help with your application, documents, certificates, account "
                f"and inventory. For anything else, contact {CONTACT_DETAILS}.", SUPPLIER_SUGGESTIONS)
    return intent.reply(user, message), intent.suggestions


def admin_chat_reply(admin, message):
    words = _chat_words(message)
    number = re.search(r"(?:application|app|#)\s*#?\s*(\d{1,9})\b", words)
    if number and admin.can("applications"):
        return admin_application_reply(int(number.group(1))), []
    intent = _best_intent(ADMIN_INTENTS, words)
    if not intent:
        return ("Sorry, I didn't quite get that. Ask me about an application by number, or about assigning, "
                "decisions, director matches, the catalogue, reports or roles.", ADMIN_SUGGESTIONS)
    if intent.section and not admin.can(intent.section):
        return f"Your role ({admin.role_label}) doesn't have access to that part of the portal.", []
    return intent.reply(admin, message), intent.suggestions


def chat_message():
    data = request.get_json(silent=True) or {}
    message = data.get("message")
    if message is None and isinstance(data.get("messages"), list) and data["messages"]:
        message = (data["messages"][-1] or {}).get("content") if isinstance(data["messages"][-1], dict) else None
    message = str(message or "").strip()
    return message if 0 < len(message) <= CHAT_MAX_MESSAGE_CHARS else None


def chat_response(reply_fn, who, limiter_key):
    message = chat_message()
    if message is None:
        return jsonify(reply=f"Please type a question of up to {CHAT_MAX_MESSAGE_CHARS} characters.", suggestions=[]), 400
    if chat_limiter.is_blocked(limiter_key):
        return jsonify(reply="You've sent a lot of messages. Please wait a few minutes and try again.", suggestions=[]), 429
    chat_limiter.hit(limiter_key)
    reply, suggestions = reply_fn(who, message)
    return jsonify(reply=reply, suggestions=suggestions)


@app.route("/chat", methods=["POST"])
@login_required
def supplier_chat():
    user = get_current_user()
    return chat_response(supplier_chat_reply, user, ("chat-supplier", user.id))


@app.route("/admin/chat", methods=["POST"])
@admin_required
def admin_chat():
    admin = get_current_admin()
    return chat_response(admin_chat_reply, admin, ("chat-admin", admin.id))


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
@click.option("--role", type=click.Choice(list(ADMIN_ROLES)), default=None,
              help="super (Procurement Team Lead): everything; applications (Procurement Officer): applications and suppliers; "
                   "reports (Procurement Manager): reports only; ict (ICT Admin): everything except the dashboard, "
                   "plus user audit, email templates and deletions. "
                   "New admins default to super; existing admins keep their role unless this is given.")
@click.password_option()
def create_admin_command(email, name, role, password):
    """Create an admin account, or reset the password (and optionally the role) of an existing one."""
    email = normalize_email(email)
    admin = find_admin_by_email(email)
    if admin:
        admin.set_password(password)
        if name:
            admin.name = name
        if role:
            admin.role = role
        click.echo(f"Updated admin {email} ({admin.role_label}).")
    else:
        admin = Admin(email=email, name=name, role=role or "super")
        admin.set_password(password)
        DB.session.add(admin)
        click.echo(f"Created admin {email} ({admin.role_label}).")
    DB.session.commit()


@app.cli.command("set-admin-role")
@click.argument("email")
@click.argument("role", type=click.Choice(list(ADMIN_ROLES)))
def set_admin_role_command(email, role):
    """Change an existing admin's role without touching their password."""
    admin = find_admin_by_email(email)
    if not admin:
        raise click.ClickException(f"No admin with email {email}.")
    admin.role = role
    DB.session.commit()
    click.echo(f"{admin.email} is now a {admin.role_label}.")


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
@click.option("--months", default=6, show_default=True, help="Spread registrations over this many months.")
@click.option("--remove", is_flag=True, help="Delete all demo data instead of creating it.")
@click.option("--seed", default=2026, show_default=True, help="Random seed, for repeatable demo data.")
def seed_demo_command(count, months, remove, seed):
    """Create (or --remove) realistic demo suppliers, applications and inventory.

    Demo suppliers use @demo-supplier.example addresses (undeliverable), password Password123!
    """
    import random

    if remove:
        click.echo(f"Removed {remove_demo_data()} demo suppliers and everything linked to them.")
        return
    if User.query.filter(User.email.like(f"%@{DEMO_EMAIL_DOMAIN}")).first():
        raise click.ClickException("Demo data already exists. Run `flask --app app seed-demo --remove` first to replace it.")
    # Only admins who can work on applications get demo applications assigned to them.
    admins = [a for a in Admin.query.order_by(Admin.id).all() if a.can("applications")]
    if not admins:
        raise click.ClickException("Create an admin first: flask --app app create-admin")

    rng = random.Random(seed)
    now = utcnow()
    period_start = now - timedelta(days=30 * months)
    status_pool = (["approved"] * 12 + ["on_file"] * 3 + ["pending_review"] * 5 + ["under_review"] * 4 + ["submitted"] * 2
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
            issued = (submitted_at - timedelta(days=rng.randint(30, 300))).date() if doc_type in DATED_DOCUMENTS else None
            DB.session.add(SupplierDocument(user_id=user.id, application_id=application.id, document_type=doc_type,
                                            file_path=f"uploads/{user.id}/{filename}", original_filename=f"{doc_type}.pdf",
                                            uploaded_at=submitted_at, issue_date=issued,
                                            expiry_date=issued and issued + timedelta(days=365)))
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
                    "on_file": ["under_review", "on_file"], "returned_for_update": ["under_review", "returned_for_update"]}.get(status, [])
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

    DB.session.commit()
    click.echo(f"Created {len(created_users)} demo suppliers ({len(approved)} approved, {len(products)} products) "
               f"over the last {months} months.")
    click.echo(f"Demo supplier logins: <company>@{DEMO_EMAIL_DOMAIN} / Password123!   Remove with: flask --app app seed-demo --remove")



# Dummy user-audit entries use the 198.51.100.0/24 documentation range (RFC 5737), which no real visitor can have,
# so they can be told apart from real entries and removed again.
DEMO_AUDIT_IP_PREFIX = "198.51.100."


@app.cli.command("seed-audit")
@click.option("--days", default=60, show_default=True, help="Spread entries over this many days.")
@click.option("--remove", is_flag=True, help="Delete the dummy entries instead of creating them.")
@click.option("--seed", default=2026, show_default=True, help="Random seed, for repeatable data.")
def seed_audit_command(days, remove, seed):
    """Create (or --remove) dummy user-audit entries (sign-ins, sign-outs, exports, template edits) for the existing admins."""
    import random

    dummy = AuditLog.query.filter(AuditLog.ip_address.like(f"{DEMO_AUDIT_IP_PREFIX}%"))
    if remove:
        count = dummy.delete(synchronize_session=False)
        DB.session.commit()
        click.echo(f"Removed {count} dummy audit entries.")
        return
    if dummy.first():
        raise click.ClickException("Dummy audit entries already exist. Run `flask --app app seed-audit --remove` first.")
    admins = Admin.query.order_by(Admin.id).all()
    if not admins:
        raise click.ClickException("Create an admin first: flask --app app create-admin")

    rng = random.Random(seed)
    ips = {}  # each person mostly signs in from the same address

    def ip_for(key):
        if key not in ips:
            ips[key] = f"{DEMO_AUDIT_IP_PREFIX}{rng.randint(2, 254)}"
        return ips[key] if rng.random() < 0.85 else f"{DEMO_AUDIT_IP_PREFIX}{rng.randint(2, 254)}"

    entries = []

    def add(when, action, actor=None, email="", detail=""):
        name, key = (actor.name, actor.id) if actor else ("", email)
        entries.append(AuditLog(created_at=when, actor_type="admin", actor_id=getattr(actor, "id", None),
                                actor_name=(name or "")[:150], actor_email=(getattr(actor, "email", None) or email)[:120],
                                action=action, detail=detail[:255], ip_address=ip_for(key)))

    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    templates = list(EMAIL_TEMPLATES.values())
    for day in range(days, -1, -1):
        date_ = today - timedelta(days=day)
        workday = date_.weekday() < 5
        for admin in admins:
            if rng.random() > (0.7 if workday else 0.08):
                continue
            start = date_ + timedelta(hours=rng.randint(7, 9), minutes=rng.randint(0, 59), seconds=rng.randint(0, 59))
            if rng.random() < 0.07:  # mistyped password first
                add(start - timedelta(minutes=1), "admin_login_failed", actor=admin, detail="Wrong password")
            add(start, "admin_login", actor=admin, detail="Microsoft" if rng.random() < 0.35 else "Email and password")
            if admin.role == "ict" and rng.random() < 0.08:
                template = rng.choice(templates)
                add(start + timedelta(minutes=rng.randint(10, 120)), "email_template_updated", actor=admin, detail=template["name"])
            if admin.can("reports") and rng.random() < 0.1:
                kind, label = rng.choice(REPORT_KINDS)
                add(start + timedelta(minutes=rng.randint(15, 240)), "report_exported", actor=admin,
                    detail=f"{label} report ({rng.choice(['30d', '90d', '12m', 'ytd'])})")
            if rng.random() < 0.6:
                add(start + timedelta(hours=rng.randint(3, 9), minutes=rng.randint(0, 59)), "admin_logout", actor=admin)
        # Someone guessing at an admin address now and then.
        if rng.random() < 0.12:
            add(date_ + timedelta(hours=rng.randint(0, 23), minutes=rng.randint(0, 59)), "admin_login_failed",
                email=rng.choice(["admin@icebolethu.co.za", "it@icebolethu.co.za", "test@icebolethu.co.za"]),
                detail="No admin account with this email")
    now = utcnow()
    entries = [e for e in entries if e.created_at <= now]
    DB.session.add_all(entries)
    DB.session.commit()
    click.echo(f"Created {len(entries)} dummy audit entries for {len(admins)} admins over the last {days} days. "
               "Remove with: flask --app app seed-audit --remove")


init_database()


if __name__ == "__main__":
    app.run(debug=env_flag("FLASK_DEBUG"), host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "5000")))
