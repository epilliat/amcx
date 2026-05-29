#!/usr/bin/env bash
cd "$(dirname "$0")"
exec .venv/bin/python auto_grading/front/server.py --port 5050 "$@"
