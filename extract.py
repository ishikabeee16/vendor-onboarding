"""
extract.py - the AI part. It does exactly two jobs:
  1. read_document(): look at an uploaded PDF/photo and return what it is + the details on it, as JSON
  2. judge_names():   for borderline name matches only, give a second opinion

AI never decides the status. It reads; checks.py compares; decide.py decides.

Model: Google Gemini. Set GEMINI_API_KEY (and optionally GEMINI_MODEL) in
.streamlit/secrets.toml locally, or in the Streamlit Cloud "Secrets" box.

Results are cached by the file's fingerprint (SHA-256), so the same document is never
read twice. That makes demo re-runs instant and keeps us inside the free-tier limits.
"""

import hashlib
import json
import os
import time

# Google retires older models, and a name that works today can stop working for new API keys
# later. So we keep a list, newest first, try them in order, and remember the one that answers.
MODEL_CANDIDATES = ["gemini-3.8-flash", "gemini-3.5-flash-lite", "gemini-2.5-flash"]
DEFAULT_MODEL = MODEL_CANDIDATES[0]

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


_working_model = None


def model_name():
    """The model we're actually using: whichever one answered last, else the configured one."""
    return _working_model or _secret("GEMINI_MODEL", DEFAULT_MODEL)


def _models_to_try():
    """The configured model first, then the rest as fallbacks, without repeats."""
    order = [model_name()] + MODEL_CANDIDATES
    seen, out = set(), []
    for m in order:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def ai_available():
    return bool(_secret("GEMINI_API_KEY"))


def fingerprint(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


_CLIENTS = {}


def _client():
    """One client per API key, kept for the life of the process.

    This must be cached. A client created fresh for each call is the only thing referring to its
    network connection, so Python can collect it while the request is still in flight, and the
    call then fails with "Cannot send a request, as the client has been closed".
    """
    from google import genai
    from google.genai import types
    key = _secret("GEMINI_API_KEY")
    if key not in _CLIENTS:
        _CLIENTS.clear()
        # Give up after 45 seconds. Without this a stuck call hangs the run for ever, and a
        # spinner that never stops is worse than a clear "couldn't read it, please retry".
        _CLIENTS[key] = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=45_000))
    return _CLIENTS[key]


def _call_gemini(contents, schema=None, retries=3):
    """One Gemini call with JSON output. Retries on rate limits (429) with a growing wait."""
    from google.genai import types
    config = types.GenerateContentConfig(
        temperature=0,  # same input -> same answer, as far as possible
        response_mime_type="application/json",
        **({"response_schema": schema} if schema else {}),
    )
    global _working_model
    last_err = None
    for model in _models_to_try():
        for attempt in range(retries):
            try:
                client = _client()   # a local name keeps it alive for the whole request
                resp = client.models.generate_content(model=model, contents=contents, config=config)
                _working_model = model      # remember it, so later calls skip the dead ones
                return json.loads(resp.text)
            except Exception as e:  # network error, rate limit, retired model, bad JSON...
                last_err = e
                msg = str(e)
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "503" in msg:
                    time.sleep(4 * (attempt + 1))
                    continue
                break
        if not ("404" in str(last_err) or "NOT_FOUND" in str(last_err)):
            break                           # a real failure, not "this model is gone"
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
