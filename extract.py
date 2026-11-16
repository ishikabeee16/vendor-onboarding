"""
extract.py - the AI part. It does exactly two jobs:
  1. read_document(): read an uploaded PDF/photo (twice) and return what it is + the details on it
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

# Which Gemini models to use, in order. Three lessons from testing on the live app:
#   1. Google retires models: "gemini-2.5-flash" now returns "not available" for new API keys.
#   2. On the free tier the bigger "Flash" models are often overloaded (error 503) or just hang,
#      while the "Flash-Lite" models answer in 1-3 seconds. So Lite models come first.
#   3. Any single model can be down at any moment, so we never depend on one: if a call fails or
#      takes too long we move straight to the next model instead of waiting and retrying.
MODEL_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-flash-lite-latest", "gemini-3.1-flash-lite",
                    "gemini-3.8-flash"]
RETIRED_MODELS = {"gemini-2.5-flash", "gemini-2.0-flash", "gemini-1.5-flash"}
DEFAULT_MODEL = MODEL_CANDIDATES[0]
CALL_TIMEOUT_SECONDS = 15
# The numbers that matter for payment. Two independent readings must agree on every one of them.
KEY_NUMBERS = ["gstin", "pan", "account_number", "ifsc"]

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

Copy numbers exactly as printed, without spaces.
Return only JSON with exactly these keys: document_type, readable, confidence, fields, notes."""

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


_settings_cache = {}


def _settings():
    """API key and preferred model, read once. (Reading them once on the main thread also means
    the background threads that read documents never have to touch Streamlit's secrets.)"""
    if not _settings_cache:
        configured = _secret("GEMINI_MODEL")
        if configured in RETIRED_MODELS:
            configured = None                  # an old setting must not point us at a dead model
        _settings_cache.update(key=_secret("GEMINI_API_KEY"), model=configured)
    return _settings_cache


_health = {}    # model name -> time of its last failure, so we skip models that just failed


def _models_to_try():
    """Preferred model first, then the others. Models that failed in the last 2 minutes go last."""
    order = [_settings()["model"]] + MODEL_CANDIDATES
    seen, fresh, recently_failed = set(), [], []
    for m in order:
        if m and m not in seen:
            seen.add(m)
            (recently_failed if time.time() - _health.get(m, 0) < 120 else fresh).append(m)
    return fresh + recently_failed


def model_name():
    return _models_to_try()[0]


def ai_available():
    return bool(_settings()["key"])


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
    key = _settings()["key"]
    if key not in _CLIENTS:
        _CLIENTS.clear()
        _CLIENTS[key] = genai.Client(
            api_key=key, http_options=types.HttpOptions(timeout=CALL_TIMEOUT_SECONDS * 1000))
    return _CLIENTS[key]


def _call_gemini(contents, schema=None, skip_models=()):
    """One Gemini call with JSON output. Returns (answer, model_used).

    If a model is overloaded, rate-limited, retired or slow, we do NOT wait for it: we note that
    it failed and go straight to the next one. Only a "bad request" (our input was refused) stops
    the loop, because every other model would refuse it too.
    """
    from google.genai import types
    config = types.GenerateContentConfig(
        temperature=0,  # same input -> same answer, as far as possible
        response_mime_type="application/json",
        **({"response_schema": schema} if schema else {}),
    )
    client = _client()       # a local name keeps it alive for the whole request
    last_err = None
    models = [m for m in _models_to_try() if m not in skip_models] or _models_to_try()
    for model in models:
        try:
            resp = client.models.generate_content(model=model, contents=contents, config=config)
            return json.loads(resp.text), model
        except Exception as e:  # overloaded, rate limit, retired model, timeout, bad JSON...
            last_err = e
            _health[model] = time.time()
            if "400" in str(e) or "INVALID_ARGUMENT" in str(e):
                raise ValueError(f"The file could not be opened: {str(e)[:150]}")
    raise RuntimeError(f"{type(last_err).__name__}: {str(last_err)[:200]}")


def _norm(v):
    return "".join(str(v or "").split()).upper()


def _one_read(part, skip_models=()):
    # We ask for JSON in the prompt rather than forcing a strict schema: tested on the live API,
    # it is accepted by every model in the list, where a strict schema is one more thing to break.
    result, model = _call_gemini([part, DOC_PROMPT], None, skip_models)
    if not isinstance(result, dict):
        raise RuntimeError("The AI answered in an unexpected format")
    fields = result.get("fields") if isinstance(result.get("fields"), dict) else {}
    result["fields"] = {k: str(v) for k, v in fields.items() if v not in (None, "", "null")}
    result.setdefault("document_type", "other")
    result.setdefault("readable", True)
    try:
        result["confidence"] = float(result.get("confidence", 0))
    except (TypeError, ValueError):
        result["confidence"] = 0.0
    return result, model


def _confirmed_by_form(fields, expect):
    """True if every key number the vendor typed appears, identical, in what the AI read."""
    return bool(expect) and all(_norm(fields.get(k)) == _norm(v) for k, v in expect.items() if v)


def read_document(data: bytes, mime_type: str, cache=None, expect=None) -> dict:
    """Read one document, and decide how far the reading can be trusted.

    `expect` = the key numbers the vendor typed on the form for this document (e.g. the account
    number and IFSC for a cheque).

    Why this is careful: in testing, a blurry cheque photo was read three times and gave three
    different account numbers, each time with "85-95% confidence". The AI's own confidence can't
    be trusted on its own. So a number is only trusted when two independent sources agree:
      - the AI's reading matches what the vendor typed  -> trusted (form + AI agree), or
      - it doesn't match, so we read the document a SECOND time, with a different model if we can:
          both readings agree  -> the document really does say something different from the form
          readings disagree    -> the document is not clear enough; ask for a better copy.

    Returns {document_type, readable, confidence, fields, notes, second_read{...}, cached, seconds}
    or {error: "..."} if no AI model could be reached at all.
    """
    key = f"{fingerprint(data)}:v3"
    start = time.time()
    first = cache.get(key) if cache is not None else None
    cached = first is not None
    if first is None:
        if not ai_available():
            return {"error": "Document reading is not configured"}
        from google.genai import types
        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        try:
            first, model_a = _one_read(part)
        except ValueError as e:          # the file itself is broken (corrupt PDF, not an image...)
            return {"document_type": "other", "readable": False, "confidence": 0.0, "fields": {},
                    "notes": str(e), "cached": False, "seconds": round(time.time() - start, 1)}
        except RuntimeError as e:        # no model reachable
            return {"error": str(e)}
        first["_model"] = model_a
        if cache is not None:
            cache.put(key, first)

    result = {k: v for k, v in first.items() if not k.startswith("_")}
    done = lambda: {**result, "cached": cached, "seconds": round(time.time() - start, 1)}   # noqa: E731

    if first.get("second_read", {}).get("agrees") is not None:
        return done()                    # a real second reading was already done for this file
    if _confirmed_by_form(result["fields"], expect):
        result["second_read"] = {"agrees": None, "confirmed_by_form": True, "differs_on": [], "values": {}}
        return done()

    # The reading and the form differ (or there was nothing to compare with): read it again.
    try:
        from google.genai import types
        part = types.Part.from_bytes(data=data, mime_type=mime_type)
        second, _ = _one_read(part, skip_models=(first.get("_model"),))
    except (RuntimeError, ValueError):
        result["second_read"] = {"agrees": None, "differs_on": [], "values": {}}   # say so; not verified
        return done()
    disagree = [k for k in KEY_NUMBERS
                if (k in result["fields"] or k in second["fields"])
                and _norm(result["fields"].get(k)) != _norm(second["fields"].get(k))]
    if result.get("document_type") != second.get("document_type"):
        disagree.append("document type")
    result["second_read"] = {"agrees": not disagree, "differs_on": disagree,
                             "values": {k: second["fields"].get(k) for k in KEY_NUMBERS if k in second["fields"]}}
    if disagree:
        result["readable"] = False
        result["confidence"] = min(float(result.get("confidence") or 0), 0.3)
        result["notes"] = ("Two independent readings of this document disagreed on: "
                           + ", ".join(d.replace("_", " ") for d in disagree)
                           + ". It is not clear enough to trust.")
    if cache is not None:
        cache.put(key, {**result, "_model": first.get("_model")})
    return done()


def judge_names(a: str, b: str) -> dict | None:
    """Second opinion for borderline name matches. Returns None if AI isn't available."""
    if not ai_available():
        return None
    try:
        return _call_gemini([NAME_PROMPT.format(a=a, b=b)])[0]
    except (RuntimeError, ValueError):
        return None
