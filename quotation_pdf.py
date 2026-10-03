"""Server-side PDF version of the quotation (same content as templates/admin_quotation.html)."""
import io
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from xml.sax.saxutils import escape

# Icebolethu Group brand colours (see static/css/icebolethu.css).
BLACK = colors.HexColor("#231F20")
DEEP_ORANGE = colors.HexColor("#F26522")
GREY = colors.HexColor("#6E696A")
DARK_GREY = colors.HexColor("#4A4546")
RULE = colors.HexColor("#E6E3E3")


def zar(value):
    return "R " + f"{Decimal(value or 0):,.2f}".replace(",", " ")


def _style(name, **kw):
    base = {"fontName": "Helvetica", "fontSize": 9, "leading": 12.5, "textColor": DARK_GREY}
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "body": _style("body"),
    "strong": _style("strong", fontName="Helvetica-Bold", textColor=BLACK),
    "label": _style("label", fontName="Helvetica-Bold", fontSize=7.5, leading=10, textColor=DEEP_ORANGE, spaceAfter=2),
    "title": _style("title", fontName="Helvetica-Bold", fontSize=24, leading=28, textColor=DEEP_ORANGE, alignment=TA_RIGHT),
    "meta_k": _style("meta_k", fontName="Helvetica-Bold", textColor=GREY),
    "meta_v": _style("meta_v", fontName="Helvetica-Bold", textColor=BLACK, alignment=TA_RIGHT),
    "th": _style("th", fontName="Helvetica-Bold", fontSize=8, textColor=colors.white),
    "th_r": _style("th_r", fontName="Helvetica-Bold", fontSize=8, textColor=colors.white, alignment=TA_RIGHT),
    "cell_r": _style("cell_r", alignment=TA_RIGHT, textColor=BLACK),
    "small": _style("small", fontSize=7.5, leading=10, textColor=GREY),
    "total_k": _style("total_k", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=BLACK),
    "total_v": _style("total_v", fontName="Helvetica-Bold", fontSize=11, leading=14, textColor=BLACK, alignment=TA_RIGHT),
}


def P(text, style="body"):
    return Paragraph(text, S[style])


def lines(*parts):
    """Join non-empty, escaped lines with <br/>."""
    return "<br/>".join(escape(str(p)) for p in parts if p)


def build_quotation_pdf(ctx, generated_by, logo_path):
    req, supplier, application, company = ctx["req"], ctx["supplier"], ctx["application"], ctx["company"]
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=14 * mm, bottomMargin=16 * mm,
        title=f"Quotation {ctx['quote_number']}", author=company["name"], subject=f"{req.reference} · {supplier.company_name}",
    )
    width = doc.width
    story = []

    # --- Header: logo + company (left), title + reference box (right)
    left = []
    if logo_path and os.path.exists(logo_path):
        img_w, img_h = ImageReader(logo_path).getSize()
        logo_w = 48 * mm
        left.append(Image(logo_path, width=logo_w, height=logo_w * img_h / img_w))
        left.append(Spacer(1, 3 * mm))
    left.append(P(f"<b>{escape(company['name'])}</b>", "strong"))
    left.append(P(lines(
        company.get("address"),
        f"Reg. no. {company['registration_number']}" if company.get("registration_number") else "",
        f"VAT no. {company['vat_number']}" if company.get("vat_number") else "",
        " · ".join(x for x in (company.get("phone"), company.get("email")) if x),
    )))

    meta_rows = [
        ("Quotation no.", ctx["quote_number"]),
        ("Request ref.", req.reference),
        ("Request date", req.created_at.strftime("%d %B %Y") if req.created_at else "—"),
        ("Accepted", ctx["accepted_on"].strftime("%d %B %Y") if ctx.get("accepted_on") else "—"),
        ("Issued", ctx["issued_on"].strftime("%d %B %Y")),
    ]
    meta = Table([[P(k, "meta_k"), P(escape(v), "meta_v")] for k, v in meta_rows], colWidths=[28 * mm, 34 * mm])
    meta.setStyle(TableStyle([
        ("LINEBELOW", (1, 0), (1, -1), 0.5, RULE),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    right = [P("QUOTATION", "title"), Spacer(1, 2 * mm), meta]

    header = Table([[left, right]], colWidths=[width - 64 * mm, 64 * mm])
    header.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 2, DEEP_ORANGE),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    story += [header, Spacer(1, 6 * mm)]

    # --- Parties
    if application:
        address = ", ".join(x for x in (application.physical_address, application.city, application.province, application.postal_code) if x)
        supplier_lines = lines(
            f"t/a {application.trading_name}" if application.trading_name else "",
            f"Supplier ID: {supplier.supplier_id or '—'}",
            f"Reg. no. {application.business_registration_number}" if application.business_registration_number else "",
            f"VAT no. {application.vat_number or 'Not VAT registered'}",
            f"Tax no. {application.tax_number}" if application.tax_number else "",
            address,
            " · ".join(x for x in (application.primary_contact_person or supplier.contact_name, application.contact_number or supplier.phone) if x),
            application.email_address or supplier.email,
        )
        supplier_name = application.registered_vendor_name or supplier.company_name
    else:
        supplier_lines = lines(f"Supplier ID: {supplier.supplier_id or '—'}", f"{supplier.contact_name} · {supplier.phone}", supplier.email)
        supplier_name = supplier.company_name

    supplier_col = [P("SUPPLIER", "label"), P(f"<b>{escape(supplier_name)}</b>", "strong"), P(supplier_lines)]
    request_col = [
        P("REQUESTED BY", "label"), P(f"<b>{escape(company['name'])}</b>", "strong"),
        P(lines(req.admin.name or req.admin.email, req.admin.email)), Spacer(1, 3 * mm),
        P("DELIVERY", "label"),
        P(lines(req.delivery_location, f"Needed by: {req.required_by.strftime('%d %B %Y') if req.required_by else 'Not specified'}")),
    ]
    parties = Table([[supplier_col, request_col]], colWidths=[width / 2, width / 2])
    parties.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story += [parties, Spacer(1, 6 * mm)]

    # --- Line items
    description = f"<b>{escape(req.product_name)}</b>"
    if req.product and req.product.category:
        description += f"<br/><font size=8 color='#6E696A'>{escape(req.product.category)}</font>"
    if req.product and req.product.description:
        description += f"<br/><font size=8 color='#6E696A'>{escape(req.product.description)}</font>"
    items = Table(
        [
            [P("#", "th"), P("DESCRIPTION", "th"), P("QTY", "th_r"), P("UNIT", "th"), P("UNIT PRICE", "th_r"), P("AMOUNT", "th_r")],
            [P("1"), P(description), P(str(req.quantity), "cell_r"), P(escape(req.unit)), P(zar(req.unit_price), "cell_r"), P(zar(ctx["total"]), "cell_r")],
        ],
        colWidths=[8 * mm, width - 8 * mm - 14 * mm - 20 * mm - 28 * mm - 28 * mm, 14 * mm, 20 * mm, 28 * mm, 28 * mm],
        repeatRows=1,
    )
    items.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), BLACK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, RULE),
        ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story += [items, Spacer(1, 5 * mm)]

    # --- Notes (left) and totals (right)
    notes = []
    if req.notes:
        notes += [P(f"NOTES FROM {escape(company['name'].upper())}", "label"), P(escape(req.notes).replace("\n", "<br/>")), Spacer(1, 2 * mm)]
    if req.supplier_note:
        notes += [P("SUPPLIER'S NOTE", "label"), P(escape(req.supplier_note).replace("\n", "<br/>")), Spacer(1, 2 * mm)]
    notes += [P("PAYMENT", "label"), P(
        "Pay to the bank account on the supplier's <i>Confirmation of Bank Account Letter</i> held on file in the "
        f"supplier portal. Quote <b>{escape(ctx['quote_number'])}</b> as the payment reference."
    )]

    total_rows = []
    if ctx["vat_registered"]:
        total_rows += [[P("Subtotal (excl. VAT)"), P(zar(ctx["total_excl_vat"]), "cell_r")],
                       [P(f"VAT ({int(ctx['vat_rate'])}%)"), P(zar(ctx["vat_amount"]), "cell_r")]]
    total_rows.append([P("Total (incl. VAT)" if ctx["vat_registered"] else "Total", "total_k"), P(zar(ctx["total"]), "total_v")])
    totals = Table(total_rows, colWidths=[36 * mm, 30 * mm])
    totals.setStyle(TableStyle([
        ("LINEABOVE", (0, -1), (-1, -1), 1.5, BLACK),
        ("TOPPADDING", (0, -1), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    summary = Table([[notes, totals]], colWidths=[width - 70 * mm, 70 * mm])
    summary.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                 ("RIGHTPADDING", (0, 0), (0, -1), 10 * mm)]))
    story += [summary, Spacer(1, 3 * mm), P(
        "Prices as listed by the supplier at the time of the request"
        + (", VAT inclusive." if ctx["vat_registered"] else "; supplier is not VAT registered.")
        + " All amounts in South African Rand (ZAR).", "small"), Spacer(1, 10 * mm)]

    # --- Sign-off
    def sign_block(title, name=""):
        line = Table([[P(escape(name), "strong")]], colWidths=[width / 2 - 8 * mm], rowHeights=[9 * mm])
        line.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, BLACK), ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                                  ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
        sig = Table([[""]], colWidths=[width / 2 - 8 * mm], rowHeights=[12 * mm])
        sig.setStyle(TableStyle([("LINEBELOW", (0, 0), (-1, -1), 0.8, BLACK)]))
        return [P(title, "label"), line, P("Name", "small"), sig, P("Signature &amp; date", "small")]

    sign = Table([[sign_block("REQUESTED BY", req.admin.name or req.admin.email), sign_block("APPROVED FOR PAYMENT (FINANCE)")]],
                 colWidths=[width / 2, width / 2])
    sign.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 0)]))
    story.append(sign)

    footer_text = (f"{company['name']} · Generated from the Icebolethu Supplier Portal on "
                   f"{ctx['issued_on'].strftime('%d %b %Y at %H:%M')} UTC by {generated_by} · {ctx['quote_number']}")

    def draw_footer(canvas, _doc):
        canvas.saveState()
        canvas.setStrokeColor(RULE)
        canvas.line(doc.leftMargin, 11 * mm, A4[0] - doc.rightMargin, 11 * mm)
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(GREY)
        canvas.drawCentredString(A4[0] / 2, 7 * mm, footer_text)
        canvas.drawRightString(A4[0] - doc.rightMargin, 3.5 * mm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    doc.build(story, onFirstPage=draw_footer, onLaterPages=draw_footer)
    return buffer.getvalue()
