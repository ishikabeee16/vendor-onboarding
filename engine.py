"""
engine.py - runs one submission through all 5 stages, in order, and reports each step as it happens.

The screen (app.py) passes in an `emit` function. Every time something happens (a stage starts,
a check finishes, the AI finishes reading a document) the engine calls emit(...) and the screen
draws it. That's how the live run view shows each step while it runs.

The same function also runs from the terminal (tests/run_cases.py) with emit=print-like.
"""

import time
from concurrent.futures import ThreadPoolExecutor, as_completed

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
    """Run every stage for one submission and save the result. Returns the run id.

    The run is written to the database first, marked "Running", and updated at the end. If
    anything goes wrong in the middle, it is recorded as "Did not finish" with the reason, so a
    submission can never silently vanish.
    """
    run_id = db.start_run(submission_id)
    started = db.now()
    try:
        return _run(run_id, started, submission_id, emit, reader, name_judge, pause)
    except Exception as e:                      # record the failure, then let the screen show it
        db.finish_run(run_id, started, db.DID_NOT_FINISH,
                      f"The checks stopped before finishing ({type(e).__name__}: {str(e)[:200]}). "
                      f"Nothing was decided. Re-run the checks.", ["RUN_FAILED"], [], {}, None, None)
        raise


def _run(run_id, started, submission_id, emit, reader, name_judge, pause):
    reader = reader or extract.read_document
    name_judge = name_judge if name_judge is not None else extract.judge_names
    sub = db.get_submission(submission_id)
    payload = dict(sub["payload"])
    payload["documents"] = [{"slot": d["slot"], "file": d["filename"]} for d in sub["documents"]]
    is_real = sub["source"] == "form"           # demo test cases don't count as real vendors

    ctx = {
        "name_judge": name_judge,
        # vendors approved earlier through this app, except earlier versions of this same request
        "approved_vendors": [v for v in db.approved_vendors() if v["request_ref"] != sub["request_ref"]],
        # other real submissions still in progress (so two pending ones can notice each other)
        "open_submissions": db.open_submissions(exclude_ref=sub["request_ref"]) if is_real else [],
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

    # Stage 4: the AI reads each document (all documents at the same time, to save waiting),
    # then code compares what it read with the form.
    emit("stage_start", 4, checks.STAGES[4])
    cache = db.Cache()
    docs = sub["documents"]
    labels = {d["slot"]: checks.RULES["document_slots"].get(d["slot"], {"label": d["slot"]})["label"] for d in docs}
    for d in docs:
        emit("doc_read_start", d["slot"], labels[d["slot"]], d["filename"])
    if reader is extract.read_document:
        extract.ai_available()                  # load settings on this thread before the workers start
    with ThreadPoolExecutor(max_workers=max(1, len(docs))) as pool:
        futures = {pool.submit(reader, d["content"], d["mime_type"], cache=cache): d["slot"] for d in docs}
        for fut in as_completed(futures):
            slot = futures[fut]
            try:
                ex = fut.result()
            except Exception as e:              # one document failing must not sink the others
                ex = {"error": f"{type(e).__name__}: {str(e)[:150]}"}
            ctx["extractions"][slot] = ex
            emit("doc_read_done", slot, labels[slot], ex)
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
    extractions = {d["slot"]: ctx["extractions"].get(d["slot"], {}) for d in docs}   # keep upload order
    db.finish_run(run_id, started, status, summ, codes, all_results, extractions, email, vendor_id)
    emit("done", run_id, status, summ, email)
    return run_id
