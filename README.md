# Vendor Onboarding — Zamp ASA case study (PS-2)

A working process that takes a vendor submission (form + documents) and decides
**Approved / Pending (waiting on vendor) / Pending (internal review) / Rejected**, shows the
reasoning for every check, drafts the email back to the vendor, and keeps a full history.

All test vendors and documents are fictional and stamped "SAMPLE - FOR TESTING ONLY".

## How it works

```
Form + documents
   │
   ├─ Stage 1  Completeness   C1 fields, C2 documents
   ├─ Stage 2  Format         C3 PAN, C4 GSTIN + checksum, C5 IFSC exists (RBI list), C6 account no.
   ├─ Stage 3  Consistency    C7 PAN inside GSTIN, C8 GSTIN state vs address, C9 PIN vs state,
   │                          C10 PAN type vs business type, C11 IFSC bank vs bank name,
   │                          C12 account holder vs company (fuzzy match)
   ├─ Stage 4  Documents (AI) Gemini reads each file → C13 right document & readable,
   │                          C14 details on the document match the form
   └─ Stage 5  Risk           C15 duplicate vendor / shared bank account, C16 email domain
   │
   Decision: the worst failure decides
     blocker → Rejected · review → Pending (internal review) · fixable → Pending (vendor) · none → Approved
```

**Design rules**
- Anything with a right answer is plain code (formats, lookups, duplicates): free, instant, repeatable.
- AI only reads documents (and gives a second opinion on borderline names). It never decides.
- AI output is always shown next to the form value, with its confidence. Low confidence → ask for
  a re-upload, never guess. If the AI can't be reached, nothing is approved.
- The vendor email is built from fixed templates per check, so it can't invent or drop an issue.
- India-specific rules live in `rules/india.json`, not in code.

## Files

| File | What it does |
|---|---|
| `app.py` | The screens: New submission + live run, Dashboard, Submission details |
| `engine.py` | Runs the 5 stages in order and reports each step to the screen |
| `checks.py` | Every check (C1–C16), one small function each |
| `extract.py` | Gemini document reading (+ cache) |
| `decide.py` | Worst-result-wins decision, plain-English summary, vendor email |
| `db.py` | Storage: hosted Postgres if `DATABASE_URL` is set, else a local SQLite file |
| `ui.py` | Colours, badges, check lines, document previews |
| `rules/india.json` | Required fields/docs, GST state codes, PIN regions, PAN types |
| `data/` | Existing vendors (for duplicates), RBI IFSC branch list + bank names (Razorpay open dataset) |
| `test_cases/` | Prepared test vendors (JSON) + their documents + `document_truth.json` (answer key) |
| `tools/make_test_data.py` | Rebuilds all test data |
| `tests/run_cases.py` | Runs every test case and checks the result |

## Run it on your laptop

```bash
cd vendor-onboarding
python3 -m pip install -r requirements.txt
mkdir -p .streamlit
echo 'GEMINI_API_KEY = "paste-your-key-here"' > .streamlit/secrets.toml
streamlit run app.py
```
It opens in your browser. Without `DATABASE_URL`, data is saved in `vendor_onboarding.db` on your laptop.

Test the rules without AI: `python3 tests/run_cases.py`
Test with real AI reading (shows how accurately Gemini read each document): `python3 tests/run_cases.py --real-ai`

## Deploy (public link that keeps its history)

1. **Database** — create a free Postgres database at https://neon.tech (sign in with GitHub →
   New project → copy the connection string that starts with `postgresql://`).
2. **Code** — push this folder to GitHub (`secrets.toml` and `*.db` are excluded by `.gitignore`).
3. **App** — https://share.streamlit.io → Create app → pick the repo, branch `main`, file `app.py`.
   Under *Advanced settings → Secrets* paste:
   ```toml
   GEMINI_API_KEY = "your-gemini-key"
   GEMINI_MODEL = "gemini-2.5-flash"
   DATABASE_URL = "postgresql://...your neon string..."
   ```
4. Open the link. The sidebar should say *AI document reading: on* and *Storage: hosted database*.

## Assumptions and limits
- Buyer is a mid-size Indian company; vendors are Indian (foreign vendors are a later extension).
- Formats and consistency are checked; live government/bank APIs (GSTN lookup, penny-drop) are not
  called. They would plug into Stage 2/3.
- The vendor email is drafted and shown, not sent.
- Gemini free tier: requests may be used by Google to improve its products, so only fictional
  documents should be uploaded to the demo.
- Demo test cases are excluded from the duplicate check against approved vendors, so the demo can be
  re-run any number of times.
