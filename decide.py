"""
decide.py - turns all the check results into ONE decision, a plain-English summary, and the vendor email.

The rule: the worst failure decides.
  any blocker              -> Rejected
  else any review item     -> Pending - internal review   (no email to vendor until a human decides)
  else any fixable item    -> Pending - waiting on vendor (one email listing every fix)
  else                     -> Approved                    (vendor ID + welcome email)

Why not an average score? One bank-name mismatch is enough to stop a payment. Averaging it with
ten passed checks would hide it.

The vendor email is built from fixed templates, not written by AI, so it can never invent or drop
an issue: each fixable check carries its own vendor_message, written in checks.py.
"""

APPROVED = "Approved"
PENDING_VENDOR = "Pending - waiting on vendor"
PENDING_REVIEW = "Pending - internal review"
REJECTED = "Rejected"
ALL_STATUSES = [APPROVED, PENDING_VENDOR, PENDING_REVIEW, REJECTED]

# Reason codes in plain words (the dashboard shows these next to the code).
REASON_TEXT = {
    "MISSING_FIELD": "A required field was left empty",
    "MISSING_DOC": "A required document was not uploaded",
    "WRONG_DOC": "The wrong document was uploaded",
    "UNREADABLE_DOC": "A document was too unclear to read",
    "INVALID_FORMAT": "A number has the wrong format (PAN, GSTIN, IFSC, account, PIN, email)",
    "UNKNOWN_IFSC": "The IFSC is not a real bank branch",
    "PAN_GSTIN_MISMATCH": "The PAN and the GSTIN do not belong together",
    "PAN_TYPE_MISMATCH": "The PAN type does not fit the business type",
    "STATE_MISMATCH": "The state, GSTIN and PIN code disagree",
    "BANK_MISMATCH": "The IFSC belongs to a different bank than named",
    "BANK_NAME_MISMATCH": "The bank account is not in the vendor's name",
    "DOC_FIELD_MISMATCH": "A document says something different from the form",
    "DUPLICATE_VENDOR": "This vendor may already exist",
    "DUPLICATE_BANK_ACCOUNT": "Another vendor uses the same bank account",
    "FREE_EMAIL_DOMAIN": "Free email address used by a company (note only)",
    "AI_UNAVAILABLE": "Document reading was not available",
    "RUN_FAILED": "The checks did not finish",
}

BUYER = "Acme Retail Pvt Ltd"
PROCUREMENT_EMAIL = "vendor-onboarding@acmeretail.example"


def decide(results: list[dict]) -> str:
    failed = [r for r in results if r["outcome"] == "fail"]
    if any(r["severity"] == "blocker" for r in failed):
        return REJECTED
    if any(r["severity"] == "review" for r in failed):
        return PENDING_REVIEW
    if any(r["severity"] == "fixable" for r in failed):
        return PENDING_VENDOR
    return APPROVED


def reason_codes(results):
    codes = []
    for r in results:
        if r["outcome"] in ("fail", "info") and r.get("code") and r["code"] not in codes:
            codes.append(r["code"])
    return codes


def vendor_fixes(results):
    """The list of things to ask the vendor for. Two checks about the same form field (e.g. the PAN
    typo caught by both the GSTIN cross-check and the PAN card) become ONE request; the later one
    wins because it's backed by a document."""
    fixes = {}
    for r in results:
        if r["outcome"] == "fail" and r["severity"] == "fixable" and r.get("vendor_message"):
            key = (r.get("evidence") or {}).get("fix_key") or r["vendor_message"]
            fixes.pop(key, None)
            fixes[key] = r["vendor_message"]
    return list(fixes.values())


def summary(status, results, vendor_name):
    """One short paragraph for the procurement executive: status, why, what to do next."""
    by_sev = lambda s: [r for r in results if r["outcome"] == "fail" and r["severity"] == s]
    infos = [r for r in results if r["outcome"] == "info"]
    passed = sum(r["outcome"] == "pass" for r in results)
    total = sum(r["outcome"] != "skip" for r in results)

    if status == APPROVED:
        text = f"{vendor_name} passed all {passed} checks and is approved."
    elif status == REJECTED:
        b = by_sev("blocker")
        text = f"Rejected: {b[0]['message']}"
        if len(b) > 1:
            text += f" (+{len(b) - 1} more blocking issue(s))"
        text += " The vendor gets a neutral rejection email that doesn't say which check failed."
    elif status == PENDING_REVIEW:
        # Findings about the vendor come first; a document the AI couldn't read is a problem with our
        # own run, so it goes last. Otherwise a rate-limited API would bury a real fraud signal.
        rv = [r for r in by_sev("review") if r.get("code") != "AI_UNAVAILABLE"]
        unread = [r for r in by_sev("review") if r.get("code") == "AI_UNAVAILABLE"]
        if rv:
            text = "Needs a human decision. " + " ".join(r["message"] for r in rv[:2])
            if len(rv) > 2:
                text += f" (+{len(rv) - 2} more)"
        else:
            text = "Needs a human decision."
        if unread:
            text += (f" Separately, {len(unread)} document(s) couldn't be read by the AI, so the run is "
                     f"incomplete. Nothing is approved without reading the documents: re-run the checks "
                     f"once document reading is available.")
        fx = by_sev("fixable")
        if fx:
            text += (f" There are also {len(fx)} fixable issue(s); the vendor email is held until a reviewer "
                     f"decides, so we don't ask for typo fixes while a fraud question is open.")
    else:
        fx = vendor_fixes(results)
        text = (f"Waiting on the vendor: {len(fx)} thing(s) to fix. "
                f"An email listing each one with the exact fix is ready to send.")
    if infos:
        text += " Note: " + " ".join(r["message"] for r in infos)
    return text + f" ({passed}/{total} checks passed.)"


def vendor_email(status, results, payload, request_ref, vendor_id=None):
    contact = payload.get("contact", {})
    name = payload.get("company", {}).get("legal_name", "your company")
    first = (contact.get("name") or "there").split()[0]
    to = contact.get("email", "")

    if status == PENDING_REVIEW:
        return None  # held: a human must decide first

    if status == APPROVED:
        return {"to": to, "subject": f"You're registered as a vendor with {BUYER} ({vendor_id})",
                "body": f"Hi {first},\n\nGood news: {name} is now an approved vendor with {BUYER}.\n\n"
                        f"Your vendor ID is {vendor_id}. Please quote it on every invoice.\n\n"
                        f"Regards,\nVendor Onboarding Team, {BUYER}"}

    if status == REJECTED:
        return {"to": to, "subject": f"Your vendor registration with {BUYER} (Ref {request_ref})",
                "body": f"Hi {first},\n\nThank you for your interest in working with {BUYER}. We're unable to "
                        f"complete this registration.\n\nIf you think this is a mistake, please contact "
                        f"{PROCUREMENT_EMAIL} quoting reference {request_ref}.\n\n"
                        f"Regards,\nVendor Onboarding Team, {BUYER}"}

    # Pending - waiting on vendor: one numbered list of every fix
    fixes = vendor_fixes(results)
    lines = "\n".join(f"{i}. {msg}" for i, msg in enumerate(fixes, 1))
    return {"to": to, "subject": f"Action needed: your vendor registration with {BUYER} (Ref {request_ref})",
            "body": f"Hi {first},\n\nThanks for registering {name}. We need {len(fixes)} thing(s) from you "
                    f"before we can approve it:\n\n{lines}\n\nPlease update your submission (reference "
                    f"{request_ref}). Everything else is already verified, so you won't need to re-enter it.\n\n"
                    f"Regards,\nVendor Onboarding Team, {BUYER}"}


def vendor_view(status, results, request_ref, vendor_id=None):
    """What the VENDOR is allowed to see. Deliberately much less than the team sees:
    no check names, no reason codes, and above all nothing about fraud or review reasons.
    Telling a fraudster which check caught them teaches them what to change next time."""
    if status == APPROVED:
        return {"tone": "good", "title": "Approved",
                "message": f"Your registration is approved. Your vendor ID is {vendor_id}. "
                           f"Please quote it on every invoice.", "fixes": []}
    if status == PENDING_VENDOR:
        fixes = vendor_fixes(results)
        return {"tone": "action", "title": "We need a few corrections from you",
                "message": f"Please fix the {len(fixes)} item(s) below and resubmit. Everything else is fine, "
                           f"so you won't need to re-enter it.", "fixes": fixes}
    if status == REJECTED:
        return {"tone": "stop", "title": "We couldn't complete this registration",
                "message": f"If you think this is a mistake, please contact {PROCUREMENT_EMAIL} "
                           f"and quote reference {request_ref}.", "fixes": []}
    # Internal review, still running, or did not finish: the vendor just sees "we're on it".
    return {"tone": "wait", "title": "Received - under review",
            "message": "Thank you. Our team is reviewing your registration and will contact you if "
                       "anything else is needed. You don't need to do anything right now.", "fixes": []}
