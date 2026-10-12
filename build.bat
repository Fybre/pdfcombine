@echo off
rem Build the standalone app for Windows (dist\PDFCombine.exe).
cd /d "%~dp0"
if not exist .venv py -3 -m venv .venv
.venv\Scripts\python -m pip install -q -r requirements.txt || exit /b 1
.venv\Scripts\python -m PyInstaller --noconfirm --clean pdfcombine.spec
