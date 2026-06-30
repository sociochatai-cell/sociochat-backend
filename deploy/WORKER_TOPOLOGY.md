# Multi-process topology (Option B: separate worker + Redis)

After the WhatsApp rebase merge, the backend runs as **three processes** sharing one Postgres
database and one Redis instance. This is the bug_fix production topology, adapted in-process
(no separate WhatsApp microservice — everything is the one `app:app`).

```
                         ┌─────────────────────────────┐
  Browser / Meta  ──────▶│  API   (gunicorn app:app)   │  HTTP + webhooks + enqueues jobs
                         └──────────────┬──────────────┘
                                        │ enqueue (Redis)
                         ┌──────────────▼──────────────┐
                         │  Worker (python worker.py)  │  consumes drip/bulk/warmup jobs
                         └──────────────┬──────────────┘
                                        │
        Postgres  ◀────────────────────┴────────────────────▶  Redis
        (shared app DB)                                         (job queue)
```

## Required env (shared `.env`, used by BOTH api and worker)
| Var | Value | Notes |
|---|---|---|
| `JOB_QUEUE_BACKEND` | `redis` | Switches from inline mode to the queue. Must be set on api AND worker. |
| `REDIS_URL` | `redis://127.0.0.1:6379/0` | Or managed Redis (e.g. GCP Memorystore). |
| `SQLALCHEMY_DATABASE_URI` | Postgres URL | Same DB for api + worker. |
| `QDRANT_ENDPOINT` / `QDRANT_KEY` | (optional) | RAG knowledge base; degrades if unset. |
| `AGENTOS_API_BASE` | (optional) | AI handoff; disabled if unset. |

If `JOB_QUEUE_BACKEND` is left unset/`inline`, the worker idles and the API processes
everything inline (single-process fallback — fine for low volume / first smoke tests).

## Processes
1. **API** — `gunicorn --workers 3 --bind 127.0.0.1:5000 app:app` → `deploy/sociochat-backend.service` (existing).
2. **Worker** — `python worker.py` → `deploy/sociochat-worker.service` (this folder).
3. **Usage-consumer** — runs **inline** via the in-process APScheduler (`whatsapp/scheduler.py`) +
   the `/api/internal/scheduler/tick` endpoint; no separate process required. (To run it as a
   standalone process instead, set `WHATSAPP_USAGE_INLINE_POLL=false` and add a service that
   invokes the usage consumer — not needed for the default setup.)

## Local dev
`docker compose -f docker-compose.dev.yml up -d` brings up Postgres + Redis + Qdrant. Run the API
(`python app.py`) and, in a second shell, the worker (`JOB_QUEUE_BACKEND=redis python worker.py`).

## Caddy / Cloud Run
- Caddy/Firebase still front ONLY the API (same-origin `/api` rewrite). The worker has no public
  routes (its `/health` is for the platform health check only).
- On Cloud Run, deploy api and worker as two services from the same image, differing by
  `SERVICE_MODE` (api vs worker) and the start command.
