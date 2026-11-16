"""Run the app WITHOUT the AI, for testing the screens offline:

    streamlit run tests/offline_app.py

Document reading is replaced by the answer key (what is really printed on the test documents).
Only for development. The real app (app.py) always uses the real AI.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import extract            # noqa: E402
import run_cases          # noqa: E402

extract.read_document = run_cases.answer_key_reader
extract.ai_available = lambda: True
extract.judge_names = lambda a, b: None

exec(compile((ROOT / "app.py").read_text(), str(ROOT / "app.py"), "exec"))
