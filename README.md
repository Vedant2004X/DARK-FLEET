# DARK-FLEET v0.1 starter

This is the first runnable foundation: FastAPI backend + React/TypeScript frontend.
All vessel records are explicitly illustrative demo data, not real AIS/SAR observations.

## Requirements
Python 3.11/3.12, Node.js LTS, npm, Git.

## Backend (Terminal 1)
Windows PowerShell:
```powershell
cd backend
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload
```
macOS/Linux:
```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
uvicorn app.main:app --reload
```
Check http://127.0.0.1:8000/docs and http://127.0.0.1:8000/api/health

## Frontend (Terminal 2, from project root)
```bash
cd frontend
npm install
npm run dev
```
Open the URL Vite prints, usually http://localhost:5173.

## Git
From project root:
```bash
git init
git add .
git commit -m "chore: initialize DARK-FLEET"
```

Next: connect real public AIS/SAR data, create reproducible features, benchmark baselines, then deploy.
Do not use demo values as real detections or model-performance claims.
