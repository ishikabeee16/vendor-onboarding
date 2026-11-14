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
    elif status == PENDING_REVIEW and any(r.get("code") == "AI_UNAVAILABLE" for r in by_sev("review")):
        bad = [r for r in by_sev("review") if r.get("code") == "AI_UNAVAILABLE"]
        text = (f"On hold: {len(bad)} document(s) couldn't be read by the AI, so the run can't be completed. "
                f"Nothing is approved without reading the documents. Re-run the checks once document reading "
                f"is available.")
        others = [r for r in by_sev("review") if r.get("code") != "AI_UNAVAILABLE"]
        if others:
            text += " Also needs review: " + " ".join(r["message"] for r in others[:2])
    elif status == PENDING_REVIEW:
        rv = by_sev("review")
        text = "Needs a human decision. " + " ".join(r["message"] for r in rv[:2])
        if len(rv) > 2:
            text += f" (+{len(rv) - 2} more)"
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
