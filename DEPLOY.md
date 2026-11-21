# Lyftr backend — deploy on Render

FastAPI + LiteLLM + SQLite in one Docker image. Endpoints: `GET /health`, `POST /chat`,
`GET /sessions/{session_id}/messages`.

## Contents
| Path | Purpose |
|---|---|
| `Dockerfile` | Multi-stage image, non-root uid 10001, one uvicorn worker on `$PORT` (default 8000) |
| `.dockerignore` | Allow-list: only `requirements.txt`, `app/`, `docker/healthcheck.py` enter the build |
| `app/` | Application code |
| `requirements.txt` | Pinned dependencies (LiteLLM 1.110.2) |
| `render.yaml` | Render Blueprint: Docker web service + 1 GB disk at `/data` |
| `scripts/verify_contract.sh` | Post-deploy curl checks of the API contract |

## Option A — Blueprint (recommended)
1. Push this folder to its own GitHub repository (`.env` is git-ignored — never commit keys).
2. Render → **New → Blueprint** → pick the repo. Render reads `render.yaml`.
3. When prompted, fill in:
   - `LLM_API_KEYS` — two distinct Gemini keys from two Google Cloud projects, comma-separated.
   - `ALLOWED_ORIGINS` — the frontend URL (`https://<frontend>.onrender.com`). If the frontend
     does not exist yet, enter `http://localhost:3000` and update it after step 2 of the frontend guide.
4. Deploy. Check `https://<backend>.onrender.com/health` → `200` with `"keys_configured": 2`.

## Option B — Dashboard
New → **Web Service** → repo → Language **Docker**, Dockerfile path `./Dockerfile`, a paid
instance type (not Free), Health Check Path `/health`, **Disk** mount path `/data` (1 GB), and the env vars
from `render.yaml` / `.env.example`.

## Notes
- Do not set `DATABASE_URL`; the image uses `sqlite:////data/app.db` on the disk.
- Do not set `PORT`; Render injects it and the image reads it.
- Keep **one instance**: the cache, key health and session locks are in memory.
- Free instances cannot have a disk: SQLite history would be lost on each restart/spin-down,
  and the service sleeps after 15 minutes idle.
- `/health` returns `503 {"detail":"service not ready"}` until two distinct keys are set; Render
  then keeps the previous deploy live / marks the deploy unhealthy.

## Verify after deploy
```bash
curl https://<backend>.onrender.com/health
scripts/verify_contract.sh https://<backend>.onrender.com https://<frontend>.onrender.com
```
`verify_contract.sh` needs `curl` and `jq`, and `ENABLE_FAILURE_SIMULATION=true`.

## Run locally
```bash
docker build -t lyftr-backend .
docker run --rm -p 8000:8000 --env-file .env -v lyftr_data:/data lyftr-backend
```
