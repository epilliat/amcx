@echo off
REM Met a jour AMCx. Ne touche a aucun projet : sujets, copies et notes vivent
REM hors du depot (Documents\AMCx\), ils ne sont jamais ecrases.
setlocal
cd /d "%~dp0"

git status --porcelain > "%TEMP%\amcx_dirty.txt"
for /f %%i in ("%TEMP%\amcx_dirty.txt") do set SIZE=%%~zi
if not "%SIZE%"=="0" (
  echo /!\ Le depot contient des modifications locales :
  type "%TEMP%\amcx_dirty.txt"
  echo.
  echo   Mise a jour annulee pour ne rien ecraser.
  del "%TEMP%\amcx_dirty.txt"
  exit /b 1
)
del "%TEMP%\amcx_dirty.txt"

echo --^> Recuperation de la derniere version...
git pull --ff-only
if errorlevel 1 exit /b 1

echo --^> Mise a jour des dependances...
.venv\Scripts\pip install -e . --upgrade-strategy only-if-needed

echo.
echo --^> Diagnostic...
.venv\Scripts\python auto_grading\doctor.py

echo.
echo OK A jour. Relancer le serveur : run.bat
