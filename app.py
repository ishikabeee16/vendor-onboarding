"""
app.py - the screens. Run locally with:   streamlit run app.py

There are TWO kinds of user, and they see different things:

  Vendor (outside the company)
    - Register as a vendor     the form + document upload; afterwards a plain result
    - Track your submission    needs the reference + a private access code

  Procurement team (inside the company)
    - Dashboard                work queue, all requests, insights
    - New submission           enter a vendor / load a test vendor, and watch the checks run live
    - Submission details       every check, documents, email, history, reviewer decision

The switch between the two is in the sidebar. It is a DEMO switch: in a real rollout these are
two separate sites and the team side sits behind a company login.

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
TEAM, VENDOR = "Procurement team", "Vendor"

# Every form field: (path in the submission, label, kind)
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
DOC_SLOTS = ["gst_certificate", "pan_card", "bank_proof", "gst_declaration"]
DEFAULTS = {"bool": True, "state": "Karnataka", "business_type": "private_ltd"}
STATUS_ICON = {decide.APPROVED: "🟢", decide.PENDING_VENDOR: "🟡", decide.PENDING_REVIEW: "🟠",
               decide.REJECTED: "🔴", db.RUNNING: "🔵", db.DID_NOT_FINISH: "⚪"}
ALL_STATUSES = decide.ALL_STATUSES + [db.DID_NOT_FINISH]


# ---------------------------------------------------------------------------
# The form (shared by the vendor page and the team page)
# ---------------------------------------------------------------------------
def key(path):
    return f"f::{path}"


def init_form_state():
    for path, _, kind in ALL_FIELDS:
        st.session_state.setdefault(key(path), DEFAULTS.get(kind, ""))
    st.session_state.setdefault("loaded_docs", {})   # slot -> document (from a test case or earlier version)
    st.session_state.setdefault("parent_id", None)
    st.session_state.setdefault("case_id", None)


def fill_form(payload, docs, case_id=None, parent_id=None):
    for path, _, kind in ALL_FIELDS:
        section, field = path.split(".")
        val = payload.get(section, {}).get(field, DEFAULTS.get(kind, ""))
        st.session_state[key(path)] = val if val is not None else ""
    st.session_state["loaded_docs"] = {d["slot"]: d for d in docs}
    st.session_state["case_id"] = case_id
    st.session_state["parent_id"] = parent_id


def clear_form():
    for path, _, kind in ALL_FIELDS:
        st.session_state[key(path)] = DEFAULTS.get(kind, "")
    st.session_state["loaded_docs"], st.session_state["case_id"], st.session_state["parent_id"] = {}, None, None


def payload_from_form():
    p = {"company": {"country": "IN"}, "contact": {}, "tax": {}, "bank": {}}
    for path, _, _ in ALL_FIELDS:
        section, field = path.split(".")
        v = st.session_state[key(path)]
        p[section][field] = v.strip() if isinstance(v, str) else v
    return p


def render_form(button_label):
    """Draws the form. Returns the new submission id when the button is pressed, else None."""
    with st.form("vendor_form"):
        left, right = st.columns(2, gap="large")
        for i, (section, fields) in enumerate(FORM.items()):
            with (left if i % 2 == 0 else right):
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
        st.caption("PDF, JPG or PNG, up to 10 MB each. Upload the documents you already have; "
                   "no special template is needed.")
        uploads = {}
        for col, slot in zip(st.columns(4), DOC_SLOTS):
            with col:
                label = RULES["document_slots"][slot]["label"]
                if slot == "gst_declaration":
                    label += " (only if not GST-registered)"
                loaded = st.session_state["loaded_docs"].get(slot)
                st.markdown(f"**{label}**")
                if loaded:
                    st.caption(f"📎 {loaded['filename']}")
                uploads[slot] = st.file_uploader("Replace" if loaded else "Upload", type=["pdf", "jpg", "jpeg", "png"],
                                                 key=f"up::{slot}")
        submitted = st.form_submit_button(button_label, type="primary", width="stretch")

    if not submitted:
        return None
    docs = []
    for slot in DOC_SLOTS:
        up = uploads.get(slot)
        if up is not None:
            data = up.getvalue()
            mime = up.type or testcases.MIME.get("." + up.name.rsplit(".", 1)[-1].lower(), "application/pdf")
            docs.append({"slot": slot, "filename": up.name, "mime_type": mime,
                         "sha256": hashlib.sha256(data).hexdigest(), "content": data})
        elif slot in st.session_state["loaded_docs"]:
            d = st.session_state["loaded_docs"][slot]
            docs.append({"slot": d["slot"], "filename": d["filename"], "mime_type": d["mime_type"],
                         "sha256": d["sha256"], "content": bytes(d["content"])})
    source = "test_case" if st.session_state["case_id"] else "form"
    return db.save_submission(payload_from_form(), docs, source=source, case_id=st.session_state["case_id"],
                              parent_id=st.session_state["parent_id"])


def resubmission_banner():
    if st.session_state["parent_id"]:
        parent = db.get_submission(st.session_state["parent_id"])
        st.info(f"**Resubmission** of {parent['request_ref']} (version {parent['version']}, "
                f"{parent['vendor_name']}). This will be saved as **version {parent['version'] + 1}** of the "
                f"same request, and every check runs again.")


def run_safely(sid, emit):
    """Run the checks; if something breaks, say so plainly (the failure is already saved)."""
    try:
        engine.run_pipeline(sid, emit=emit)
        return True
    except Exception as e:
        st.error(f"The checks stopped before finishing: {type(e).__name__}: {str(e)[:200]}. "
                 f"Nothing was decided. The submission is saved; please run the checks again.")
        return False


# ---------------------------------------------------------------------------
# VENDOR pages
# ---------------------------------------------------------------------------
def vendor_result(sub):
    run = sub["runs"][-1]
    view = decide.vendor_view(db.final_status(run), run["checks"] or [], sub["request_ref"], run.get("vendor_id"))
    ui.vendor_card(view, sub["request_ref"], db.access_code(sub["request_ref"]))
    return view


def page_vendor_submit():
    init_form_state()
    if st.session_state.get("v_run_now"):
        sid = st.session_state.pop("v_run_now")
        st.session_state["v_show_sid"] = sid
        st.title("Checking your details")
        progress = st.progress(0.0, text="Starting…")

        def emit(event, *args):                 # the vendor only sees progress, never the checks
            if event == "stage_start":
                progress.progress((args[0] - 1) / 5, text=f"Step {args[0]} of 5…")
            elif event == "done":
                progress.progress(1.0, text="Done")

        run_safely(sid, emit)
        st.rerun()
    if st.session_state.get("v_show_sid"):
        sub = db.get_submission(st.session_state["v_show_sid"])
        st.title("Thank you")
        view = vendor_result(sub)
        c1, c2 = st.columns(2)
        if view["tone"] == "action" and c1.button("Fix these and resubmit", type="primary", width="stretch"):
            fill_form(sub["payload"], sub["documents"], parent_id=sub["id"])
            st.session_state.pop("v_show_sid")
            st.rerun()
        if c2.button("Start a new registration", width="stretch"):
            st.session_state.pop("v_show_sid")
            clear_form()
            st.rerun()
        return

    st.title("Register as a vendor")
    st.caption(f"Fill in your details and upload your documents. You'll get a result in under a minute. "
               f"Questions: {decide.PROCUREMENT_EMAIL}")
    resubmission_banner()
    sid = render_form("Submit registration")
    if sid:
        st.session_state["v_run_now"] = sid
        st.rerun()


def page_vendor_track():
    st.title("Track your submission")
    st.caption("Enter the reference and access code you were given when you submitted.")
    with st.form("track"):
        c1, c2 = st.columns(2)
        ref = c1.text_input("Reference", placeholder="REQ-0007")
        code = c2.text_input("Access code", placeholder="AB12-CD34")
        go = st.form_submit_button("Show my status", type="primary")
    if go:
        sid = db.find_by_ref(ref) if ref.strip() else None
        # Same message whether the reference or the code is wrong, so nobody can test which references exist.
        if not sid or code.strip().upper() != db.access_code(ref):
            st.session_state.pop("track_sid", None)
            st.error("That reference and access code don't match. Please check both and try again.")
        else:
            st.session_state["track_sid"] = sid
    if st.session_state.get("track_sid"):
        sub = db.get_submission(st.session_state["track_sid"])
        st.markdown(f"#### {ui.esc(sub['vendor_name'])} · version {sub['version']} · "
                    f"submitted {ui.ist(sub['created_at'])}")
        view = vendor_result(sub)
        if view["tone"] == "action" and st.button("Fix these and resubmit", type="primary"):
            init_form_state()
            fill_form(sub["payload"], sub["documents"], parent_id=sub["id"])
            st.session_state.pop("v_show_sid", None)
            st.switch_page(PAGES["v_submit"])


# ---------------------------------------------------------------------------
# TEAM page: new submission + live run
# ---------------------------------------------------------------------------
def page_team_new():
    init_form_state()

    # After "Run" is pressed, the form is replaced by the live run view (full screen, no scrolling).
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
    st.caption("Enter a vendor's details on their behalf, or load a prepared test vendor. You will see every "
               "check run live. (Vendors use their own, simpler page.)")

    cases = testcases.list_cases()
    with st.container(border=True):
        c1, c2, c3 = st.columns([4, 1, 1], vertical_alignment="bottom")
        labels = {c["case_id"]: c["label"] for c in cases}
        chosen = c1.selectbox("Start from a prepared test vendor (optional)", list(labels),
                              format_func=lambda k: labels[k], index=None, placeholder="Choose a test vendor…")
        if c2.button("Load", width="stretch", disabled=not chosen):
            case = testcases.load_case(chosen)
            parent = None
            if case.get("parent_case"):
                parent = db.latest_submission_for_case(case["parent_case"])
                if not parent:
                    st.session_state["load_warning"] = (
                        f"This test vendor is a RESUBMISSION. Run '{labels[case['parent_case']]}' first, then load "
                        f"this one, so the two are linked as version 1 → version 2.")
            fill_form(testcases.case_payload(case), testcases.case_documents(case), chosen, parent)
            st.rerun()
        if c3.button("Clear form", width="stretch"):
            clear_form()
            st.rerun()
    if st.session_state.get("load_warning"):
        st.warning(st.session_state.pop("load_warning"))

    resubmission_banner()
    sid = render_form("▶  Run onboarding check")
    if sid:
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
    boxes = {}
    for no, name in checks.STAGES.items():
        box = st.container(border=True)
        boxes[no] = {"header": box.empty(), "body": box.container(), "name": name}
        boxes[no]["header"].markdown(ui.stage_header_html(no, name, "waiting"), unsafe_allow_html=True)
    result_area = st.container()
    reading = {}     # slot -> the placeholder showing "reading…", later replaced by what was read

    def emit(event, *args):
        if event == "stage_start":
            no, name = args
            boxes[no]["header"].markdown(ui.stage_header_html(no, name, "running"), unsafe_allow_html=True)
            progress.progress((no - 1) / 5, text=f"Stage {no} of 5: {name}")
        elif event == "check":
            r = args[0]
            boxes[r["stage"]]["body"].markdown(ui.check_html(r), unsafe_allow_html=True)
        elif event == "doc_read_start":
            slot, label, filename = args
            reading[slot] = boxes[4]["body"].empty()
            reading[slot].markdown(f'<div class="vo-ai">⏳ Reading the <b>{ui.esc(label)}</b> '
                                   f'({ui.esc(filename)}), twice…</div>', unsafe_allow_html=True)
        elif event == "doc_read_done":
            slot, label, ex = args
            reading[slot].markdown(ui.ai_box_html(label, ex), unsafe_allow_html=True)
        elif event == "stage_end":
            no, worst = args
            boxes[no]["header"].markdown(ui.stage_header_html(no, boxes[no]["name"], worst), unsafe_allow_html=True)
            progress.progress(no / 5, text=f"Stage {no} of 5 done")
        elif event == "done":
            progress.progress(1.0, text="Done")

    if run_safely(sid, emit):
        sub = db.get_submission(sid)
        with result_area:
            st.markdown("### Decision")
            show_run_result(sub, sub["runs"][-1], show_checks=False)


def status_extra(run, status):
    extra = None
    if run.get("overrides"):
        o = run["overrides"][-1]
        extra = (f"Changed by {o['reviewer']} on {ui.ist(o['created_at'])} (the process had said: "
                 f"{run['status']}). Reason: {o['reason']}")
    if run.get("vendor_id") and status == decide.APPROVED:
        extra = (extra + " · " if extra else "") + f"Vendor ID: {run['vendor_id']}"
    return extra


def show_run_result(sub, run, show_checks=True):
    status = db.final_status(run)
    ui.status_card(status, run["summary"], run["reason_codes"], status_extra(run, status))
    c1, c2 = st.columns([3, 2], gap="large")
    with c1:
        if show_checks:
            ui.render_checks_by_stage(run["checks"] or [], run["extractions"])
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
# TEAM page: dashboard
# ---------------------------------------------------------------------------
def aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def requests_table(rows, now):
    table = []
    for r in rows:
        days = (now - aware(r["started_at"])).days
        table.append({
            "sid": r["submission_id"], "Ref": r["request_ref"], "Vendor": r["vendor_name"],
            "Version": f"v{r['version']}",
            "Status": STATUS_ICON.get(r["final_status"], "") + " " + r["final_status"]
                      + (" (changed by reviewer)" if r["overridden"] else ""),
            "Reasons": ", ".join(r["reason_codes"]) or "—", "Submitted": ui.ist(r["started_at"]),
            "Waiting": f"{days} day(s)" if r["final_status"] not in (decide.APPROVED, decide.REJECTED) else "",
            "Source": "Test vendor" if r["source"] == "test_case" else "Vendor form",
        })
    return pd.DataFrame(table)


def clickable_table(df, table_key):
    event = st.dataframe(df.drop(columns=["sid"]), width="stretch", hide_index=True, key=table_key,
                         on_select="rerun", selection_mode="single-row",
                         column_config={"Reasons": st.column_config.TextColumn(width="large")})
    if event.selection.rows:
        st.session_state["detail_sid"] = int(df.iloc[event.selection.rows[0]]["sid"])
        st.switch_page(PAGES["detail"])


def page_dashboard():
    st.title("Dashboard")
    runs = db.list_runs()
    if not runs:
        st.info("No submissions yet. Go to **New submission** and run one, or switch to the Vendor view "
                "and register as a vendor.")
        return

    # The current state of each request = its latest run (latest version)
    latest = {}
    for r in runs:                              # newest first
        latest.setdefault(r["request_ref"], r)
    current = list(latest.values())
    now = datetime.now(timezone.utc)

    counts = {s: sum(r["final_status"] == s for r in current) for s in ALL_STATUSES + [db.RUNNING]}
    secs = [r["seconds"] for r in runs if r["seconds"] is not None]
    m = st.columns(6)
    m[0].metric("Vendors submitted", len(current))
    m[1].metric("Approved", counts[decide.APPROVED])
    m[2].metric("Waiting on vendor", counts[decide.PENDING_VENDOR])
    m[3].metric("Needs our review", counts[decide.PENDING_REVIEW])
    m[4].metric("Rejected", counts[decide.REJECTED])
    m[5].metric("Avg time to decide", f"{sum(secs) / len(secs):.0f} sec" if secs else "–")

    def needs_us(r):
        old = (now - aware(r["started_at"])).days > 3
        return (r["final_status"] in (decide.PENDING_REVIEW, db.DID_NOT_FINISH)
                or (r["final_status"] == decide.PENDING_VENDOR and old))

    queue = sorted([r for r in current if needs_us(r)], key=lambda r: aware(r["started_at"]))   # oldest first
    tab_queue, tab_all, tab_insights = st.tabs([f"Needs attention ({len(queue)})", "All requests", "Insights"])

    with tab_queue:
        st.caption("Work to do, oldest first: submissions waiting for a reviewer, runs that did not finish, "
                   "and vendors who have not replied for more than 3 days. Click a row to open it.")
        if queue:
            clickable_table(requests_table(queue, now), "t_queue")
        else:
            st.success("Nothing needs your attention right now.")

    with tab_all:
        f1, f2, f3, f4 = st.columns([2, 2, 2, 1], vertical_alignment="bottom")
        status_f = f1.multiselect("Status", ALL_STATUSES, placeholder="All statuses")
        code_f = f2.multiselect("Reason", sorted({c for r in runs for c in r["reason_codes"]}),
                                placeholder="All reasons")
        search = f3.text_input("Search vendor or reference")
        show_all = f4.toggle("Every run", help="Also show earlier versions and re-runs")
        rows = [r for r in (runs if show_all else current)
                if (not status_f or r["final_status"] in status_f)
                and (not code_f or set(code_f) & set(r["reason_codes"]))
                and (not search or search.lower() in f"{r['vendor_name']} {r['request_ref']}".lower())]
        if rows:
            st.caption("Click a row to open it.")
            clickable_table(requests_table(rows, now), "t_all")
        else:
            st.warning("Nothing matches these filters.")

    with tab_insights:
        decided = [r for r in current if r["final_status"] in decide.ALL_STATUSES]
        first_runs = {}
        for r in reversed(runs):                # oldest first: the first run of each request
            first_runs.setdefault(r["request_ref"], r)
        clean_first_time = sum(r["status"] == decide.APPROVED for r in first_runs.values())
        overridden = sum(r["overridden"] for r in runs)
        a, b, c = st.columns(3)
        a.metric("Approved first time, no human needed", f"{clean_first_time} of {len(first_runs)}",
                 help="Submissions that passed every check on their first try.")
        b.metric("Decisions changed by a reviewer", overridden,
                 help="If this is high, the rules are too strict or too loose.")
        c.metric("Requests decided", f"{len(decided)} of {len(current)}")
        st.markdown("**Why submissions get stopped** (each request counted once, latest version)")
        reasons = pd.Series([code for r in current for code in r["reason_codes"]]).value_counts()
        if len(reasons):
            table = pd.DataFrame({"What went wrong": [decide.REASON_TEXT.get(c, c) for c in reasons.index],
                                  "Code": reasons.index, "Requests": reasons.values})
            st.dataframe(table, hide_index=True, width="stretch", column_config={
                "Requests": st.column_config.ProgressColumn(format="%d", min_value=0, max_value=int(reasons.max()))})
            st.caption("If one reason dominates, the fix is usually the form or its instructions, "
                       "not chasing each vendor one by one.")
        else:
            st.caption("No problems recorded yet.")


# ---------------------------------------------------------------------------
# TEAM page: one submission in full
# ---------------------------------------------------------------------------
def page_detail():
    runs = db.list_runs()
    if not runs:
        st.title("Submission details")
        st.info("No submissions yet.")
        return
    options = {}
    for r in runs:
        options.setdefault(r["submission_id"],
                           f"{r['request_ref']} v{r['version']} — {r['vendor_name']} ({r['final_status']})")
    ids = list(options)
    default = st.session_state.get("detail_sid")
    sid = st.selectbox("Submission", ids, index=ids.index(default) if default in ids else 0,
                       format_func=options.get)
    st.session_state["detail_sid"] = sid
    sub = db.get_submission(sid)
    run = sub["runs"][-1]
    p = sub["payload"]
    status = db.final_status(run)

    st.title(sub["vendor_name"] or "(no name given)")
    st.markdown(f"{sub['request_ref']} · version {sub['version']} · submitted {ui.ist(sub['created_at'])} · "
                f"{BUSINESS_TYPES.get(p['company'].get('business_type'), '')} · {p['company'].get('state', '')}")
    ui.status_card(status, run["summary"], run["reason_codes"], status_extra(run, status))

    tabs = st.tabs(["Checks", "Documents", "Email to vendor", "What the vendor sees", "Submitted details",
                    "History & audit trail"])
    with tabs[0]:
        if run["checks"]:
            ui.render_checks_by_stage(run["checks"], run["extractions"])
        else:
            st.info("This run has no check results (it is still running, or it did not finish). "
                    "Use **Re-run all checks** below.")
    with tabs[1]:
        if not sub["documents"]:
            st.info("No documents were uploaded.")
        for d in sub["documents"]:
            label = RULES["document_slots"].get(d["slot"], {"label": d["slot"]})["label"]
            ex = (run["extractions"] or {}).get(d["slot"], {})
            with st.container(border=True):
                c1, c2 = st.columns(2, gap="large")
                c1.markdown(f"**{label}** · {ui.esc(d['filename'])}")
                try:
                    c1.image(ui.preview_image(bytes(d["content"]), d["mime_type"]), width="stretch")
                    c1.caption("Preview shows the first page. The AI reads the whole file.")
                except Exception:
                    c1.warning("This file can't be previewed (it may be damaged).")
                with c2:
                    st.markdown(ui.ai_box_html(label, ex), unsafe_allow_html=True)
                    st.download_button("Download file", bytes(d["content"]), file_name=d["filename"],
                                       mime=d["mime_type"], key=f"dl_{d['id']}")
    with tabs[2]:
        ui.email_box(run["email"])
    with tabs[3]:
        st.caption("This is everything the vendor is shown. No check names, no reasons for a review, nothing "
                   "about fraud: telling someone which check caught them teaches them what to change.")
        ui.vendor_card(decide.vendor_view(status, run["checks"] or [], sub["request_ref"], run.get("vendor_id")),
                       sub["request_ref"], db.access_code(sub["request_ref"]))
    with tabs[4]:
        for section in ("company", "contact", "tax", "bank"):
            st.markdown(f"**{section.title()}**")
            st.table(pd.DataFrame([(k.replace("_", " "), str(v)) for k, v in p.get(section, {}).items()],
                                  columns=["Field", "Value"]))
    with tabs[5]:
        st.markdown("**Versions of this request**")
        for v in sub["versions"]:
            vs = db.get_submission(v["id"])
            vstatus = db.final_status(vs["runs"][-1]) if vs["runs"] else "not run"
            st.markdown(f"- v{v['version']} · {ui.ist(v['created_at'])} · {ui.status_badge(vstatus)}"
                        f"{' ← this one' if v['id'] == sid else ''}", unsafe_allow_html=True)
        st.markdown("**Runs and decisions**")
        for r in sub["runs"]:
            took = f"took {r['seconds']} sec" if r["seconds"] is not None else "did not finish"
            st.markdown(f"- Run #{r['id']} · {ui.ist(r['started_at'])} · {took} · the process decided "
                        f"{ui.status_badge(db.shown_status(r['status'], r['started_at']))}", unsafe_allow_html=True)
            for o in r["overrides"]:
                st.markdown(f"  - ✍️ **{ui.esc(o['reviewer'])}** changed it to {ui.status_badge(o['new_status'])} "
                            f"on {ui.ist(o['created_at'])}: “{ui.esc(o['reason'])}”", unsafe_allow_html=True)

    st.divider()
    a1, a2 = st.columns(2, gap="large")
    with a1:
        st.markdown("#### Reviewer decision")
        st.caption("Change the decision. Your name and a reason are required and are kept in the audit trail.")
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
                    st.rerun()
    with a2:
        st.markdown("#### Other actions")
        st.caption("Resubmit: opens the form filled with this submission; saved as the next version, and "
                   "every check runs again.")
        if st.button("Resubmit as a new version", width="stretch"):
            init_form_state()
            fill_form(p, sub["documents"], case_id=sub["case_id"], parent_id=sid)
            st.session_state.pop("show_sid", None)
            st.switch_page(PAGES["new"])
        if st.button("Re-run all checks on this version", width="stretch"):
            st.session_state["run_now"] = sid
            st.switch_page(PAGES["new"])


# ---------------------------------------------------------------------------
# Sidebar + navigation
# ---------------------------------------------------------------------------
PAGES = {
    "dashboard": st.Page(page_dashboard, title="Dashboard", icon="📊", default=True),
    "new": st.Page(page_team_new, title="New submission", icon="📝", url_path="new"),
    "detail": st.Page(page_detail, title="Submission details", icon="🔎", url_path="details"),
    "v_submit": st.Page(page_vendor_submit, title="Register as a vendor", icon="📝", url_path="register"),
    "v_track": st.Page(page_vendor_track, title="Track your submission", icon="📍", url_path="track"),
}
TEAM_PAGES = [PAGES["dashboard"], PAGES["new"], PAGES["detail"]]
VENDOR_PAGES = [PAGES["v_submit"], PAGES["v_track"]]

# Every page is always registered, so a link such as /track works for anyone who opens it directly.
# Which side you are on is decided by the page you are looking at; the sidebar only lists that
# side's pages, and the switch simply takes you to the other side's first page.
current = st.navigation(TEAM_PAGES + VENDOR_PAGES, position="hidden")
role = VENDOR if current in VENDOR_PAGES else TEAM

with st.sidebar:
    st.markdown("### Vendor Onboarding")
    st.caption("Acme Retail Pvt Ltd")
    choice = st.radio("You are viewing as", [TEAM, VENDOR], index=[TEAM, VENDOR].index(role),
                      help="A demo switch. In a real rollout these are two separate sites, and the team "
                           "side needs a company login.")
    if choice != role:
        st.switch_page(VENDOR_PAGES[0] if choice == VENDOR else TEAM_PAGES[0])
    st.divider()
    for page in (TEAM_PAGES if role == TEAM else VENDOR_PAGES):
        st.page_link(page)
    st.divider()
    if role == TEAM:
        st.caption("Document reading: " + ("ready" if extract.ai_available()
                                           else "NOT set up — runs will be held for review"))
        st.caption("History: " + ("saved permanently" if db.using_hosted_db() else "saved on this computer only"))
    st.caption("Demo data: all vendors and documents in the test cases are fictional.")

current.run()
