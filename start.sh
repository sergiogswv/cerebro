#!/bin/bash
./venv/bin/python3 -m uvicorn app.main:app --host 0.0.0.0 --port 4000
