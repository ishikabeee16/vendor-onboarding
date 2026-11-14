"""
extract.py - the AI part. It does exactly two jobs:
  1. read_document(): look at an uploaded PDF/photo and return what it is + the details on it, as JSON
  2. judge_names():   for borderline name matches only, give a second opinion

AI never decides the status. It reads; checks.py compares; decide.py decides.

Model: Google Gemini (free tier). Set GEMINI_API_KEY (and optionally GEMINI_MODEL) in
.streamlit/secrets.toml locally, or in the Streamlit Cloud "Secrets" box.

Results are cached by the file's fingerprint (SHA-256), so the same document is never
read twice. That makes demo re-runs instant and keeps us inside the free-tier limits.
"""

import hashlib
import json
import os
import time

DEFAULT_MODEL = "gemini-2.5-flash"

DOC_PROMPT = """You are reading a document uploaded during vendor onboarding at an Indian company.

1. Identify the document type. Use exactly one of:
   gst_certificate   - GST registration certificate (Form GST REG-06)
   pan_card          - PAN card or PAN allotment letter
   cancelled_cheque  - a cheque leaf (usually crossed / marked CANCELLED)
   bank_letter       - a letter from a bank confirming account details
   gst_declaration   - a declaration that the business is not registered under GST
   other             - anything else

2. Extract these fields if they appear on the document (use null if not present; never guess):
   legal_name, trade_name, name (the name printed on a PAN card), gstin, pan,
   account_holder, account_number, ifsc, bank_name, address

3. readable: false if the document is too blurry, cut off or unclear to read the key numbers reliably.
4. confidence: 0.0 to 1.0, how sure you are about the type AND the key numbers you extracted.
   If any digit of a key number (GSTIN, PAN, account number, IFSC) is uncertain, confidence must be below 0.6.
5. notes: one short sentence about anything unusual (e.g. "marked SAMPLE", "rotated photo", "name handwritten").

Copy numbers exactly as printed, without spaces. Return only JSON."""

DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "document_type": {"type": "string", "enum": ["gst_certificate", "pan_card", "cancelled_cheque",
                                                     "bank_letter", "gst_declaration", "other"]},
        "readable": {"type": "boolean"},
        "confidence": {"type": "number"},
        "fields": {
            "type": "object",
            "properties": {k: {"type": "string", "nullable": True} for k in
                           ["legal_name", "trade_name", "name", "gstin", "pan", "account_holder",
                            "account_number", "ifsc", "bank_name", "address"]},
        },
        "notes": {"type": "string"},
    },
    "required": ["document_type", "readable", "confidence", "fields"],
}

NAME_PROMPT = """Two names come from a vendor onboarding form in India.
Company / legal name: "{a}"
Bank account holder name: "{b}"
Do they refer to the same legal entity? Abbreviations, word order, punctuation and suffixes
(Pvt Ltd / Private Limited, & / and) don't matter. A person's name vs a company name is NOT the same entity.
Return JSON: {{"same_entity": true/false, "confidence": 0.0-1.0, "reason": "<one sentence>"}}"""


def _secret(name, default=None):
    """Read a setting from Streamlit secrets if available, else from environment variables."""
    try:
        import streamlit as st
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name, default)


def model_name():
    return _secret("GEMINI_MODEL", DEFAULT_MODEL)


def ai_available():
    return bool(_secret("GEMINI_API_KEY"))


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _client():
    from google import genai
    return genai.Client(api_key=_secret("GEMINI_API_KEY"))


def _call_gemini(contents, schema=None, retries=3):
    """One Gemini call with JSON output. Retries on rate limits (429) with a growing wait."""
    from google.genai import types
    config = types.GenerateContentConfig(
        temperature=0,  # same input -> same answer, as far as possible
        response_mime_type="application/json",
        **({"response_schema": schema} if schema else {}),
    )
    last_err = None
    for attempt in range(retries):
        try:
            resp = _client().models.generate_content(model=model_name(), contents=contents, config=config)
            return json.loads(resp.text)
        except Exception as e:  # network error, rate limit, bad JSON...
            last_err = e
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e) or "503" in str(e):
                time.sleep(4 * (attempt + 1))
                continue
            break
    raise RuntimeError(f"{type(last_err).__name__}: {str(last_err)[:200]}")


def read_document(data: bytes, mime_type: str, cache=None) -> dict:
    """Return {document_type, readable, confidence, fields{...}, notes, model, cached, seconds}
    or {error: "..."} if the AI couldn't be reached."""
    key = f"{fingerprint(data)}:{model_name()}"
    if cache is not None:
        hit = cache.get(key)
        if hit:
            return {**hit, "cached": True, "seconds": 0.0}
    if not ai_available():
        return {"error": "No GEMINI_API_KEY configured"}

    from google.genai import types
    start = time.time()
    try:
        result = _call_gemini([types.Part.from_bytes(data=data, mime_type=mime_type), DOC_PROMPT], DOC_SCHEMA)
    except RuntimeError as e:
        return {"error": str(e)}
    result["fields"] = {k: v for k, v in (result.get("fields") or {}).items() if v not in (None, "", "null")}
    result["model"] = model_name()
    if cache is not None:
        cache.put(key, result)
    return {**result, "cached": False, "seconds": round(time.time() - start, 1)}


def judge_names(a: str, b: str) -> dict | None:
    """Second opinion for borderline name matches. Returns None if AI isn't available."""
    if not ai_available():
        return None
    try:
        return _call_gemini([NAME_PROMPT.format(a=a, b=b)])
    except RuntimeError:
        return None
