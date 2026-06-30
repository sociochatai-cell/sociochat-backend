# Monolith integration — WhatsApp capabilities + usage events

## Environment variables

| Variable | Purpose |
|----------|---------|
| `WHATSAPP_INTERNAL_API_URL` | Base URL of WhatsApp API service (no trailing slash), e.g. `https://whatsapp-api-xxx.run.app` |
| `WHATSAPP_CAPABILITIES_UPSERT_SECRET` | Bearer token for `POST|PUT /api/internal/whatsapp/accounts/<id>/capabilities` |
| `WHATSAPP_USAGE_EVENTS_SECRET` | Optional; falls back to capabilities secret for `GET /api/internal/whatsapp/usage-events` |
| `SQLALCHEMY_DATABASE_URI` | Shared PostgreSQL (checkpoints + dedupe + quarantine tables from migration `006`) |
| `WHATSAPP_USAGE_CONSUMER_NAME` | Checkpoint row key (default `billing_default`) |

## Capability writer

- Call `schedule_capabilities_resync_for_user(user_id, reason=...)` after plan or subscription-affecting changes (already wired from `change_user_plan` and `save_whatsapp_account` in this repo).
- HTTP: `PUT {WHATSAPP_INTERNAL_API_URL}/api/internal/whatsapp/accounts/{account_id}/capabilities` with retries and structured logs.

## Usage consumer CLI

```bash
cd whatsapp-service
set PYTHONPATH=.
python scripts/run_whatsapp_usage_consumer.py
```

Runs a polling loop with checkpoint + `event_key` dedupe per `DEPLOYMENT_READINESS.md` / `WHATSAPP_CAPABILITIES_CONTRACT.md`.

## Billing handler

Pass a callable to `WhatsappUsageEventConsumer(..., on_message_sent=fn)` or use the default which calls `subscription.service.record_message_sent` when the owning user can be resolved.
