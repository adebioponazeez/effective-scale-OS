#!/usr/bin/env bash
set -euo pipefail
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
pip install -e ".[test]"
pytest -q
python -m saf.cli.main doctor
