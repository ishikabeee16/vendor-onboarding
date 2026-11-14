"""
app.py - the screens. Run locally with:   streamlit run app.py

Three pages:
  1. New submission  - the vendor form + the live run view (each stage lights up as it runs)
  2. Dashboard       - every request, its status, reasons and history; filters
  3. Submission      - everything about one request: checks, documents, email, versions, overrides

The logic lives in checks.py / extract.py / decide.py / engine.py. This file only draws things.
"""

import hashlib
from datetime import datetime, timezone

import pandas as pd
import streamlit as st

import checks
import db
import decide
import engine
import extract
import testcases
import ui

st.set_page_config(page_title="Vendor Onboarding", page_icon="✅", layout="wide")
ui.inject_css()
db.engine()  # make sure the tables exist

RULES = checks.RULES
STATES = sorted(RULES["gst_state_codes"].values())
BUSINESS_TYPES = RULES["business_types"]

# Every form field: (session key / path in the submission, label, kind)
FORM = {
    "Company details": [
        ("company.legal_name", "Legal company name *", "text"),
        ("company.trade_name", "Trade / brand name (required for proprietors)", "text"),
        ("company.business_type", "Business type *", "business_type"),
        ("company.address", "Registered address *", "text"),
        ("company.city", "City *", "text"),
        ("company.state", "State *", "state"),
        ("company.pin", "PIN code *", "text"),
    ],
    "Contact person": [
        ("contact.name", "Contact name *", "text"),
        ("contact.email", "Contact email *", "text"),
        ("contact.phone", "Contact phone *", "text"),
    ],
    "Tax details": [
        ("tax.gst_registered", "Registered under GST", "bool"),
        ("tax.pan", "PAN *", "text"),
        ("tax.gstin", "GSTIN (if GST-registered)", "text"),
    ],
    "Bank details": [
        ("bank.account_holder", "Account holder name (as per bank) *", "text"),
        ("bank.account_number", "Account number *", "text"),
        ("bank.ifsc", "IFSC *", "text"),
        ("bank.bank_name", "Bank name *", "text"),
    ],
}
ALL_FIELDS = [f for section in FORM.values() for f in section]
STATUS_ICON = {decide.APPROVED: "🟢", decide.PENDING_VENDOR: "🟡", decide.PENDING_REVIEW: "🟠", decide.REJECTED: "🔴"}
DOC_SLOTS = ["gst_certificate", "pan_card", "bank_proof", "gst_declaration"]


def key(path):
    return f"f::{path}"


def init_form_state():
    for path, _, kind in ALL_FIELDS:
        st.session_state.setdefault(key(path), {"bool": True, "state": "Karnataka",
                                                "business_type": "private_ltd"}.get(kind, ""))
    st.session_state.setdefault("loaded_docs", {})   # slot -> doc dict (from a test case or earlier version)
    st.session_state.setdefault("parent_id", None)
    st.session_state.setdefault("case_id", None)


def fill_form(payload, docs, case_id=None, parent_id=None):
    for path, _, _ in ALL_FIELDS:
        section, field = path.split(".")
        val = payload.get(section, {}).get(field, "")
        st.session_state[key(path)] = val if val is not None else ""
    st.session_state["loaded_docs"] = {d["slot"]: d for d in docs}
    st.session_state["case_id"] = case_id
    st.session_state["parent_id"] = parent_id


def clear_form():
    for path, _, kind in ALL_FIELDS:
        st.session_state[key(path)] = {"bool": True, "state": "Karnataka", "business_type": "private_ltd"}.get(kind, "")
    st.session_state["loaded_docs"], st.session_state["case_id"], st.session_state["parent_id"] = {}, None, None


def payload_from_form():
    p = {"company": {"country": "IN"}, "contact": {}, "tax": {}, "bank": {}}
    for path, _, kind in ALL_FIELDS:
        section, field = path.split(".")
        v = st.session_state[key(path)]
        p[section][field] = v.strip() if isinstance(v, str) else v
    return p


# ---------------------------------------------------------------------------
# Page 1: New submission + live run
# ---------------------------------------------------------------------------
def page_new():
    init_form_state()

    # After "Run" is clicked, the form is replaced by the live run view (full screen, no scrolling).
    if st.session_state.get("run_now"):
        sid = st.session_state.pop("run_now")
        st.session_state["show_sid"] = sid
        back_button()
        live_run(sid)
        return
    if st.session_state.get("show_sid"):
        s = db.get_submission(st.session_state["show_sid"])
        back_button()
        st.markdown(f"### {s['request_ref']} v{s['version']} · {ui.esc(s['vendor_name'])}")
        show_run_result(s, s["runs"][-1])
        return

    st.title("New vendor submission")
    st.caption("A vendor fills this in and uploads their documents. The process checks everything, "
               "decides Approved / Pending / Rejected, explains why, and drafts the email back to the vendor.")

    # --- Load a prepared test case (so the demo needs no typing) -----------
    cases = testcases.list_cases()
    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="bottom")
        labels = {c["case_id"]: c["label"] for c in cases}
        chosen = c1.selectbox("Start from a prepared test case (optional)", list(labels),
                              format_func=lambda k: labels[k], index=None, placeholder="Choose a test vendor…")
        if c2.button("Load", width="stretch", disabled=not chosen):
            case = testcases.load_case(chosen)
            parent = None
            if case.get("parent_case"):
                parent = db.latest_submission_for_case(case["parent_case"])
                if not parent:
                    st.warning(f"This is a resubmission of '{case['parent_case']}'. Run that one first, "
                               f"then load this, so they're linked as v1 → v2.")
            fill_form(testcases.case_payload(case), testcases.case_documents(case), chosen, parent)
            st.rerun()
        if c3.button("Clear form", width="stretch"):
            clear_form()
            st.rerun()

    if st.session_state["parent_id"]:
        parent = db.get_submission(st.session_state["parent_id"])
        st.info(f"**Resubmission** of {parent['request_ref']} (v{parent['version']}, {parent['vendor_name']}). "
                f"This will be saved as **v{parent['version'] + 1}** of the same request, and every check re-runs.")

    # --- The form ------------------------------------------------------------
    with st.form("vendor_form"):
        left, right = st.columns(2, gap="large")
        for i, (section, fields) in enumerate(FORM.items()):
            col = left if i % 2 == 0 else right
            with col:
                st.subheader(section)
                for path, label, kind in fields:
                    k = key(path)
                    if kind == "text":
                        st.text_input(label, key=k)
                    elif kind == "bool":
                        st.checkbox(label, key=k, help="Small businesses below the GST turnover limit "
                                                       "can legally be unregistered.")
                    elif kind == "state":
                        st.selectbox(label, STATES, key=k)
                    elif kind == "business_type":
                        st.selectbox(label, list(BUSINESS_TYPES), key=k, format_func=BUSINESS_TYPES.get)

        st.subheader("Documents")
        st.caption("PDF, JPG or PNG. Upload the documents you already have; no special template needed.")
        uploads = {}
        dcols = st.columns(4)
        for col, slot in zip(dcols, DOC_SLOTS):
            with col:
                label = RULES["document_slots"][slot]["label"]
                if slot == "gst_declaration":
                    label += " (only if not GST-registered)"
                loaded = st.session_state["loaded_docs"].get(slot)
                st.markdown(f"**{label}**")
                if loaded:
                    st.caption(f"📎 {loaded['filename']}")
                uploads[slot] = st.file_uploader("Replace" if loaded else "Upload", type=["pdf", "jpg", "jpeg", "png"],
                                                 key=f"up::{slot}", label_visibility="visible")
        submitted = st.form_submit_button("▶  Run onboarding check", type="primary", width="stretch")

    if submitted:
        payload = payload_from_form()
        docs = []
        for slot in DOC_SLOTS:
            up = uploads.get(slot)
            if up is not None:
                data = up.getvalue()
                docs.append({"slot": slot, "filename": up.name, "mime_type": up.type or "application/pdf",
                             "sha256": hashlib.sha256(data).hexdigest(), "content": data})
            elif slot in st.session_state["loaded_docs"]:
                d = st.session_state["loaded_docs"][slot]
                docs.append({k: d[k] for k in ("slot", "filename", "mime_type", "sha256", "content")})
        source = "test_case" if st.session_state["case_id"] else "form"
        sid = db.save_submission(payload, docs, source=source, case_id=st.session_state["case_id"],
                                 parent_id=st.session_state["parent_id"])
        st.session_state["run_now"] = sid
        st.rerun()


def back_button():
    if st.button("← New submission"):
        st.session_state.pop("show_sid", None)
        clear_form()
        st.rerun()


def live_run(sid):
    """Draws the 5 stages, then fills them in as the engine reports each step."""
    sub = db.get_submission(sid)
    st.markdown(f"### Live run · {sub['request_ref']} v{sub['version']} · {ui.esc(sub['vendor_name'])}")
    progress = st.progress(0.0, text="Starting…")
    stage_boxes = {}
    for no, name in checks.STAGES.items():
        box = st.container(border=True)
        stage_boxes[no] = {"header": box.empty(), "body": box.container(), "name": name}
        stage_boxes[no]["header"].markdown(ui.stage_header_html(no, name, "waiting"), unsafe_allow_html=True)
    result_area = st.container()
    spinner_slot = {"ph": None}

    def emit(event, *args):
        if event == "stage_start":
            no, name = args
            stage_boxes[no]["header"].markdown(ui.stage_header_html(no, name, "running"), unsafe_allow_html=True)
            progress.progress((no - 1) / 5, text=f"Stage {no} of 5: {name}")
        elif event == "check":
            r = args[0]
            stage_boxes[r["stage"]]["body"].markdown(ui.check_html(r), unsafe_allow_html=True)
        elif event == "doc_read_start":
            slot, label, filename = args
            spinner_slot["ph"] = stage_boxes[4]["body"].empty()
            spinner_slot["ph"].markdown(f'<div class="vo-ai">⏳ Gemini is reading the <b>{ui.esc(label)}</b> '
                                        f'({ui.esc(filename)})…</div>', unsafe_allow_html=True)
        elif event == "doc_read_done":
            slot, label, ex = args
            spinner_slot["ph"].markdown(ui.ai_box_html(label, ex), unsafe_allow_html=True)
        elif event == "stage_end":
            no, worst = args
            stage_boxes[no]["header"].markdown(ui.stage_header_html(no, stage_boxes[no]["name"], worst),
                                               unsafe_allow_html=True)
            progress.progress(no / 5, text=f"Stage {no} of 5 done")
        elif event == "done":
            progress.progress(1.0, text="Done")

    engine.run_pipeline(sid, emit=emit)
    sub = db.get_submission(sid)
    with result_area:
        st.markdown("### Decision")
        show_run_result(sub, sub["runs"][-1], show_checks=False)


def show_run_result(sub, run, show_checks=True):
    status = db.final_status(run)
    extra = None
    if run.get("overrides"):
        o = run["overrides"][-1]
        extra = (f"Changed by {o['reviewer']} on {ui.ist(o['created_at'])} (process said: {run['status']}). "
                 f"Reason: {o['reason']}")
    if run.get("vendor_id") and status == decide.APPROVED:
        extra = (extra + " · " if extra else "") + f"Vendor ID: {run['vendor_id']}"
    ui.status_card(status, run["summary"], run["reason_codes"], extra)
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        if show_checks:
            ui.render_checks_by_stage(run["checks"], run["extractions"])
        else:
            st.markdown("**Email to the vendor**")
            ui.email_box(run["email"])
    with c2:
        if show_checks:
            st.markdown("**Email to the vendor**")
            ui.email_box(run["email"])
        if st.button("Open full details →", key=f"open_{run['id']}", width="stretch"):
            st.session_state["detail_sid"] = sub["id"]
            st.switch_page(PAGES["detail"])


# ---------------------------------------------------------------------------
# Page 2: Dashboard
# ---------------------------------------------------------------------------
def page_dashboard():
    st.title("Dashboard")
    runs = db.list_runs()
    if not runs:
        st.info("No submissions yet. Go to **New submission** and run one.")
        return

    # Current state of each request = its latest run (latest version)
    latest = {}
    for r in runs:  # newest first
        latest.setdefault(r["request_ref"], r)
    current = list(latest.values())

    counts = {s: sum(r["final_status"] == s for r in current) for s in decide.ALL_STATUSES}
    secs = [r["seconds"] for r in runs if r["seconds"] is not None]
    m = st.columns(6)
    m[0].metric("Requests", len(current))
    m[1].metric("Approved", counts[decide.APPROVED])
    m[2].metric("Waiting on vendor", counts[decide.PENDING_VENDOR])
    m[3].metric("Internal review", counts[decide.PENDING_REVIEW])
    m[4].metric("Rejected", counts[decide.REJECTED])
    m[5].metric("Avg time to decision", f"{sum(secs) / len(secs):.0f}s" if secs else "–")

    f1, f2, f3, f4 = st.columns([2, 2, 2, 1], vertical_alignment="bottom")
    status_f = f1.multiselect("Status", decide.ALL_STATUSES, placeholder="All statuses")
    all_codes = sorted({c for r in runs for c in r["reason_codes"]})
    code_f = f2.multiselect("Reason code", all_codes, placeholder="All reasons")
    search = f3.text_input("Search vendor or reference")
    show_all = f4.toggle("Every run", help="Show every run, including earlier versions and re-runs")

    rows = runs if show_all else current
    now = datetime.now(timezone.utc)
    table = []
    for r in rows:
        if status_f and r["final_status"] not in status_f:
            continue
        if code_f and not set(code_f) & set(r["reason_codes"]):
            continue
        if search and search.lower() not in f"{r['vendor_name']} {r['request_ref']}".lower():
            continue
        started = r["started_at"] if r["started_at"].tzinfo else r["started_at"].replace(tzinfo=timezone.utc)
        waiting = (now - started).days if r["final_status"] == decide.PENDING_VENDOR else None
        table.append({
            "sid": r["submission_id"], "Ref": r["request_ref"], "Vendor": r["vendor_name"],
            "Version": f"v{r['version']}",
            "Status": STATUS_ICON[r["final_status"]] + " " + r["final_status"] + (" (overridden)" if r["overridden"] else ""),
            "Reasons": ", ".join(r["reason_codes"]) or "—", "Submitted": ui.ist(r["started_at"]),
            "Days waiting": f"{waiting}d" + (" ⚠️" if waiting and waiting > 3 else "") if waiting is not None else "",
            "Source": "Test case" if r["source"] == "test_case" else "Form",
        })
    if not table:
        st.warning("Nothing matches these filters.")
        return
    df = pd.DataFrame(table)
    st.caption("Click a row to open it.")
    event = st.dataframe(df.drop(columns=["sid"]), width="stretch", hide_index=True,
                         on_select="rerun", selection_mode="single-row",
                         column_config={"Reasons": st.column_config.TextColumn(width="large")})
    if event.selection.rows:
        st.session_state["detail_sid"] = int(df.iloc[event.selection.rows[0]]["sid"])
        st.switch_page(PAGES["detail"])


# ---------------------------------------------------------------------------
# Page 3: Submission detail
# ---------------------------------------------------------------------------
def page_detail():
    runs = db.list_runs()
    if not runs:
        st.title("Submission details")
        st.info("No submissions yet.")
        return
    options, seen = {}, set()
    for r in runs:
        if r["submission_id"] not in seen:
            seen.add(r["submission_id"])
            options[r["submission_id"]] = f"{r['request_ref']} v{r['version']} — {r['vendor_name']} ({r['final_status']})"
    default = st.session_state.get("detail_sid")
    ids = list(options)
    sid = st.selectbox("Submission", ids, index=ids.index(default) if default in ids else 0,
                       format_func=options.get)
    st.session_state["detail_sid"] = sid
    sub = db.get_submission(sid)
    run = sub["runs"][-1]
    p = sub["payload"]

    st.title(f"{sub['vendor_name']}")
    st.markdown(f"{sub['request_ref']} · version {sub['version']} · submitted {ui.ist(sub['created_at'])} · "
                f"{BUSINESS_TYPES.get(p['company'].get('business_type'), '')} · {p['company'].get('state', '')}")
    show_status = db.final_status(run)
    extra = None
    if run.get("overrides"):
        o = run["overrides"][-1]
        extra = f"Changed by {o['reviewer']} on {ui.ist(o['created_at'])} (process said: {run['status']}). Reason: {o['reason']}"
    if run.get("vendor_id") and show_status == decide.APPROVED:
        extra = (extra + " · " if extra else "") + f"Vendor ID: {run['vendor_id']}"
    ui.status_card(show_status, run["summary"], run["reason_codes"], extra)

    tabs = st.tabs(["Checks", "Documents", "Email to vendor", "Submitted details", "History & audit trail"])
    with tabs[0]:
        ui.render_checks_by_stage(run["checks"], run["extractions"])
    with tabs[1]:
        for d in sub["documents"]:
            label = RULES["document_slots"].get(d["slot"], {"label": d["slot"]})["label"]
            ex = (run["extractions"] or {}).get(d["slot"], {})
            with st.container(border=True):
                c1, c2 = st.columns([1, 1], gap="large")
                c1.markdown(f"**{label}** · {ui.esc(d['filename'])}")
                c1.image(ui.preview_image(d["content"], d["mime_type"]), width="stretch")
                with c2:
                    st.markdown(ui.ai_box_html(label, ex), unsafe_allow_html=True)
                    st.download_button("Download file", d["content"], file_name=d["filename"],
                                       mime=d["mime_type"], key=f"dl_{d['id']}")
    with tabs[2]:
        ui.email_box(run["email"])
    with tabs[3]:
        for section in ("company", "contact", "tax", "bank"):
            st.markdown(f"**{section.title()}**")
            st.table(pd.DataFrame([(k.replace("_", " "), str(v)) for k, v in p.get(section, {}).items()],
                                  columns=["Field", "Value"]))
    with tabs[4]:
        st.markdown("**Versions of this request**")
        for v in sub["versions"]:
            vs = db.get_submission(v["id"])
            status = db.final_status(vs["runs"][-1]) if vs["runs"] else "not run"
            st.markdown(f"- v{v['version']} · {ui.ist(v['created_at'])} · {ui.status_badge(status)}"
                        f"{' ← this one' if v['id'] == sid else ''}", unsafe_allow_html=True)
        st.markdown("**Runs and decisions**")
        for r in sub["runs"]:
            st.markdown(f"- Run #{r['id']} · {ui.ist(r['started_at'])} · took {r['seconds']}s · "
                        f"process decided {ui.status_badge(r['status'])}", unsafe_allow_html=True)
            for o in r["overrides"]:
                st.markdown(f"  - ✍️ **{ui.esc(o['reviewer'])}** changed it to {ui.status_badge(o['new_status'])} "
                            f"on {ui.ist(o['created_at'])}: “{ui.esc(o['reason'])}”", unsafe_allow_html=True)

    st.divider()
    a1, a2 = st.columns(2, gap="large")
    with a1:
        st.markdown("#### Reviewer decision")
        st.caption("Override the process's decision. A reason is required and is logged with your name.")
        with st.form(f"override_{run['id']}", clear_on_submit=True):
            new_status = st.radio("New status", [decide.APPROVED, decide.REJECTED, decide.PENDING_VENDOR],
                                  horizontal=True)
            reviewer = st.text_input("Your name")
            reason = st.text_area("Reason", placeholder="e.g. Called the vendor on their registered number; "
                                                        "the account belongs to the proprietor.")
            if st.form_submit_button("Save decision", type="primary"):
                if not reviewer.strip() or len(reason.strip()) < 10:
                    st.error("Please give your name and a reason (at least 10 characters).")
                else:
                    db.save_override(run["id"], new_status, reason.strip(), reviewer.strip())
                    st.success("Saved to the audit trail.")
                    st.rerun()
    with a2:
        st.markdown("#### Vendor sent corrections?")
        st.caption("Opens the form pre-filled with this submission. Change what the vendor fixed; "
                   "it's saved as the next version and every check runs again.")
        if st.button("Resubmit as a new version", width="stretch"):
            fill_form(p, sub["documents"], case_id=None, parent_id=sid)
            st.session_state.pop("show_sid", None)
            st.switch_page(PAGES["new"])
        if st.button("Re-run all checks on this version", width="stretch"):
            st.session_state["run_now"] = sid
            st.switch_page(PAGES["new"])


# ---------------------------------------------------------------------------
# Sidebar + navigation
# ---------------------------------------------------------------------------
PAGES = {
    "new": st.Page(page_new, title="New submission", icon="📝", default=True),
    "dashboard": st.Page(page_dashboard, title="Dashboard", icon="📊", url_path="dashboard"),
    "detail": st.Page(page_detail, title="Submission details", icon="🔎", url_path="details"),
}

with st.sidebar:
    st.markdown("### Vendor Onboarding")
    st.caption("Acme Retail Pvt Ltd · procurement")
nav = st.navigation(list(PAGES.values()))
with st.sidebar:
    st.divider()
    if extract.ai_available():
        st.success(f"AI document reading: on ({extract.model_name()})", icon="🤖")
    else:
        st.warning("AI document reading: no API key set. Document checks will hold runs for review.", icon="⚠️")
    st.caption("Storage: hosted database (permanent)" if db.using_hosted_db()
               else "Storage: local file on this machine")
    st.caption("All vendors and documents in the test cases are fictional.")

nav.run()
