@echo off
REM Installe AMCx dans un venv local (.venv\) et indique comment lancer.
setlocal

cd /d "%~dp0"

REM 1) Python
where python >nul 2>&1
if errorlevel 1 (
  echo X Python introuvable. Installer Python 3.10+ depuis https://www.python.org/downloads/
  exit /b 1
)

REM 2) pdflatex (warning seulement)
where pdflatex >nul 2>&1
if errorlevel 1 (
  echo.
  echo /!\ pdflatex introuvable. Pour compiler le sujet, installer MiKTeX :
  echo     https://miktex.org/download
  echo.
)

REM 3) venv + install
echo --^> Creation du venv (.venv\)...
python -m venv .venv

echo --^> Installation des dependances...
.venv\Scripts\pip install --upgrade pip >nul
.venv\Scripts\pip install -e .

echo.
echo OK Installe. Pour lancer le serveur :
echo     run.bat
echo.
echo   Puis ouvrir http://localhost:5050/ dans le navigateur.
