"""
ui.py - small drawing helpers shared by all screens (colours, status badges, check lines, previews).

Colour means status and nothing else:  green = fine,  amber = needs someone,  red = stop,  grey = not run.
"""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

import streamlit as st

import checks
import decide

IST = ZoneInfo("Asia/Kolkata")

STATUS_STYLE = {
    decide.APPROVED: ("#1B7F4B", "#E7F5EC", "✓"),
    decide.PENDING_VENDOR: ("#9A6700", "#FFF6DD", "↺"),
    decide.PENDING_REVIEW: ("#B4560B", "#FDEEE2", "!"),
    decide.REJECTED: ("#B42318", "#FDECEA", "✕"),
}
NEXT_STEP = {
    decide.APPROVED: "Nothing to do. The vendor has a vendor ID and can be paid.",
    decide.PENDING_VENDOR: "Send the drafted email. The vendor fixes the issues and resubmits.",
    decide.PENDING_REVIEW: "A reviewer must look at the evidence below and approve or reject.",
    decide.REJECTED: "Send the neutral rejection email. No further action.",
}
SEVERITY_TAG = {
    "fixable": ("Vendor to fix", "#9A6700", "#FFF6DD"),
    "review": ("Needs review", "#B4560B", "#FDEEE2"),
    "blocker": ("Blocker", "#B42318", "#FDECEA"),
}

CSS = """
<style>
.block-container {padding-top: 2rem; max-width: 1200px;}
h1, h2, h3 {letter-spacing: -0.01em;}
.vo-card {border:1px solid #E4E7EC; border-radius:12px; padding:16px 18px; background:#fff; margin-bottom:12px;}
.vo-status {border-radius:12px; padding:18px 22px; margin:6px 0 14px 0;}
.vo-status .big {font-size:1.6rem; font-weight:700;}
.vo-status .sub {font-size:0.95rem; margin-top:6px; color:#344054;}
.vo-chip {display:inline-block; padding:2px 9px; border-radius:999px; font-size:0.78rem; font-weight:600;
          margin:2px 4px 2px 0; border:1px solid #E4E7EC; background:#F9FAFB; color:#344054;}
.vo-check {display:flex; gap:10px; padding:7px 0; border-bottom:1px dashed #EEF0F3; font-size:0.93rem;}
.vo-check:last-child {border-bottom:none;}
.vo-dot {flex:0 0 22px; height:22px; border-radius:50%; color:#fff; font-size:0.75rem; font-weight:700;
         display:flex; align-items:center; justify-content:center; margin-top:1px;}
.vo-cid {color:#667085; font-weight:600; margin-right:4px;}
.vo-tag {display:inline-block; padding:0 7px; border-radius:6px; font-size:0.72rem; font-weight:700; margin-left:6px;}
.vo-stage {display:flex; align-items:center; gap:10px; font-weight:700; font-size:1.02rem;}
.vo-stage .state {font-weight:500; font-size:0.85rem; color:#667085;}
.vo-muted {color:#667085; font-size:0.88rem;}
.vo-ai {background:#F5F8FF; border:1px solid #D9E2FF; border-radius:10px; padding:10px 12px; margin:6px 0 8px 0;
        font-size:0.88rem;}
</style>
"""


def inject_css():
    st.markdown(CSS, unsafe_allow_html=True)


def esc(x):
    return html.escape(str(x if x is not None else ""))


def ist(dt):
    if dt is None:
        return ""
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(IST).strftime("%d %b %Y, %I:%M %p")


def status_card(status, summary, codes=None, extra=None):
    fg, bg, icon = STATUS_STYLE.get(status, ("#344054", "#F2F4F7", "•"))
    chips = "".join(f'<span class="vo-chip">{esc(c)}</span>' for c in (codes or []))
    st.markdown(
        f'<div class="vo-status" style="background:{bg}; border:1px solid {fg}33;">'
        f'<div class="big" style="color:{fg};">{icon}&nbsp; {esc(status)}</div>'
        f'<div class="sub">{esc(summary)}</div>'
        f'<div class="sub"><b>Next step:</b> {esc(NEXT_STEP.get(status, ""))}</div>'
        f'{"<div class=sub>" + esc(extra) + "</div>" if extra else ""}'
        f'<div style="margin-top:8px;">{chips}</div></div>', unsafe_allow_html=True)


def status_badge(status):
    fg, bg, icon = STATUS_STYLE.get(status, ("#344054", "#F2F4F7", "•"))
    return (f'<span style="background:{bg}; color:{fg}; border:1px solid {fg}44; padding:2px 10px; '
            f'border-radius:999px; font-weight:700; font-size:0.82rem;">{icon} {esc(status)}</span>')


def check_html(r):
    if r["outcome"] == "pass":
        color, sym, tag = "#1B7F4B", "✓", ""
    elif r["outcome"] == "skip":
        color, sym, tag = "#98A2B3", "–", ""
    elif r["outcome"] == "info":
        color, sym, tag = "#475467", "i", '<span class="vo-tag" style="background:#F2F4F7;color:#475467;">Info</span>'
    else:
        label, fg, bg = SEVERITY_TAG[r["severity"]]
        color = fg
        sym = "✕" if r["severity"] == "blocker" else "!"
        tag = f'<span class="vo-tag" style="background:{bg};color:{fg};">{label}</span>'
    return (f'<div class="vo-check"><div class="vo-dot" style="background:{color};">{sym}</div>'
            f'<div><span class="vo-cid">{esc(r["id"])}</span><b>{esc(r["name"])}</b>{tag}<br>'
            f'<span class="vo-muted" style="color:#344054;">{esc(r["message"])}</span></div></div>')


STAGE_STATE = {
    "waiting": ("#D0D5DD", "○", "waiting"),
    "running": ("#2E6BE6", "◐", "running…"),
    "pass": ("#1B7F4B", "✓", "all passed"),
    "fixable": ("#9A6700", "!", "vendor needs to fix something"),
    "review": ("#B4560B", "!", "needs a human"),
    "blocker": ("#B42318", "✕", "blocked"),
}


def stage_header_html(no, name, state):
    color, sym, text = STAGE_STATE[state]
    return (f'<div class="vo-stage"><div class="vo-dot" style="background:{color};">{sym}</div>'
            f'Stage {no} · {esc(name)} <span class="state">— {esc(text)}</span></div>')


def ai_box_html(label, ex):
    if ex.get("error"):
        return f'<div class="vo-ai">🤖 <b>{esc(label)}</b>: could not be read — {esc(ex["error"])}</div>'
    fields = ex.get("fields", {})
    rows = " · ".join(f"<b>{esc(k.replace('_', ' '))}</b>: {esc(v)}" for k, v in fields.items()) or "no fields found"
    how = "from cache (read before)" if ex.get("cached") else f"read in {ex.get('seconds', '?')}s"
    return (f'<div class="vo-ai">🤖 <b>{esc(label)}</b> → AI says it\'s a '
            f'<b>{esc((ex.get("document_type") or "?").replace("_", " "))}</b>, '
            f'confidence <b>{ex.get("confidence", 0):.0%}</b> <span class="vo-muted">({how})</span><br>{rows}'
            f'{"<br><span class=vo-muted>Note: " + esc(ex["notes"]) + "</span>" if ex.get("notes") else ""}</div>')


def worst_of(results):
    fails = [r for r in results if r["outcome"] == "fail"]
    for sev in ("blocker", "review", "fixable"):
        if any(r["severity"] == sev for r in fails):
            return sev
    return "pass"


def render_checks_by_stage(results, extractions=None):
    """Static view of a finished run, grouped by stage (used on the detail page and after a live run)."""
    for no, name in checks.STAGES.items():
        rs = [r for r in results if r["stage"] == no]
        with st.container(border=True):
            st.markdown(stage_header_html(no, name, worst_of(rs)), unsafe_allow_html=True)
            if no == 4 and extractions:
                for slot, ex in extractions.items():
                    label = checks.RULES["document_slots"].get(slot, {"label": slot})["label"]
                    st.markdown(ai_box_html(label, ex), unsafe_allow_html=True)
            st.markdown("".join(check_html(r) for r in rs), unsafe_allow_html=True)


@st.cache_data(show_spinner=False, max_entries=200)
def preview_image(content: bytes, mime_type: str):
    """First page of a PDF as a PNG (or the image itself), for showing documents on screen."""
    if mime_type == "application/pdf":
        import io

        import pypdfium2
        page = pypdfium2.PdfDocument(content)[0]
        img = page.render(scale=1.4).to_pil()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    return content


def email_box(email):
    if not email:
        st.info("No email to the vendor yet: a reviewer must decide first. "
                "(We don't ask a vendor to fix typos while a fraud question is open.)")
        return
    st.markdown(f"**To:** {esc(email['to'])}  \n**Subject:** {esc(email['subject'])}")
    st.text_area("Email body", email["body"], height=260, label_visibility="collapsed")
    st.caption("Drafted automatically from the failed checks. In this demo it's shown here, not sent.")
