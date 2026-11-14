"""
testcases.py - loads the prepared test vendors from test_cases/ so the app can pre-fill the form
("Load test case") and the terminal tests can run them. Only Indian cases are offered for now.
"""

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CASES_DIR = ROOT / "test_cases"

MIME = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def list_cases(india_only=True):
    out = []
    for p in sorted(CASES_DIR.glob("*.json")):
        if p.name == "document_truth.json":
            continue
        c = json.loads(p.read_text())
        if india_only and c["company"]["country"] != "IN":
            continue
        out.append(c)
    order = ["happy_path", "E1", "E2", "E4_resubmission_v1", "E4_resubmission_v2", "backup"]
    return sorted(out, key=lambda c: next((i for i, k in enumerate(order) if c["case_id"].startswith(k)), 99))


def load_case(case_id):
    return json.loads((CASES_DIR / f"{case_id}.json").read_text())


def case_payload(case):
    """The form fields only (no test metadata, no documents)."""
    return {k: case[k] for k in ("company", "contact", "tax", "bank")}


def case_documents(case):
    """[{slot, filename, mime_type, sha256, content}] read from disk."""
    docs = []
    for d in case["documents"]:
        path = CASES_DIR / d["file"]
        data = path.read_bytes()
        docs.append({"slot": d["slot"], "filename": path.name, "mime_type": MIME[path.suffix.lower()],
                     "sha256": hashlib.sha256(data).hexdigest(), "content": data})
    return docs


def document_truth():
    return json.loads((CASES_DIR / "document_truth.json").read_text())
