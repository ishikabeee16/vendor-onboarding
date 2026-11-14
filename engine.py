"""
engine.py - runs one submission through all 5 stages, in order, and reports each step as it happens.

The screen (app.py) passes in an `emit` function. Every time something happens (a stage starts,
a check finishes, the AI finishes reading a document) the engine calls emit(...) and the screen
draws it. That's how the live run view shows each step while it runs.

The same function also runs from the terminal (tests/run_cases.py) with emit=print-like.
"""

import time

import checks
import db
import decide
import extract

# A short pause between checks so a human can follow along on screen. The rule checks themselves
# take milliseconds; this is purely for readability in the live view. Set to 0 for tests.
STEP_PAUSE = 0.12


def _worst(results):
    fails = [r for r in results if r["outcome"] == "fail"]
    for sev in ("blocker", "review", "fixable"):
        if any(r["severity"] == sev for r in fails):
            return sev
    return "pass"


def run_pipeline(submission_id, emit=lambda *a: None, reader=None, name_judge=None, pause=STEP_PAUSE):
    reader = reader or extract.read_document
    name_judge = name_judge if name_judge is not None else extract.judge_names
    started = db.now()
    sub = db.get_submission(submission_id)
    payload = dict(sub["payload"])
    payload["documents"] = [{"slot": d["slot"], "file": d["filename"]} for d in sub["documents"]]

    ctx = {
        "name_judge": name_judge,
        # vendors approved earlier through this app, except earlier versions of this same request
        "approved_vendors": [v for v in db.approved_vendors() if v["request_ref"] != sub["request_ref"]],
        "extractions": {},
    }
    all_results = []

    def run_stage(stage_no, funcs):
        emit("stage_start", stage_no, checks.STAGES[stage_no])
        stage_results = []
        for fn in funcs:
            out = fn(payload, ctx)
            for r in (out if isinstance(out, list) else [out]):
                d = r.to_dict()
                stage_results.append(d)
                emit("check", d)
                time.sleep(pause)
        emit("stage_end", stage_no, _worst(stage_results))
        all_results.extend(stage_results)

    # Stages 1-3: completeness, format, consistency (pure rules)
    for stage_no in (1, 2, 3):
        run_stage(stage_no, checks.RULE_STAGES[stage_no])

    # Stage 4: the AI reads each document, then code compares
    emit("stage_start", 4, checks.STAGES[4])
    cache = db.Cache()
    for doc in sub["documents"]:
        label = checks.RULES["document_slots"].get(doc["slot"], {"label": doc["slot"]})["label"]
        emit("doc_read_start", doc["slot"], label, doc["filename"])
        ex = reader(doc["content"], doc["mime_type"], cache=cache)
        ctx["extractions"][doc["slot"]] = ex
        emit("doc_read_done", doc["slot"], label, ex)
    stage4 = []
    for fn in checks.DOC_CHECKS:
        for r in fn(payload, ctx):
            d = r.to_dict()
            stage4.append(d)
            emit("check", d)
            time.sleep(pause)
    emit("stage_end", 4, _worst(stage4))
    all_results.extend(stage4)

    # Stage 5: risk
    run_stage(5, checks.RISK_CHECKS)

    # Decision
    status = decide.decide(all_results)
    vendor_id = db.next_vendor_id() if status == decide.APPROVED else None
    name = payload["company"].get("legal_name", "This vendor")
    summ = decide.summary(status, all_results, name)
    email = decide.vendor_email(status, all_results, payload, sub["request_ref"], vendor_id)
    codes = decide.reason_codes(all_results)
    run_id = db.save_run(submission_id, started, status, summ, codes, all_results,
                         ctx["extractions"], email, vendor_id)
    emit("done", run_id, status, summ, email)
    return run_id
