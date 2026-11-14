"""
make_test_data.py
-----------------
Creates ALL the fake test data for the demo:
  - test_cases/<case>.json        one vendor submission per file (form fields + document list)
  - test_cases/docs/*.pdf / .jpg  fake documents, every one stamped "SAMPLE - FOR TESTING"
  - data/existing_vendors.csv     vendors "already in our system" (for the duplicate check, C13)

Run it from the vendor-onboarding folder:
    python tools/make_test_data.py

Everything here is made up. No real company, person, PAN, GSTIN or bank account.
"""

import csv
import json
import random
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.colors import Color, HexColor, black, white
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "test_cases"
DOCS = CASES / "docs"
DATA = ROOT / "data"


# ---------------------------------------------------------------------------
# 1. ID helpers: these build IDs that PASS the official format rules.
#    (checks.py will use the same maths to VERIFY them.)
# ---------------------------------------------------------------------------
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_check_digit(first14: str) -> str:
    """The 15th character of a GSTIN is a checksum of the first 14.
    Each character becomes a number 0-35. Odd positions are multiplied by 1,
    even positions by 2. Each product is split into (product // 36) + (product % 36)
    and summed. The check digit is whatever brings the total to a multiple of 36."""
    total = 0
    for i, ch in enumerate(first14):
        product = CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return CHARS[(36 - total % 36) % 36]


def make_gstin(state_code: str, pan: str, entity_no: str = "1") -> str:
    """GSTIN = 2-digit state code + 10-char PAN + entity number + 'Z' + check digit."""
    first14 = f"{state_code}{pan}{entity_no}Z"
    return first14 + gstin_check_digit(first14)


def aba_check_digit(first8: str) -> str:
    """US bank routing (ABA) numbers have a check digit too:
    3*(d1+d4+d7) + 7*(d2+d5+d8) + 1*(d3+d6+d9) must be a multiple of 10."""
    d = [int(x) for x in first8]
    s = 3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5])
    return str((10 - s % 10) % 10)


# ---------------------------------------------------------------------------
# 2. The vendors. Each one exists to prove ONE behaviour of the process.
# ---------------------------------------------------------------------------
def build_cases():
    cases = []

    # ---- Happy path: everything correct and consistent -------------------
    pan = "AAKCS4821M"  # 4th letter C = company, 5th letter S = "Sharma"
    cases.append({
        "case_id": "happy_path",
        "label": "Happy path - clean Indian company",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Sharma Packaging Pvt Ltd", "trade_name": "Sharma Pack",
            "business_type": "private_ltd", "country": "IN",
            "address": "14, 2nd Cross, Peenya Industrial Area", "city": "Bengaluru",
            "state": "Karnataka", "pin": "560058",
        },
        "contact": {"name": "Rahul Sharma", "email": "rahul@sharmapackaging.in", "phone": "+91 98450 11223"},
        "tax": {"gst_registered": True, "pan": pan, "gstin": make_gstin("29", pan)},
        "bank": {
            # Deliberately written as "PRIVATE LIMITED" while the legal name says "Pvt Ltd":
            # proves the name-cleaning step works and a real-world variation still passes.
            "account_holder": "SHARMA PACKAGING PRIVATE LIMITED",
            "account_number": "50100234567890", "ifsc": "HDFC0001234", "bank_name": "HDFC Bank",
        },
        "expected": {
            "status": "Approved", "failing_checks": [],
            "why": "Every check passes. 'Pvt Ltd' vs 'PRIVATE LIMITED' is normalised before the name match.",
        },
    })

    # ---- E1: bank account in a person's name ------------------------------
    pan = "AABCG7316Q"
    cases.append({
        "case_id": "E1_bank_name_mismatch",
        "label": "E1 - Bank account belongs to a person, not the company",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Green Leaf Foods Pvt Ltd", "trade_name": "",
            "business_type": "private_ltd", "country": "IN",
            "address": "Plot 22, SIDCO Industrial Estate, Guindy", "city": "Chennai",
            "state": "Tamil Nadu", "pin": "600032",
        },
        "contact": {"name": "Arjun Mehta", "email": "arjun.mehta.glf@gmail.com", "phone": "+91 98410 55667"},
        "tax": {"gst_registered": True, "pan": pan, "gstin": make_gstin("33", pan)},
        "bank": {
            "account_holder": "ARJUN MEHTA",
            "account_number": "002101558832", "ifsc": "ICIC0004521", "bank_name": "ICICI Bank",
        },
        "expected": {
            "status": "Pending - internal review", "failing_checks": ["C12"], "info_flags": ["C16"],
            "why": "All formats are valid and the cheque matches the form, but the account holder is a "
                   "person, not the company. Classic fraud pattern, so a human must verify. No vendor email. "
                   "The Gmail address is noted as info only.",
        },
    })

    # ---- E2: looks valid alone, contradicts when compared ----------------
    true_pan = "AAKFB5079H"          # 4th letter F = firm/LLP
    typo_pan = "AAKFB5079N"          # what the vendor typed: last letter wrong
    cases.append({
        "case_id": "E2_hidden_inconsistency",
        "label": "E2 - Valid GSTIN, but its state and PAN contradict the form",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Bright Steel Industries LLP", "trade_name": "Bright Steel",
            "business_type": "llp", "country": "IN",
            "address": "88, 4th Main, Rajajinagar Industrial Town", "city": "Bengaluru",
            "state": "Karnataka", "pin": "560044",
        },
        "contact": {"name": "Neha Kulkarni", "email": "accounts@brightsteel.co.in", "phone": "+91 98220 33445"},
        # GSTIN starts with 27 = Maharashtra, and contains the TRUE pan. Its checksum is valid.
        "tax": {"gst_registered": True, "pan": typo_pan, "gstin": make_gstin("27", true_pan)},
        "bank": {
            "account_holder": "BRIGHT STEEL INDUSTRIES LLP",
            "account_number": "39847261550", "ifsc": "SBIN0040123", "bank_name": "State Bank of India",
        },
        "_doc_overrides": {"pan_on_documents": true_pan,
                           "gst_address": "Gat No. 112, Bhosari MIDC, Pune, Maharashtra 411026"},
        "expected": {
            "status": "Pending - waiting on vendor", "failing_checks": ["C7", "C8", "C14"],
            "why": "GSTIN format and checksum pass (C4). But its state code 27 is Maharashtra while the "
                   "address is Karnataka (C8), and the PAN inside it differs from the PAN field by one "
                   "character (C7). The PAN card agrees with the GSTIN, so it's a typo -> fixable. "
                   "One email asks the vendor to correct the PAN and confirm the state.",
        },
    })

    # ---- E3: foreign vendor (USA) ----------------------------------------
    routing8 = "31209876"
    cases.append({
        "case_id": "E3_foreign_vendor",
        "label": "E3 - US vendor (kept for later; the app is India-only for now)",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Northwind Supplies Inc", "trade_name": "Northwind",
            "business_type": "corporation", "country": "US",
            "address": "455 Harbor Blvd, Suite 300", "city": "Austin",
            "state": "Texas", "pin": "78701",
        },
        "contact": {"name": "Emily Carter", "email": "vendors@northwindsupplies.com", "phone": "+1 512 555 0142"},
        "tax": {"gst_registered": False, "tax_id_type": "EIN", "tax_id": "84-3920175"},
        "bank": {
            "account_holder": "NORTHWIND SUPPLIES INC",
            "account_number": "4401928374",
            "routing_number": routing8 + aba_check_digit(routing8),   # US domestic bank code
            "swift": "FHBKUS44",                                         # for international wires
            "bank_name": "First Harbor Bank",
        },
        "expected": {
            "status": "Approved", "failing_checks": [],
            "why": "Country = US switches the rule set: GSTIN/PAN/IFSC checks are skipped; EIN, SWIFT and "
                   "ABA routing checks run instead; required docs become incorporation certificate, bank "
                   "letter and tax residency certificate.",
        },
    })

    # ---- E4: resubmission (v1 wrong doc, v2 fixed) -----------------------
    pan = "BQRPD6142K"  # 4th letter P = individual (a proprietor uses a personal PAN)
    kiran = {
        "company": {
            # For a sole proprietor, the LEGAL name is the person. The business name is the trade name.
            "legal_name": "Kiran Desai", "trade_name": "Kiran Textiles",
            "business_type": "proprietor", "country": "IN",
            "address": "Shop 9, Ring Road Textile Market", "city": "Surat",
            "state": "Gujarat", "pin": "395002",
        },
        "contact": {"name": "Kiran Desai", "email": "kirantextiles@gmail.com", "phone": "+91 98250 77889"},
        "tax": {"gst_registered": True, "pan": pan, "gstin": make_gstin("24", pan)},
        "bank": {
            # Account is in the TRADE name. C9 must compare against legal name OR trade name.
            "account_holder": "KIRAN TEXTILES",
            "account_number": "917020045123456", "ifsc": "UTIB0002871", "bank_name": "Axis Bank",
        },
    }
    cases.append({
        "case_id": "E4_resubmission_v1",
        "label": "E4 (v1) - Proprietor uploads a PAN card where the cheque should be",
        "version": 1, "parent_case": None, **kiran,
        "_doc_overrides": {"wrong_doc_in_cheque_slot": True},
        "expected": {
            "status": "Pending - waiting on vendor", "failing_checks": ["C13"],
            "why": "The file in the 'cancelled cheque' slot is actually a PAN card (AI reads the document "
                   "type). Fixable: email asks for a cheque or bank letter.",
        },
    })
    cases.append({
        "case_id": "E4_resubmission_v2",
        "label": "E4 (v2) - Same proprietor resubmits with the correct cheque",
        "version": 2, "parent_case": "E4_resubmission_v1", **kiran,
        "expected": {
            "status": "Approved", "failing_checks": [],
            "why": "Linked to v1 as version 2. All checks re-run and pass. Account holder matches the "
                   "trade name, which is accepted for proprietors. Gmail is normal for a proprietor.",
        },
    })

    # ---- Backup: duplicate bank account ----------------------------------
    pan = "AAEFN3307D"
    cases.append({
        "case_id": "backup_duplicate_bank",
        "label": "Backup - Bank account already belongs to another vendor",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Nova Traders", "trade_name": "",
            "business_type": "partnership", "country": "IN",
            "address": "Unit 5, Bhiwandi Logistics Park", "city": "Thane",
            "state": "Maharashtra", "pin": "421302",
        },
        "contact": {"name": "Sameer Khan", "email": "sameer@novatraders.in", "phone": "+91 98190 22113"},
        "tax": {"gst_registered": True, "pan": pan, "gstin": make_gstin("27", pan)},
        "bank": {
            "account_holder": "NOVA TRADERS",
            "account_number": "7712345098",   # <- same as existing vendor Metro Office Supplies
            "ifsc": "KKBK0000654", "bank_name": "Kotak Mahindra Bank",
        },
        "expected": {
            "status": "Rejected", "failing_checks": ["C15"],
            "why": "The bank account is already registered to a DIFFERENT existing vendor. Blocker: "
                   "possible fake vendor. The rejection email gives no detail about which check failed.",
        },
    })

    # ---- Backup: blurry cheque -------------------------------------------
    pan = "AAJCS8854R"
    cases.append({
        "case_id": "backup_blurry_doc",
        "label": "Backup - Cheque photo is blurry and rotated",
        "version": 1, "parent_case": None,
        "company": {
            "legal_name": "Sunrise Electricals Pvt Ltd", "trade_name": "",
            "business_type": "private_ltd", "country": "IN",
            "address": "3-45, IDA Balanagar", "city": "Hyderabad",
            "state": "Telangana", "pin": "500037",
        },
        "contact": {"name": "Priya Reddy", "email": "priya@sunriseelectricals.in", "phone": "+91 98490 66778"},
        "tax": {"gst_registered": True, "pan": pan, "gstin": make_gstin("36", pan)},
        "bank": {
            "account_holder": "SUNRISE ELECTRICALS PRIVATE LIMITED",
            "account_number": "1234101998877", "ifsc": "CNRB0003310", "bank_name": "Canara Bank",
        },
        "_doc_overrides": {"blurry_cheque": True},
        "expected": {
            "status": "Pending - waiting on vendor", "failing_checks": ["C13"],
            "why": "The AI can't read the cheque confidently. Instead of guessing, the process asks the "
                   "vendor to re-upload a clearer copy.",
        },
    })
    return cases


EXISTING_VENDORS = [
    # vendor_id, legal_name, pan, state_code, account_number, ifsc
    ("VND-0007", "Metro Office Supplies", "AABFM2210K", "27", "7712345098", "KKBK0000654"),
    ("VND-0012", "Deccan Logistics Pvt Ltd", "AACCD9081L", "36", "60321889012", "HDFC0004410"),
    ("VND-0019", "Patel Agro Exports", "AAGFP4471E", "24", "31005567820", "BARB0SURATX"),
    ("VND-0023", "Coastal Cold Chain Pvt Ltd", "AADCC6620P", "32", "10982233445", "FDRL0001122"),
    ("VND-0031", "Apex Print Solutions", "AAHFA1189B", "29", "92001005566", "ICIC0001987"),
]


# ---------------------------------------------------------------------------
# 3. Fake documents (PDF). Plain layouts, no logos or emblems, big SAMPLE marks.
# ---------------------------------------------------------------------------
RED = HexColor("#C62828")
GREY = HexColor("#555555")


def sample_marks(c, w, h):
    """Red banner at the top + light diagonal watermark. Every document gets this."""
    c.setFillColor(RED)
    c.rect(0, h - 28, w, 28, fill=1, stroke=0)
    c.setFillColor(white)
    c.setFont("Helvetica-Bold", 11)
    c.drawCentredString(w / 2, h - 19, "SAMPLE - FOR TESTING ONLY - NOT A REAL DOCUMENT")
    c.saveState()
    c.setFillColor(Color(0.8, 0.1, 0.1, alpha=0.08))
    c.setFont("Helvetica-Bold", 90)
    c.translate(w / 2, h / 2)
    c.rotate(35)
    c.drawCentredString(0, 0, "SAMPLE")
    c.restoreState()
    c.setFillColor(black)


def field_rows(c, x, y, rows, label_w=190, gap=22):
    for label, value in rows:
        c.setFont("Helvetica", 10)
        c.setFillColor(GREY)
        c.drawString(x, y, label)
        c.setFillColor(black)
        c.setFont("Helvetica-Bold", 11)
        # Long values (addresses) wrap onto extra lines instead of running off the page.
        for i, line in enumerate(textwrap.wrap(str(value), 52) or [""]):
            c.drawString(x + label_w, y - i * 14, line)
        y -= gap + 14 * (max(1, len(textwrap.wrap(str(value), 52))) - 1)
    return y


CONSTITUTION = {"private_ltd": "Private Limited Company", "llp": "Limited Liability Partnership",
                "partnership": "Partnership", "proprietor": "Proprietorship"}


def gst_certificate(path, v, pan_override=None, address_override=None):
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 80, "GST Registration Certificate")
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, h - 96, "(Test specimen modelled on Form GST REG-06)")
    co, tax = v["company"], v["tax"]
    address = address_override or f'{co["address"]}, {co["city"]}, {co["state"]} {co["pin"]}'
    field_rows(c, 60, h - 150, [
        ("Registration Number (GSTIN)", tax["gstin"]),
        ("Legal Name", co["legal_name"]),
        ("Trade Name, if any", co["trade_name"] or "-"),
        ("Constitution of Business", CONSTITUTION[co["business_type"]]),
        ("Address of Principal Place", address),
        ("Date of Liability", "01/04/2021"),
        ("Type of Registration", "Regular"),
        ("Certificate Issued On", "12/04/2021"),
    ])
    c.setFont("Helvetica-Oblique", 9)
    c.setFillColor(GREY)
    c.drawString(60, 120, "Generated for a software demo. Numbers are fictitious.")
    c.save()


def pan_card(path, v, pan_override=None):
    """A card-sized box on an A4 page."""
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    x, y, cw, ch = 110, h - 360, 375, 235
    c.setFillColor(HexColor("#E8F0FB"))
    c.roundRect(x, y, cw, ch, 12, fill=1, stroke=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 13)
    c.drawString(x + 20, y + ch - 32, "Permanent Account Number Card")
    c.setFont("Helvetica", 8)
    c.drawString(x + 20, y + ch - 46, "(test specimen)")
    co = v["company"]
    is_person = co["business_type"] == "proprietor"
    field_rows(c, x + 20, y + ch - 80, [
        ("Name", co["legal_name"].upper()),
        ("Date of Birth" if is_person else "Date of Incorporation", "14/08/1986" if is_person else "03/02/2015"),
        ("Permanent Account Number", pan_override or v["tax"]["pan"]),
    ], label_w=150, gap=30)
    c.rect(x + cw - 95, y + 25, 75, 90, fill=0, stroke=1)
    c.setFont("Helvetica", 7)
    c.drawCentredString(x + cw - 57, y + 68, "PHOTO" if is_person else "SEAL")
    c.save()


def cancelled_cheque(path, v):
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    b = v["bank"]
    x, y, cw, ch = 40, h - 330, 515, 230
    c.setFillColor(HexColor("#F3F8EE"))
    c.rect(x, y, cw, ch, fill=1, stroke=1)
    c.setFillColor(black)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(x + 15, y + ch - 28, b["bank_name"])
    c.setFont("Helvetica", 9)
    c.drawString(x + 15, y + ch - 42, f'Branch: {v["company"]["city"]} Main    IFSC: {b["ifsc"]}')
    c.drawRightString(x + cw - 15, y + ch - 28, "Date  __ __ / __ __ / __ __ __ __")
    c.setFont("Helvetica", 10)
    c.drawString(x + 15, y + ch - 80, "Pay ______________________________________________   or Bearer")
    c.drawString(x + 15, y + ch - 105, "Rupees ____________________________________________")
    c.drawString(x + 15, y + 80, f'A/c No.  {b["account_number"]}')
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(x + cw - 15, y + 60, f'For {b["account_holder"]}')
    c.setFont("Helvetica", 8)
    c.drawRightString(x + cw - 15, y + 46, "Authorised Signatory")
    c.setFont("Courier", 11)
    c.drawString(x + 60, y + 15, f'"000123"  {b["ifsc"][:4]}0{random.randint(10000, 99999)}:  {b["account_number"][-6:]}"  29')
    # The CANCELLED cross
    c.setStrokeColor(RED)
    c.setLineWidth(2.5)
    c.line(x + 120, y + 30, x + 360, y + ch - 20)
    c.line(x + 160, y + 30, x + 400, y + ch - 20)
    c.saveState()
    c.setFillColor(RED)
    c.setFont("Helvetica-Bold", 30)
    c.translate(x + 260, y + 115)
    c.rotate(43)
    c.drawCentredString(0, 0, "CANCELLED")
    c.restoreState()
    c.save()


def us_incorporation(path, v):
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    co = v["company"]
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 80, "Certificate of Incorporation")
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, h - 96, "(test specimen)")
    field_rows(c, 60, h - 150, [
        ("Entity Name", co["legal_name"]),
        ("Entity Type", "Corporation"),
        ("State of Formation", co["state"]),
        ("Date of Formation", "09/22/2017"),
        ("Registered Address", f'{co["address"]}, {co["city"]}, {co["state"]} {co["pin"]}'),
        ("Employer Identification No.", v["tax"]["tax_id"]),
    ])
    c.save()


def us_bank_letter(path, v):
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    b = v["bank"]
    c.setFont("Helvetica-Bold", 15)
    c.drawString(60, h - 80, b["bank_name"])
    c.setFont("Helvetica", 10)
    c.drawString(60, h - 96, "Account Verification Letter (test specimen)")
    c.drawString(60, h - 130, "To whom it may concern,")
    c.drawString(60, h - 148, "This letter confirms the following account is held with our bank and is in good standing.")
    field_rows(c, 60, h - 190, [
        ("Account Holder", b["account_holder"]),
        ("Account Number", b["account_number"]),
        ("ABA Routing Number", b["routing_number"]),
        ("SWIFT / BIC", b["swift"]),
        ("Account Type", "Business Checking"),
    ])
    c.drawString(60, h - 330, "Sincerely,")
    c.drawString(60, h - 346, "Branch Operations, First Harbor Bank (fictional)")
    c.save()


def us_tax_residency(path, v):
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    sample_marks(c, w, h)
    co = v["company"]
    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(w / 2, h - 80, "Tax Residency Certificate")
    c.setFont("Helvetica", 10)
    c.drawCentredString(w / 2, h - 96, "(test specimen modelled on IRS Form 6166)")
    c.drawString(60, h - 140, "This certifies that the taxpayer below is a resident of the United States")
    c.drawString(60, h - 156, "for purposes of U.S. taxation for the period stated.")
    field_rows(c, 60, h - 200, [
        ("Taxpayer Name", co["legal_name"]),
        ("Taxpayer ID (EIN)", v["tax"]["tax_id"]),
        ("Tax Period", "Calendar year 2026"),
    ])
    c.save()


def blurry_cheque_jpg(pdf_source_vendor, path):
    """Draw a cheque as an image, then rotate + blur + darken it to mimic a bad phone photo."""
    b = pdf_source_vendor["bank"]
    img = Image.new("RGB", (1100, 500), "#F3F8EE")
    d = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("DejaVuSans.ttf", 26)
        big = ImageFont.truetype("DejaVuSans-Bold.ttf", 34)
    except OSError:
        font = big = ImageFont.load_default()
    d.rectangle([5, 5, 1095, 495], outline="black", width=3)
    d.text((25, 20), b["bank_name"], fill="black", font=big)
    d.text((25, 70), f'IFSC: {b["ifsc"]}', fill="black", font=font)
    d.text((25, 300), f'A/c No. {b["account_number"]}', fill="black", font=font)
    d.text((560, 380), f'For {b["account_holder"]}', fill="black", font=font)
    d.text((300, 200), "CANCELLED", fill="#C62828", font=big)
    d.text((25, 450), "SAMPLE - FOR TESTING ONLY", fill="#C62828", font=font)
    img = img.rotate(17, expand=True, fillcolor="#6b6b6b")
    img = img.filter(ImageFilter.GaussianBlur(4.5))
    img = img.resize((img.width // 2, img.height // 2))
    img = img.point(lambda p: int(p * 0.7))
    img.save(path, quality=35)


# ---------------------------------------------------------------------------
# 4. Wire it together
# ---------------------------------------------------------------------------
TRUTH = {}  # filename -> what is actually printed on that document (answer key for tests)


def make_documents(v):
    """Create this vendor's files and return the 'documents' list for its JSON."""
    cid, ov = v["case_id"], v.get("_doc_overrides", {})
    co, tax, b = v["company"], v["tax"], v["bank"]
    docs = []
    gst_truth = {"document_type": "gst_certificate", "readable": True, "fields": {
        "gstin": tax.get("gstin"), "legal_name": co["legal_name"], "trade_name": co["trade_name"] or None}}
    pan_truth = {"document_type": "pan_card", "readable": True, "fields": {
        "pan": ov.get("pan_on_documents") or tax.get("pan"), "name": co["legal_name"].upper()}}
    cheque_truth = {"document_type": "cancelled_cheque", "readable": True, "fields": {
        "account_number": b["account_number"], "ifsc": b.get("ifsc"),
        "account_holder": b["account_holder"], "bank_name": b["bank_name"]}}

    def add(slot, filename, truth=None):
        docs.append({"slot": slot, "file": f"docs/{filename}"})
        if truth:
            TRUTH[filename] = truth

    if v["company"]["country"] == "US":
        us_incorporation(DOCS / f"{cid}__incorporation.pdf", v)
        us_bank_letter(DOCS / f"{cid}__bank_letter.pdf", v)
        us_tax_residency(DOCS / f"{cid}__tax_residency.pdf", v)
        add("incorporation_certificate", f"{cid}__incorporation.pdf")
        add("bank_proof", f"{cid}__bank_letter.pdf")
        add("tax_residency_certificate", f"{cid}__tax_residency.pdf")
        return docs

    # Indian vendor
    gst_certificate(DOCS / f"{cid}__gst_certificate.pdf", v,
                    address_override=ov.get("gst_address"))
    pan_card(DOCS / f"{cid}__pan_card.pdf", v, pan_override=ov.get("pan_on_documents"))
    add("gst_certificate", f"{cid}__gst_certificate.pdf", gst_truth)

    if ov.get("wrong_doc_in_cheque_slot"):
        # The vendor uploaded their PAN card a second time instead of a cheque.
        pan_card(DOCS / f"{cid}__cheque_slot_upload.pdf", v)
        add("bank_proof", f"{cid}__cheque_slot_upload.pdf", pan_truth)
    elif ov.get("blurry_cheque"):
        blurry_cheque_jpg(v, DOCS / f"{cid}__cancelled_cheque.jpg")
        add("bank_proof", f"{cid}__cancelled_cheque.jpg",
            {"document_type": "cancelled_cheque", "readable": False, "fields": {"bank_name": b["bank_name"]}})
    else:
        cancelled_cheque(DOCS / f"{cid}__cancelled_cheque.pdf", v)
        add("bank_proof", f"{cid}__cancelled_cheque.pdf", cheque_truth)

    add("pan_card", f"{cid}__pan_card.pdf", pan_truth)
    return docs


def main():
    random.seed(7)  # same output every run
    DOCS.mkdir(parents=True, exist_ok=True)
    DATA.mkdir(parents=True, exist_ok=True)

    for v in build_cases():
        v["documents"] = make_documents(v)
        v.pop("_doc_overrides", None)          # internal only, not part of the submission
        out = CASES / f'{v["case_id"]}.json'
        out.write_text(json.dumps(v, indent=2))
        print(f'wrote {out.relative_to(ROOT)}  ->  expected: {v["expected"]["status"]}')

    (CASES / "document_truth.json").write_text(json.dumps(TRUTH, indent=2))
    print("wrote test_cases/document_truth.json  (what is printed on each document)")

    with open(DATA / "existing_vendors.csv", "w", newline="") as f:
        wr = csv.writer(f)
        wr.writerow(["vendor_id", "legal_name", "pan", "gstin", "account_number", "ifsc"])
        for vid, name, pan, sc, acct, ifsc in EXISTING_VENDORS:
            wr.writerow([vid, name, pan, make_gstin(sc, pan), acct, ifsc])
    print("wrote data/existing_vendors.csv")


if __name__ == "__main__":
    main()
