$ErrorActionPreference = "Stop"
py -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\pip.exe install -e ".[test]"
.venv\Scripts\pytest.exe -q
.venv\Scripts\python.exe -m saf.cli.main doctor
