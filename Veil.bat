@echo off
rem Double-click to start veil and open it in your browser.
rem Keep this window open while you use veil; closing it stops veil.
setlocal
title veil
set "URL=http://127.0.0.1:7788"

rem Work from the project folder, whether this file sits in it or one level up.
cd /d "%~dp0"
if not exist "pyproject.toml" if exist "veil\pyproject.toml" cd /d "%~dp0veil"
if not exist "pyproject.toml" (
  echo Can't find the veil project next to this file.
  pause
  exit /b 1
)

rem Already running? Just open it.
curl -s -o nul -m 2 "%URL%/api/state" && (
  echo veil is already running: opening %URL%
  start "" "%URL%"
  ping -n 3 127.0.0.1 >nul
  exit /b 0
)

where uv >nul 2>nul || (
  echo uv is not installed or not on PATH: https://docs.astral.sh/uv/
  pause
  exit /b 1
)

rem First run (or after a fresh clone): build the chat app.
if not exist "web\out\index.html" (
  where npm >nul 2>nul || (
    echo Node.js is needed once to build the app: https://nodejs.org/
    pause
    exit /b 1
  )
  echo Building the veil app (first run only^)...
  pushd web
  if not exist "node_modules" call npm install --no-audit --no-fund || (popd & pause & exit /b 1)
  call npm run build || (popd & pause & exit /b 1)
  popd
)

echo Starting veil on %URL% ... (close this window to stop it)
uv run veil ui
if errorlevel 1 pause
