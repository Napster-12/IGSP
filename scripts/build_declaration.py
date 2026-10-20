"""Build the supplier Declaration of Interest form (static/Declaration.pdf), a fillable one-page A4 PDF.

Run from the project folder:  python scripts/build_declaration.py
Suppliers can type into the fields and tick Yes/No before printing, or print it blank and fill it in by hand.
"""
import os
import sys

from reportlab.lib.colors import HexColor, white
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.platypus import Paragraph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGO = os.path.join(ROOT, "static", "images", "logo.png")
OUT = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "static", "Declaration.pdf")

ORANGE = HexColor("#DD7A2D")
ORANGE_TEXT = HexColor("#B4560C")
TINT = HexColor("#FDF3EA")
INK = HexColor("#1F2430")
MUTED = HexColor("#5B5F66")
LINE = HexColor("#D5D7DB")
SOFT = HexColor("#F7F6F6")

W, H = A4
M = 50  # side margin
CW = W - 2 * M
FOOTER_TOP = 64  # content must stay above this

body = ParagraphStyle("body", fontName="Helvetica", fontSize=10, leading=14, textColor=INK)
small = ParagraphStyle("small", parent=body, fontSize=8.5, leading=11.5, textColor=MUTED)

c = canvas.Canvas(OUT, pagesize=A4)
c.setTitle("Declaration of Interest – Icebolethu Group")
c.setAuthor("Icebolethu Group")
c.setSubject("Supplier onboarding – Section C")
form = c.acroForm


def para(text, x, y, width, style=body):
    """Draw a paragraph whose top is at y; return the y below it."""
    p = Paragraph(text, style)
    _, h = p.wrap(width, 1000)
    p.drawOn(c, x, y - h)
    return y - h


def section(y, number, title):
    c.setFillColor(ORANGE)
    c.circle(M + 9, y - 6, 9, stroke=0, fill=1)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 10)
    c.drawCentredString(M + 9, y - 9.5, str(number))
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 12.5)
    c.drawString(M + 26, y - 10.5, title)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.7)
    c.line(M + 26 + c.stringWidth(title, "Helvetica-Bold", 12.5) + 10, y - 6, M + CW, y - 6)
    return y - 22


def field(name, label, x, y, width, height=21, multiline=False, tooltip=None):
    """Label above a bordered box with a fillable text field inside. Returns the y below the box."""
    c.setFillColor(MUTED)
    c.setFont("Helvetica-Bold", 8)
    c.drawString(x, y - 8, label.upper())
    top = y - 12
    c.setFillColor(white)
    c.setStrokeColor(LINE)
    c.setLineWidth(0.8)
    c.roundRect(x, top - height, width, height, 4, stroke=1, fill=1)
    form.textfield(
        name=name, tooltip=tooltip or label, x=x + 3, y=top - height + 2, width=width - 6, height=height - 4,
        borderWidth=0, fillColor=white, textColor=INK, fontName="Helvetica", fontSize=0 if multiline else 10.5,
        fieldFlags="multiline" if multiline else "", forceBorder=False,
    )
    return top - height


def yes_no(name, number, question, y):
    """A numbered question with Yes / No radio buttons on the right. Returns the y below the row."""
    text = f"<b>{number}.</b>&nbsp; {question}"
    text_w = CW - 140
    row_h = Paragraph(text, body).wrap(text_w, 1000)[1] + 14
    c.setFillColor(SOFT)
    c.roundRect(M, y - row_h, CW, row_h, 6, stroke=0, fill=1)
    para(text, M + 12, y - 7, text_w)
    mid = y - row_h / 2
    for i, value in enumerate(("Yes", "No")):
        bx = M + CW - 118 + i * 60
        c.setStrokeColor(MUTED)
        c.setFillColor(white)
        c.setLineWidth(1)
        c.circle(bx + 7, mid, 7, stroke=1, fill=1)
        form.radio(name=name, value=value, tooltip=value, x=bx, y=mid - 7, size=14, buttonStyle="circle",
                   borderColor=None, fillColor=None, textColor=ORANGE_TEXT, borderWidth=0, forceBorder=False)
        c.setFillColor(INK)
        c.setFont("Helvetica-Bold", 10)
        c.drawString(bx + 19, mid - 3.5, value)
    return y - row_h - 5


# ---- Header ----
c.setFillColor(ORANGE)
c.rect(0, H - 8, W, 8, stroke=0, fill=1)

logo = ImageReader(LOGO)
lw = 112
lh = lw * logo.getSize()[1] / logo.getSize()[0]
c.drawImage(logo, M, H - 24 - lh, width=lw, height=lh, mask="auto")

c.setFillColor(MUTED)
c.setFont("Helvetica-Bold", 8)
c.drawRightString(W - M, H - 40, "SUPPLIER ONBOARDING")
c.setFillColor(ORANGE_TEXT)
c.setFont("Helvetica-Bold", 11)
c.drawRightString(W - M, H - 55, "Section C")

y = H - 24 - lh - 14
c.setFillColor(INK)
c.setFont("Helvetica-Bold", 22)
c.drawString(M, y - 20, "Declaration of Interest")
y = para("Complete this form, sign it and upload it with your supplier application on the Icebolethu Supplier "
         "Portal. You can type into the fields before printing, or print it and complete it by hand.",
         M, y - 29, CW, small)
y -= 13

# ---- 1. Declarant ----
y = section(y, 1, "Your details")
half = (CW - 14) / 2
y = field("full_names", "Full names", M, y, CW)
y -= 5
field("id_number", "Identity number", M, y, half)
y = field("role", "Role / position in the company", M + half + 14, y, half)
y -= 5
y = field("company_name", "Company name", M, y, CW)
y -= 14

# ---- 2. Questions ----
y = section(y, 2, "Declaration of interest")
y = yes_no("q1_employee", 1, "Are you an employee of Icebolethu Group or any of its subsidiaries?", y)
y = yes_no("q2_family", 2, "Do you have any direct or indirect family relationship with any employee of "
                           "Icebolethu Group or any of its subsidiaries?", y)
y -= 1
y = field("details", "If you answered Yes to question 1 or 2, give full details", M, y, CW, height=46, multiline=True,
          tooltip="Names of the employees, how you are related, and their positions")
y = para("Include the employee's full name, how you are related and their position or department.",
         M, y - 3, CW, small)
y -= 9
y = yes_no("q3_other_supplier", 3, "Are any of your company's owners or directors also owners or directors of "
                                   "another company that is already a supplier to Icebolethu Group?", y)
y -= 1
y = field("other_supplier_company", "If yes, give the name of the other company", M, y, CW,
          tooltip="Name of the other company that already supplies Icebolethu Group")
y -= 14

# ---- 3. Acknowledgement and signature ----
y = section(y, 3, "Acknowledgement")
box_top = y
ack = [
    "I hereby acknowledge that failure to disclose any direct or indirect family relation as set out above "
    "will constitute a material breach of this agreement, and the relationship can be terminated with "
    "immediate effect.",
    "I hereby declare that the information provided on this form is true and correct.",
]
ack_h = sum(Paragraph(t, body).wrap(CW - 28, 1000)[1] for t in ack) + 6 * (len(ack) - 1)
box_h = ack_h + 18
c.setFillColor(TINT)
c.roundRect(M, box_top - box_h, CW, box_h, 6, stroke=0, fill=1)
c.setFillColor(ORANGE)
c.rect(M, box_top - box_h, 4, box_h, stroke=0, fill=1)
text_y = box_top - 9
for t in ack:
    text_y = para(t, M + 16, text_y, CW - 28) - 6
y = box_top - box_h - 8

# Signature (by hand) and date.
sig_w = CW * 0.62
c.setStrokeColor(INK)
c.setLineWidth(0.9)
c.line(M, y - 30, M + sig_w, y - 30)
c.setFillColor(MUTED)
c.setFont("Helvetica-Bold", 8)
c.drawString(M, y - 41, "SIGNATURE")
date_x = M + sig_w + 24
form.textfield(name="date", tooltip="Date (DD/MM/YYYY)", x=date_x, y=y - 29, width=W - M - date_x, height=20,
               borderWidth=0, fillColor=white, textColor=INK, fontName="Helvetica", fontSize=10.5, forceBorder=False)
c.line(date_x, y - 30, W - M, y - 30)
c.drawString(date_x, y - 41, "DATE")
content_bottom = y - 41

# ---- Footer ----
c.setStrokeColor(ORANGE)
c.setLineWidth(1.2)
c.line(M, 52, W - M, 52)
c.setFillColor(INK)
c.setFont("Helvetica-Bold", 8.5)
c.drawString(M, 38, "Icebolethu Group")
c.setFillColor(MUTED)
c.setFont("Helvetica", 8.5)
c.drawString(M + c.stringWidth("Icebolethu Group ", "Helvetica-Bold", 8.5), 38, "· Supplier Onboarding")
c.drawRightString(W - M, 38, "Tel 031 208 9102  ·  supplier_onboarding@icebolethu.co.za")

if content_bottom < FOOTER_TOP:
    sys.exit(f"Content runs into the footer (bottom at {content_bottom:.0f}pt, must be above {FOOTER_TOP}pt).")
c.showPage()
c.save()
print(f"Wrote {OUT} (content ends {content_bottom - FOOTER_TOP:.0f}pt above the footer)")
