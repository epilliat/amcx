@echo off
cd /d "%~dp0"
.venv\Scripts\python auto_grading\front\server.py --port 5050 %*
