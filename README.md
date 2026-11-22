# Lyftr Backend

FastAPI backend for Lyftr. LiteLLM handles provider calls and local response caching; SQLite stores successful chat turns and session history.

## Live API

- API base URL: https://test-backend-pao2.onrender.com
- Health: https://test-backend-pao2.onrender.com/health
- Frontend: https://test-frontend-plum-seven.vercel.app

## Requirements

- Python 3.12 or newer
- At least two distinct provider API keys for a ready service
- A persistent disk mounted at `/data` when running the Docker deployment

## Run locally without Docker

From this directory, create `.env` from `.env.example`, add your provider keys, and change `DATABASE_URL` to a local path:

```bash
cp .env.example .env
# Edit .env: set LLM_API_KEYS and DATABASE_URL=sqlite:///./data/app.db
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

The API listens at http://localhost:8000. `/health` reports `503 service not ready` until the database, cache, and two distinct keys are ready.

To run both frontend and backend with Docker, use the project-root `docker-compose.yml` and `make up` instructions in the top-level README.

## API endpoints

- `GET /health` — readiness and service configuration status.
- `POST /chat` — send one new user message. The request contains `session_id`, `message`, and optional `system_prompt`; the response includes the reply and per-request metadata.
- `GET /sessions/{session_id}/messages` — retrieve a session's stored history, oldest first.

Provider failures are retried against the next configured key. If all keys fail, the backend returns JSON `503` with `{"detail":"all providers unavailable"}`. Invalid chat inputs return JSON `422`. Failed requests are not written to history.

## Failure simulation

Set `ENABLE_FAILURE_SIMULATION=true` to enable the test hook. Example request header:

```text
X-Simulate-Failure: key_1=429
```

Supported statuses are `429`, `500`, `502`, and `503`; `*` targets every key. Simulated errors apply to one request and do not change key health or order.

## Tests

Tests use Python's standard library `unittest` runner:

```bash
python -m unittest discover -s tests -v
```

## Deploy

See [DEPLOY.md](./DEPLOY.md) for the Render Blueprint and Docker instructions. The production Docker image stores `sqlite:////data/app.db` on the mounted `/data` volume and runs as a non-root user. Keep one backend instance because the LiteLLM cache and key health are in memory.
