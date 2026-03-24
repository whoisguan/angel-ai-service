@echo off
echo Starting Angel AI Service on port 8001...
cd /d %~dp0
python -m uvicorn main:app --host 0.0.0.0 --port 8001 --reload
