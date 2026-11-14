"""
Run every Indian test case through the full pipeline and compare with its expected result.

    python tests/run_cases.py            # uses the answer key instead of the AI (works offline)
    python tests/run_cases.py --real-ai  # uses Gemini for real (needs GEMINI_API_KEY)

The answer-key reader exists ONLY here, for testing the rules without the AI. The app always uses
the real AI. With --real-ai, the same test also shows how accurately the AI read each document.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DATABASE_URL", f"sqlite:///{tempfile.mkdtemp()}/test.db")  # fresh DB every run

import db       # noqa: E402
import engine   # noqa: E402
import extract  # noqa: E402
import testcases  # noqa: E402

TRUTH = testcases.document_truth()
REAL_AI = "--real-ai" in sys.argv


def answer_key_reader(data, mime_type, cache=None):
    """Pretend-AI that returns exactly what is printed on our fake documents."""
    for case in testcases.list_cases(india_only=False):
        for d in testcases.case_documents(case):
            if d["content"] == data:
                t = TRUTH[d["filename"]]
                return {**t, "confidence": 0.95 if t["readable"] else 0.3,
                        "fields": {k: v for k, v in t["fields"].items() if v}}
    return {"error": "unknown file"}


def main():
    reader = extract.read_document if REAL_AI else answer_key_reader
    passed = 0
    cases = testcases.list_cases()
    for case in cases:
        parent = db.latest_submission_for_case(case["parent_case"]) if case.get("parent_case") else None
        sid = db.save_submission(testcases.case_payload(case), testcases.case_documents(case),
                                 source="test_case", case_id=case["case_id"], parent_id=parent)
        run_id = engine.run_pipeline(sid, reader=reader, name_judge=None if not REAL_AI else extract.judge_names,
                                     pause=0)
        run = db.get_submission(sid)["runs"][-1]
        failing = sorted({r["id"] for r in run["checks"] if r["outcome"] == "fail"}, key=lambda x: int(x[1:]))
        exp = case["expected"]
        good = run["status"] == exp["status"] and failing == sorted(exp["failing_checks"], key=lambda x: int(x[1:]))
        passed += good
        print(f"{'PASS' if good else 'FAIL'}  {case['case_id']:<26} got: {run['status']:<28} failing={failing}")
        if not good:
            print(f"      expected: {exp['status']:<28} failing={exp['failing_checks']}")
            for r in run["checks"]:
                if r["outcome"] == "fail":
                    print(f"        {r['id']} [{r['severity']}] {r['message']}")
        if REAL_AI:
            for d in testcases.case_documents(case):
                ex = run["extractions"].get(d["slot"], {})
                t = TRUTH.get(d["filename"], {})
                wrong = [k for k, v in t.get("fields", {}).items()
                         if v and str(ex.get("fields", {}).get(k, "")).replace(" ", "").upper() != str(v).replace(" ", "").upper()]
                print(f"        AI read {d['filename']}: type={ex.get('document_type')} "
                      f"conf={ex.get('confidence')} {'all fields correct' if not wrong else 'WRONG: ' + ', '.join(wrong)}")
    print(f"\n{passed}/{len(cases)} cases behave as expected")
    return passed == len(cases)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
