"""Small, fast tests for the rules that were added or tightened after testing on the live app.

    python tests/test_rules.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATABASE_URL"] = f"sqlite:///{tempfile.mkdtemp()}/t.db"

import checks      # noqa: E402
import decide      # noqa: E402
import extract     # noqa: E402
import testcases   # noqa: E402

results = []


def check(name, condition):
    results.append(condition)
    print(("PASS  " if condition else "FAIL  ") + name)


def base():
    c = testcases.load_case("happy_path")
    return {k: dict(v) for k, v in testcases.case_payload(c).items()}


# --- C12: a company cannot use the trade-name box to make a personal account look right ------
p = base()
p["company"]["trade_name"] = "Rohan Verma Enterprises"
p["bank"]["account_holder"] = "ROHAN VERMA ENTERPRISES"
r = checks.c12_account_holder_name(p, {})
check("C12: company + account in a made-up trade name -> held for review", r.outcome == "fail" and r.severity == "review")

p = testcases.case_payload(testcases.load_case("E4_resubmission_v2"))
r = checks.c12_account_holder_name(p, {})
check("C12: genuine proprietor with account in the shop name -> passes", r.outcome == "pass")

# --- C15: two submissions still in progress share a bank account ------------------------------
p = base()
other = [{"request_ref": "REQ-0099", "legal_name": "Some Other Co", "pan": "AAACO1111A",
          "account_number": p["bank"]["account_number"], "ifsc": p["bank"]["ifsc"], "status": "Pending - waiting on vendor"}]
r = checks.c15_duplicate_vendor(p, {"open_submissions": other})
check("C15: same bank account on another open submission -> review", r.outcome == "fail" and r.severity == "review")
r = checks.c15_duplicate_vendor(p, {"open_submissions": []})
check("C15: nothing shared -> passes", r.outcome == "pass")

# --- Reading twice: disagreement means 'unreadable', agreement means trusted ------------------
reads = []


def fake_reads(part, skip_models=()):
    return reads.pop(0)


extract._settings_cache.update(key="test", model=None)
real = extract._one_read
extract._one_read = fake_reads
import google.genai.types as _t   # noqa: E402
_t.Part.from_bytes = staticmethod(lambda data, mime_type: "part")

A = {"document_type": "cancelled_cheque", "readable": True, "confidence": 0.9,
     "fields": {"account_number": "1234101998877", "ifsc": "CNRB0003310"}}
B = {"document_type": "cancelled_cheque", "readable": True, "confidence": 0.9,
     "fields": {"account_number": "1234351948877", "ifsc": "CNRB0003310"}}
reads[:] = [(dict(A), "m1"), (dict(B), "m2")]
out = extract.read_document(b"x", "image/jpeg")
check("double read: readings disagree -> not readable, confidence forced low",
      out["readable"] is False and out["confidence"] <= 0.3 and out["second_read"]["differs_on"] == ["account_number"])
reads[:] = [(dict(A), "m1"), (dict(A), "m2")]
out = extract.read_document(b"y", "image/jpeg")
check("double read: readings agree -> trusted", out["readable"] is True and out["second_read"]["agrees"] is True)
reads[:] = [(dict(A), "m1"), ({**A, "fields": {"account_number": "1234 1019 98877", "ifsc": "cnrb0003310"}}, "m2")]
out = extract.read_document(b"z", "image/jpeg")
check("double read: spaces and capitals don't count as disagreement", out["second_read"]["agrees"] is True)
extract._one_read = real

# --- Model failover: a failing model is skipped, not waited for --------------------------------
calls = []


class FakeModels:
    def generate_content(self, model, contents, config):
        calls.append(model)
        if model == extract.MODEL_CANDIDATES[0]:
            raise RuntimeError("503 UNAVAILABLE high demand")
        return type("R", (), {"text": '{"ok": true}'})()


extract._client = lambda: type("C", (), {"models": FakeModels()})()
extract._health.clear()
answer, used = extract._call_gemini(["hi"])
check("failover: first model overloaded -> second model answers, no retry of the first",
      answer == {"ok": True} and calls == extract.MODEL_CANDIDATES[:2])
check("failover: the failed model is tried last next time", extract._models_to_try()[-1] == extract.MODEL_CANDIDATES[0])

# --- What the vendor is shown never includes review reasons ------------------------------------
review = [{"outcome": "fail", "severity": "review", "code": "BANK_NAME_MISMATCH", "message": "fraud pattern",
           "vendor_message": None, "evidence": {}}]
v = decide.vendor_view(decide.PENDING_REVIEW, review, "REQ-1")
check("vendor view: internal review shows 'under review' and nothing else",
      v["fixes"] == [] and "fraud" not in (v["title"] + v["message"]).lower())
v = decide.vendor_view(decide.REJECTED, review, "REQ-1")
check("vendor view: rejection names no reason", "bank" not in v["message"].lower() and v["fixes"] == [])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
