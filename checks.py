"""
checks.py - every onboarding check, one small function each.

Each check looks at the submission and returns a CheckResult saying:
  - what it checked, what it found (evidence)
  - outcome:  pass / fail / info / skip
  - severity (only when it fails):
        fixable  -> the vendor can correct it        -> status "Pending - waiting on vendor"
        review   -> a human must judge it            -> status "Pending - internal review"
        blocker  -> cannot be onboarded              -> status "Rejected"
  - a reason code (for filtering on the dashboard)
  - a message for the procurement team, and (for fixable issues) a message for the vendor

No AI in this file. Anything with a right answer is plain code: free, instant, same result every time.
The two document checks (C13, C14) use what extract.py read off the documents, but the
COMPARING is still done here, by code.
"""

import csv
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from rapidfuzz import fuzz

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Reference data, loaded once
# ---------------------------------------------------------------------------
RULES = json.loads((ROOT / "rules" / "india.json").read_text())
BANK_NAMES = json.loads((ROOT / "data" / "bank_names.json").read_text())          # "HDFC" -> "HDFC Bank"
IFSC_BRANCHES = json.loads((ROOT / "data" / "ifsc_branches.json").read_text())    # "HDFC" -> ["000001", ...]
with open(ROOT / "data" / "existing_vendors.csv") as f:
    EXISTING_VENDORS = list(csv.DictReader(f))

STAGES = {
    1: "Completeness",
    2: "Format",
    3: "Consistency",
    4: "Documents (AI reading)",
    5: "Risk",
}


@dataclass
class CheckResult:
    id: str
    stage: int
    name: str
    outcome: str                      # pass | fail | info | skip
    message: str                      # plain English, for the procurement team
    severity: str | None = None       # fixable | review | blocker  (only when outcome == fail)
    code: str | None = None           # reason code, e.g. BANK_NAME_MISMATCH
    vendor_message: str | None = None # what to tell the vendor (fixable issues only)
    evidence: dict = field(default_factory=dict)

    def to_dict(self):
        return asdict(self)


def ok(cid, stage, name, message, **evidence):
    return CheckResult(cid, stage, name, "pass", message, evidence=evidence)


def skip(cid, stage, name, message):
    return CheckResult(cid, stage, name, "skip", message)


def fail(cid, stage, name, severity, code, message, vendor_message=None, **evidence):
    return CheckResult(cid, stage, name, "fail", message, severity, code, vendor_message, evidence)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def get(sub, dotted):
    """get(sub, 'bank.ifsc') -> sub['bank']['ifsc'], or '' if missing."""
    cur = sub
    for part in dotted.split("."):
        cur = (cur or {}).get(part)
    return cur if cur is not None else ""


def clean_id(s):
    """Upper-case and strip spaces/dashes: ' 29aakcs4821m1zs ' -> '29AAKCS4821M1ZS'."""
    return re.sub(r"[\s\-]", "", str(s or "")).upper()


def gst_registered(sub):
    return bool(get(sub, "tax.gst_registered"))


NAME_REPLACEMENTS = [
    (r"\bpvt\.?\b", "private"), (r"\bprivate\b", "private"),
    (r"\bltd\.?\b", "limited"), (r"\bco\.?\b", "company"), (r"\bcorp\.?\b", "corporation"),
    (r"&", " and "), (r"\bm/s\.?\b", ""), (r"\bthe\b", ""),
]


def normalise_name(name):
    """Make names comparable: 'M/s Sharma Packaging Pvt. Ltd.' -> 'sharma packaging private limited'."""
    s = str(name or "").lower()
    for pattern, repl in NAME_REPLACEMENTS:
        s = re.sub(pattern, repl, s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def name_score(a, b):
    """0-100 similarity after cleaning. token_sort_ratio ignores word order."""
    if not a or not b:
        return 0
    return round(fuzz.token_sort_ratio(normalise_name(a), normalise_name(b)))


def one_char_apart(a, b):
    """True if two equal-length IDs differ in exactly one position (a likely typo)."""
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
ACCOUNT_RE = re.compile(r"^[0-9]{9,18}$")
GST_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"


def gstin_checksum_ok(gstin):
    total = 0
    for i, ch in enumerate(gstin[:14]):
        product = GST_CHARS.index(ch) * (1 if i % 2 == 0 else 2)
        total += product // 36 + product % 36
    return GST_CHARS[(36 - total % 36) % 36] == gstin[14]


# ---------------------------------------------------------------------------
# Stage 1: Completeness
# ---------------------------------------------------------------------------
FIELD_LABELS = {
    "company.legal_name": "legal company name", "company.trade_name": "trade name",
    "company.business_type": "business type", "company.address": "registered address",
    "company.city": "city", "company.state": "state", "company.pin": "PIN code",
    "contact.name": "contact name", "contact.email": "contact email", "contact.phone": "contact phone",
    "tax.pan": "PAN", "tax.gstin": "GSTIN", "bank.account_holder": "bank account holder name",
    "bank.account_number": "bank account number", "bank.ifsc": "IFSC code", "bank.bank_name": "bank name",
}


def required_fields(sub):
    fields = list(RULES["required_fields"])
    if gst_registered(sub):
        fields += RULES["required_fields_if_gst_registered"]
    fields += RULES["required_fields_by_business_type"].get(get(sub, "company.business_type"), [])
    return fields


def required_docs(sub):
    docs = list(RULES["required_documents"])
    docs += RULES["required_documents_if_gst_registered" if gst_registered(sub)
                  else "required_documents_if_not_gst_registered"]
    return docs


def c1_required_fields(sub, ctx):
    name = "All required fields are filled"
    missing = [f for f in required_fields(sub) if not str(get(sub, f)).strip()]
    if not missing:
        return ok("C1", 1, name, f"All {len(required_fields(sub))} required fields are filled.")
    labels = [FIELD_LABELS.get(f, f) for f in missing]
    return fail("C1", 1, name, "fixable", "MISSING_FIELD",
                f"Missing: {', '.join(labels)}.",
                f"Please fill in: {', '.join(labels)}.", missing=missing)


def c2_required_documents(sub, ctx):
    name = "All required documents are uploaded"
    uploaded = {d["slot"] for d in sub.get("documents", [])}
    missing = [s for s in required_docs(sub) if s not in uploaded]
    if not missing:
        return ok("C2", 1, name, f"All {len(required_docs(sub))} required documents are uploaded.")
    labels = [RULES["document_slots"][s]["label"] for s in missing]
    return fail("C2", 1, name, "fixable", "MISSING_DOC",
                f"Missing documents: {', '.join(labels)}.",
                f"Please upload: {', '.join(labels)}.", missing=missing)


# ---------------------------------------------------------------------------
# Stage 2: Format
# ---------------------------------------------------------------------------
def c3_pan_format(sub, ctx):
    name = "PAN format"
    pan = clean_id(get(sub, "tax.pan"))
    if not pan:
        return skip("C3", 2, name, "No PAN given (see C1).")
    if PAN_RE.match(pan):
        return ok("C3", 2, name, f"{pan} has the right shape: 5 letters, 4 digits, 1 letter.", pan=pan)
    return fail("C3", 2, name, "fixable", "INVALID_FORMAT",
                f"'{pan}' is not a valid PAN (should be 5 letters, 4 digits, 1 letter).",
                f"The PAN '{pan}' doesn't look right. A PAN has 10 characters: 5 letters, 4 digits, 1 letter (e.g. ABCDE1234F).",
                pan=pan)


def c4_gstin_format(sub, ctx):
    name = "GSTIN format and checksum"
    if not gst_registered(sub):
        return skip("C4", 2, name, "Vendor says it isn't GST-registered, so there's no GSTIN to check.")
    gstin = clean_id(get(sub, "tax.gstin"))
    if not gstin:
        return skip("C4", 2, name, "No GSTIN given (see C1).")
    if not GSTIN_RE.match(gstin):
        return fail("C4", 2, name, "fixable", "INVALID_FORMAT",
                    f"'{gstin}' doesn't have the 15-character GSTIN structure.",
                    f"The GSTIN '{gstin}' doesn't look right. It should be 15 characters, e.g. 29ABCDE1234F1Z5.",
                    gstin=gstin)
    if not gstin_checksum_ok(gstin):
        return fail("C4", 2, name, "fixable", "INVALID_FORMAT",
                    f"'{gstin}' has the right shape but its last (check) character is wrong, so it's mistyped.",
                    f"The GSTIN '{gstin}' seems to have a typo. Please copy it exactly from your GST certificate.",
                    gstin=gstin)
    return ok("C4", 2, name, f"{gstin} has the right structure and a valid check digit.", gstin=gstin)


def c5_ifsc_format(sub, ctx):
    name = "IFSC format and branch exists"
    ifsc = clean_id(get(sub, "bank.ifsc"))
    if not ifsc:
        return skip("C5", 2, name, "No IFSC given (see C1).")
    if not IFSC_RE.match(ifsc):
        return fail("C5", 2, name, "fixable", "INVALID_FORMAT",
                    f"'{ifsc}' isn't a valid IFSC (4 letters, then 0, then 6 characters).",
                    f"The IFSC '{ifsc}' doesn't look right. It has 11 characters, e.g. HDFC0001234.", ifsc=ifsc)
    bank, branch = ifsc[:4], ifsc[5:]
    if bank not in IFSC_BRANCHES:
        return fail("C5", 2, name, "fixable", "UNKNOWN_IFSC",
                    f"Bank code '{bank}' in IFSC {ifsc} isn't in the RBI branch list.",
                    f"We couldn't find the IFSC '{ifsc}'. Please check it against your cheque book.", ifsc=ifsc)
    if branch not in IFSC_BRANCHES[bank]:
        return fail("C5", 2, name, "fixable", "UNKNOWN_IFSC",
                    f"{ifsc}: the bank ({BANK_NAMES.get(bank, bank)}) exists, but this branch code isn't in the RBI branch list.",
                    f"We couldn't find the IFSC '{ifsc}'. Please check it against your cheque book.", ifsc=ifsc)
    return ok("C5", 2, name, f"{ifsc} is a real {BANK_NAMES.get(bank, bank)} branch (RBI branch list).", ifsc=ifsc)


def c6_account_number_format(sub, ctx):
    name = "Bank account number format"
    acct = clean_id(get(sub, "bank.account_number"))
    if not acct:
        return skip("C6", 2, name, "No account number given (see C1).")
    if ACCOUNT_RE.match(acct):
        return ok("C6", 2, name, f"Account number has {len(acct)} digits (Indian accounts have 9-18).")
    return fail("C6", 2, name, "fixable", "INVALID_FORMAT",
                f"Account number '{acct}' should be 9-18 digits only.",
                "Your bank account number doesn't look right. It should be 9 to 18 digits.", account=acct)


# ---------------------------------------------------------------------------
# Stage 3: Consistency (fields that look fine alone but must agree with each other)
# ---------------------------------------------------------------------------
def c7_pan_in_gstin(sub, ctx):
    name = "PAN inside the GSTIN matches the PAN"
    gstin, pan = clean_id(get(sub, "tax.gstin")), clean_id(get(sub, "tax.pan"))
    if not gst_registered(sub) or not GSTIN_RE.match(gstin) or not PAN_RE.match(pan):
        return skip("C7", 3, name, "Needs a valid GSTIN and PAN first.")
    inner = gstin[2:12]
    if inner == pan:
        return ok("C7", 3, name, f"Characters 3-12 of the GSTIN ({inner}) equal the PAN.")
    if one_char_apart(inner, pan):
        # One character apart = almost certainly a typo. The vendor can fix it.
        return fail("C7", 3, name, "fixable", "PAN_GSTIN_MISMATCH",
                    f"The PAN field says {pan}, but the GSTIN contains {inner}. One character differs: likely a typo.",
                    f"Your PAN ({pan}) and the PAN inside your GSTIN ({inner}) differ by one character. "
                    f"Please check which is correct.",
                    pan=pan, pan_in_gstin=inner, fix_key="tax.pan")
    # Completely different = the GSTIN may belong to someone else. A human must look.
    return fail("C7", 3, name, "review", "PAN_GSTIN_MISMATCH",
                f"The GSTIN contains PAN {inner}, which is completely different from the PAN given ({pan}). "
                f"The GSTIN may belong to another business.",
                pan=pan, pan_in_gstin=inner)


def c8_gstin_state(sub, ctx):
    name = "GSTIN state matches the address"
    gstin = clean_id(get(sub, "tax.gstin"))
    if not gst_registered(sub) or not GSTIN_RE.match(gstin):
        return skip("C8", 3, name, "Needs a valid GSTIN first.")
    gst_state = RULES["gst_state_codes"].get(gstin[:2])
    addr_state = str(get(sub, "company.state")).strip()
    if gst_state and gst_state.lower() == addr_state.lower():
        return ok("C8", 3, name, f"GSTIN starts with {gstin[:2]} = {gst_state}, same as the address.")
    return fail("C8", 3, name, "fixable", "STATE_MISMATCH",
                f"GSTIN starts with {gstin[:2]} ({gst_state or 'unknown state'}), but the address is in {addr_state}.",
                f"Your GSTIN is registered in {gst_state or 'another state'} (it starts with {gstin[:2]}), but the "
                f"address you gave is in {addr_state}. Please confirm your registered address, or share the GSTIN "
                f"for your {addr_state} registration.",
                gst_state=gst_state, address_state=addr_state)


def c9_pin_state(sub, ctx):
    name = "PIN code matches the state"
    pin, state = str(get(sub, "company.pin")).strip(), str(get(sub, "company.state")).strip()
    if not re.fullmatch(r"[1-9][0-9]{5}", pin):
        return fail("C9", 3, name, "fixable", "INVALID_FORMAT", f"'{pin}' isn't a 6-digit PIN code.",
                    "Please give a valid 6-digit PIN code.", pin=pin)
    states = RULES["pin_prefix_states"].get(pin[:2])
    if states is None:
        return skip("C9", 3, name, f"No region data for PIN prefix {pin[:2]}.")
    if state.lower() in [s.lower() for s in states]:
        return ok("C9", 3, name, f"PIN {pin} is in the {' / '.join(states)} postal region, which matches {state}.")
    return fail("C9", 3, name, "fixable", "STATE_MISMATCH",
                f"PIN {pin} belongs to {' / '.join(states)}, not {state}.",
                f"Your PIN code {pin} is in {' / '.join(states)}, but you selected {state}. Please check your address.",
                pin=pin, pin_states=states, state=state)


def c10_pan_type(sub, ctx):
    name = "PAN type matches the business type"
    pan, btype = clean_id(get(sub, "tax.pan")), get(sub, "company.business_type")
    expected = RULES["expected_pan_type_by_business_type"].get(btype)
    if not PAN_RE.match(pan) or not expected:
        return skip("C10", 3, name, "Needs a valid PAN and business type first.")
    actual = pan[3]
    holder = RULES["pan_holder_type"].get(actual, "unknown")
    btype_label = RULES["business_types"][btype]
    if actual in expected:
        return ok("C10", 3, name, f"4th letter of PAN is '{actual}' ({holder}), as expected for a {btype_label}.")
    return fail("C10", 3, name, "fixable", "PAN_TYPE_MISMATCH",
                f"PAN {pan} is a '{holder}' PAN (4th letter '{actual}'), but the vendor says it's a {btype_label}. "
                f"Often a director's personal PAN entered by mistake.",
                f"The PAN you gave ({pan}) belongs to a {holder.lower()}, but your business is registered as a "
                f"{btype_label}. Please give the PAN of the business itself.",
                pan=pan, pan_type=holder, business_type=btype_label)


def c11_ifsc_bank_name(sub, ctx):
    name = "IFSC bank matches the bank name"
    ifsc, given = clean_id(get(sub, "bank.ifsc")), str(get(sub, "bank.bank_name")).strip()
    if not IFSC_RE.match(ifsc) or not given:
        return skip("C11", 3, name, "Needs a valid IFSC and bank name first.")
    official = BANK_NAMES.get(ifsc[:4])
    if not official:
        return skip("C11", 3, name, f"Bank code {ifsc[:4]} not in the bank list.")
    score = max(name_score(official, given), fuzz.partial_ratio(normalise_name(official), normalise_name(given)))
    if score >= 80:
        return ok("C11", 3, name, f"IFSC code {ifsc[:4]} = {official}, matches '{given}'.")
    return fail("C11", 3, name, "fixable", "BANK_MISMATCH",
                f"IFSC {ifsc} belongs to {official}, but the bank name given is '{given}'.",
                f"Your IFSC ({ifsc}) belongs to {official}, but you wrote '{given}' as the bank. Please check both.",
                official_bank=official, given_bank=given)


def c12_account_holder_name(sub, ctx):
    """The most important fraud check: is the money going to the vendor itself?"""
    name = "Bank account holder matches the company"
    holder = get(sub, "bank.account_holder")
    legal, trade = get(sub, "company.legal_name"), get(sub, "company.trade_name")
    if not holder or not legal:
        return skip("C12", 3, name, "Needs account holder and legal name first.")
    scores = {"legal name": name_score(holder, legal)}
    if trade:
        scores["trade name"] = name_score(holder, trade)
    best_against, best = max(scores.items(), key=lambda kv: kv[1])
    target = legal if best_against == "legal name" else trade
    th = RULES["name_match"]
    ev = {"account_holder": holder, "compared_with": target, "score": best, "all_scores": scores}

    if best >= th["pass_at"]:
        return ok("C12", 3, name, f"'{holder}' vs {best_against} '{target}': score {best}/100.", **ev)

    # Borderline: ask the AI for a second opinion, but a human still decides.
    if best >= th["review_below"] and ctx.get("name_judge"):
        verdict = ctx["name_judge"](holder, target)
        ev["ai_opinion"] = verdict
        if verdict and verdict.get("same_entity") and verdict.get("confidence", 0) >= 0.85:
            return ok("C12", 3, name, f"'{holder}' vs '{target}': score {best}/100 (borderline). "
                      f"AI judged them the same entity: {verdict.get('reason', '')}", **ev)

    risk = "likely a different person or entity" if best < th["review_below"] else "borderline"
    return fail("C12", 3, name, "review", "BANK_NAME_MISMATCH",
                f"Account holder '{holder}' vs {best_against} '{target}': score {best}/100 ({risk}). "
                f"Payments would go to an account not in the vendor's name. Common fraud pattern: "
                f"verify with the vendor by phone before approving.", **ev)


# ---------------------------------------------------------------------------
# Stage 4: Documents (uses what the AI read; the comparison itself is code)
# ---------------------------------------------------------------------------
MIN_CONFIDENCE = 0.6


def c13_document_type(sub, ctx):
    """One result per uploaded document: is it the document it claims to be, and readable?"""
    results = []
    for doc in sub.get("documents", []):
        slot = doc["slot"]
        slot_rules = RULES["document_slots"].get(slot, {"label": slot, "accepts": [slot]})
        label = slot_rules["label"]
        name = f"Right document: {label}"
        ex = ctx.get("extractions", {}).get(slot)
        if ex is None or ex.get("error"):
            results.append(fail("C13", 4, name, "review", "AI_UNAVAILABLE",
                                f"Couldn't read the {label}: {(ex or {}).get('error', 'not processed')}. "
                                f"Retry the run; nothing is approved without this step.", slot=slot))
            continue
        conf = ex.get("confidence", 0)
        if not ex.get("readable", True) or conf < MIN_CONFIDENCE:
            results.append(fail("C13", 4, name, "fixable", "UNREADABLE_DOC",
                                f"The {label} is too unclear to read reliably (AI confidence {conf:.0%}). "
                                f"Asking for a re-upload instead of guessing.",
                                f"Your {label} is too blurry or unclear for us to read. Please upload a clear, "
                                f"straight scan or PDF.", slot=slot, confidence=conf))
            continue
        dtype = ex.get("document_type")
        if dtype in slot_rules["accepts"]:
            results.append(ok("C13", 4, name, f"AI identified it as a {dtype.replace('_', ' ')} "
                              f"(confidence {conf:.0%}).", slot=slot, confidence=conf))
        else:
            got = (dtype or "unknown document").replace("_", " ")
            results.append(fail("C13", 4, name, "fixable", "WRONG_DOC",
                                f"Uploaded as a {label}, but the AI says it's a {got}.",
                                f"The file you uploaded as your {label} looks like a {got}. Please upload your {label}.",
                                slot=slot, detected=dtype, confidence=conf))
    return results


FIX_KEYS = {"PAN": "tax.pan", "GSTIN": "tax.gstin", "Account number": "bank.account_number", "IFSC": "bank.ifsc"}


def _compare_id(field_label, on_doc, on_form, doc_label, results, slot):
    """Exact match passes. One character off -> fixable typo. Anything else -> human review."""
    a, b = clean_id(on_doc), clean_id(on_form)
    if not a:
        return
    if a == b:
        results.append(("pass", f"{field_label} on {doc_label} ({a}) matches the form."))
    elif one_char_apart(a, b):
        results.append(("fixable", f"{field_label} on {doc_label} is {a}; the form says {b} (one character "
                        f"off, likely a typo on the form).",
                        f"The {field_label} on your {doc_label} is {a}, but you typed {b}. Please correct it.",
                        FIX_KEYS.get(field_label)))
    else:
        results.append(("review", f"{field_label} on {doc_label} is {a}, but the form says {b}."))


def _compare_name(field_label, on_doc, on_form, doc_label, results):
    if not on_doc:
        return
    s = name_score(on_doc, on_form)
    if s >= 85:
        results.append(("pass", f"{field_label} on {doc_label} ('{on_doc}') matches the form (score {s})."))
    else:
        results.append(("review", f"{field_label} on {doc_label} is '{on_doc}', but the form says "
                        f"'{on_form}' (score {s})."))


def c14_document_details(sub, ctx):
    """Compare what the AI read off each document with what the vendor typed into the form."""
    results = []
    for doc in sub.get("documents", []):
        slot = doc["slot"]
        ex = ctx.get("extractions", {}).get(slot) or {}
        label = RULES["document_slots"].get(slot, {"label": slot})["label"]
        name = f"Details match the form: {label}"
        accepts = RULES["document_slots"].get(slot, {}).get("accepts", [slot])
        if ex.get("error") or ex.get("document_type") not in accepts or ex.get("confidence", 0) < MIN_CONFIDENCE:
            results.append(skip("C14", 4, name, "Skipped: the document itself failed C13."))
            continue
        f = ex.get("fields", {})
        comps = []
        legal = get(sub, "company.legal_name")
        if slot == "gst_certificate":
            _compare_id("GSTIN", f.get("gstin"), get(sub, "tax.gstin"), label, comps, slot)
            _compare_name("Legal name", f.get("legal_name"), legal, label, comps)
        elif slot == "pan_card":
            _compare_id("PAN", f.get("pan"), get(sub, "tax.pan"), label, comps, slot)
            _compare_name("Name", f.get("name") or f.get("legal_name"), legal, label, comps)
        elif slot == "bank_proof":
            _compare_id("Account number", f.get("account_number"), get(sub, "bank.account_number"), label, comps, slot)
            _compare_id("IFSC", f.get("ifsc"), get(sub, "bank.ifsc"), label, comps, slot)
            _compare_name("Account holder", f.get("account_holder"), get(sub, "bank.account_holder"), label, comps)

        if not comps:
            results.append(skip("C14", 4, name, "No comparable details were found on the document."))
            continue
        worst = "review" if any(c[0] == "review" for c in comps) else \
                "fixable" if any(c[0] == "fixable" for c in comps) else "pass"
        detail = " ".join(c[1] for c in comps)
        ev = {"slot": slot, "read_from_document": f, "comparisons": [c[1] for c in comps]}
        if worst == "pass":
            results.append(ok("C14", 4, name, detail, **ev))
        elif worst == "fixable":
            fixes = [c for c in comps if c[0] == "fixable"]
            vmsg = " ".join(c[2] for c in fixes)
            results.append(fail("C14", 4, name, "fixable", "DOC_FIELD_MISMATCH", detail, vmsg,
                                fix_key=fixes[0][3] if len(fixes) == 1 else None, **ev))
        else:
            results.append(fail("C14", 4, name, "review", "DOC_FIELD_MISMATCH", detail, **ev))
    return results


# ---------------------------------------------------------------------------
# Stage 5: Risk
# ---------------------------------------------------------------------------
def c15_duplicate_vendor(sub, ctx):
    name = "Not a duplicate of an existing vendor"
    pan, gstin = clean_id(get(sub, "tax.pan")), clean_id(get(sub, "tax.gstin"))
    acct, ifsc = clean_id(get(sub, "bank.account_number")), clean_id(get(sub, "bank.ifsc"))
    vendors = EXISTING_VENDORS + ctx.get("approved_vendors", [])

    same_bank_other_vendor, same_identity = [], []
    for v in vendors:
        v_pan = clean_id(v.get("pan"))
        bank_match = acct and clean_id(v.get("account_number")) == acct and clean_id(v.get("ifsc"))[:4] == ifsc[:4]
        id_match = (pan and v_pan == pan) or (gstin and clean_id(v.get("gstin")) == gstin)
        if bank_match and v_pan != pan:
            same_bank_other_vendor.append(v)
        elif id_match:
            same_identity.append(v)

    if same_bank_other_vendor:
        v = same_bank_other_vendor[0]
        return fail("C15", 5, name, "blocker", "DUPLICATE_BANK_ACCOUNT",
                    f"Bank account {acct} is already registered to a different vendor: {v['legal_name']} "
                    f"({v['vendor_id']}). Two businesses don't share a bank account; possible fake vendor.",
                    matched_vendor=v["vendor_id"])
    if same_identity:
        v = same_identity[0]
        return fail("C15", 5, name, "review", "DUPLICATE_VENDOR",
                    f"Same PAN/GSTIN as existing vendor {v['legal_name']} ({v['vendor_id']}). "
                    f"May already be onboarded; check before creating a second record.",
                    matched_vendor=v["vendor_id"])
    return ok("C15", 5, name, f"PAN, GSTIN and bank account don't match any of {len(vendors)} existing vendors.")


def c16_email_domain(sub, ctx):
    name = "Contact email domain"
    email = str(get(sub, "contact.email")).strip().lower()
    if "@" not in email:
        return fail("C16", 5, name, "fixable", "INVALID_FORMAT", f"'{email}' isn't an email address.",
                    "Please give a valid contact email address.")
    domain = email.split("@")[1]
    if domain in RULES["free_email_domains"] and get(sub, "company.business_type") != "proprietor":
        return CheckResult("C16", 5, name, "info",
                           f"Uses a free email ({domain}) for a registered company. Noted, not blocking.",
                           code="FREE_EMAIL_DOMAIN")
    return ok("C16", 5, name, f"Email domain {domain} is fine.")


# Order matters: this is the order the live run shows them.
RULE_STAGES = {
    1: [c1_required_fields, c2_required_documents],
    2: [c3_pan_format, c4_gstin_format, c5_ifsc_format, c6_account_number_format],
    3: [c7_pan_in_gstin, c8_gstin_state, c9_pin_state, c10_pan_type, c11_ifsc_bank_name, c12_account_holder_name],
}
DOC_CHECKS = [c13_document_type, c14_document_details]
RISK_CHECKS = [c15_duplicate_vendor, c16_email_domain]
