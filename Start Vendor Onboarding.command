#!/bin/bash
# Double-click this file to start the vendor onboarding app.
# It sets everything up the first time (that takes a few minutes), then opens in your browser.
cd "$(dirname "$0")" || exit 1
clear
echo "Starting the vendor onboarding app..."
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 isn't installed on this Mac."
  echo "Install it from https://www.python.org/downloads/ (click the big yellow button),"
  echo "then double-click this file again."
  echo
  read -r -p "Press Enter to close."
  exit 1
fi

if [ ! -d .venv ]; then
  echo "First-time setup. This takes 2-5 minutes; you only wait once."
  python3 -m venv .venv || { echo "Could not create the environment."; read -r; exit 1; }
fi
# shellcheck disable=SC1091
source .venv/bin/activate

if ! python -c "import streamlit" >/dev/null 2>&1; then
  echo "Installing what the app needs..."
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt || { echo "Install failed."; read -r; exit 1; }
fi

echo
echo "Opening the app in your browser."
echo "Leave this black window open while you use it."
echo "To stop the app: close this window, or press Control+C here."
echo
python -m streamlit run app.py
