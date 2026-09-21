@echo off
REM meshtech-node COMPANION DEVICE starter (Windows PC).
REM
REM One-time setup:
REM   1.  python -m venv .venv
REM   2.  .venv\Scripts\pip install -e . aiohttp pycryptodome
REM   3.  copy deploy\config.companion.json config.json
REM   4.  edit config.json: set companion_host to hilltop's IP
REM   5.  put the observer token hilltop printed into companion.token
REM
REM Then start this device any time with:  start-companion.cmd
REM The web app opens at http://127.0.0.1:8710/

cd /d "%~dp0.."
if not exist config.json (
  echo No config.json found - copy deploy\config.companion.json to
  echo config.json and set companion_host to hilltop's IP first.
  pause
  exit /b 1
)
if not exist companion.token (
  echo No companion.token found - copy the observer token hilltop
  echo printed into a file named companion.token in this folder.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m meshtech_node --config config.json
pause
