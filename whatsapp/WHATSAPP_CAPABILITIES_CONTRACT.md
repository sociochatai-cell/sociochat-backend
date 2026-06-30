# WhatsApp account capabilities — entitlement projection contract

Operator checklist (migrations, env, rollout): `whatsapp-service/DEPLOYMENT_READINESS.md`.

## Ownership

| Concern | Owner |
|--------|--------|
| Plans, trials, invoices, coupons, lifecycle | **Monolith** (billing domain) |
| Operational snapshot in `whatsapp_account_capabilities` | **Monolith writer** (app or outbox worker) |
| Read + enforce gates | **WhatsApp microservice** only |

The WhatsApp service **must not** query `users`, `subscriptions`, `plans`, or payment tables for authorization.

## Table

`whatsapp_account_capabilities` (see `whatsapp/migrations/004_whatsapp_account_capabilities_expand.sql`)

- `account_id` — `INTEGER` PK, FK → `whatsapp_accounts.id` (same numeric id as elsewhere in this service; not a UUID in this schema).
- `subscription_status` — short string, e.g. `ACTIVE`, `TRIAL`, `PAST_DUE`, `CANCELED`. WhatsApp treats a small deny-list as “no outbound / no gated features” (see `whatsapp/capabilities.py`).
- `ai_enabled`, `automation_enabled`, `broadcast_enabled` — booleans.
- `daily_message_limit` — optional; if `> 0`, WhatsApp counts **outbound** rows for that account since **UTC midnight** and denies Cloud API sends when count ≥ limit.
- `monthly_ai_tokens` — optional reserved field for future enforcement (not enforced in v1).
- `projection_version` — monotonic integer for **drift / ordering** (optional for optimistic concurrency on the writer side).
- `updated_at` — writer sets on each upsert.

## Writer API (monolith → WhatsApp DB)

**Endpoint:** `POST` or `PUT`  
`/api/internal/whatsapp/accounts/<account_id>/capabilities`

**Auth:** `Authorization: Bearer <WHATSAPP_CAPABILITIES_UPSERT_SECRET>`  
Set `WHATSAPP_CAPABILITIES_UPSERT_SECRET` in the WhatsApp API deployment; the monolith worker uses the same value.

**Body (JSON, fields optional — omit keys you are not changing):**

```json
{
  "subscription_status": "ACTIVE",
  "ai_enabled": true,
  "automation_enabled": true,
  "broadcast_enabled": false,
  "daily_message_limit": 5000,
  "monthly_ai_tokens": null,
  "projection_version": 12
}
```

**Response:** `{ "success": true, "account_id": <int> }` or `4xx` with `error`.

## Usage events (WhatsApp → Billing)

WhatsApp now emits durable outbound usage events to `whatsapp_usage_events`
(`whatsapp/migrations/005_whatsapp_usage_events_expand.sql`), instead of mutating
billing tables directly.

- Current event type: `message_sent`
- Emission point: successful outbound store path (`whatsapp/services.py::_store_outgoing_message`)
- Idempotency: unique `event_key` (`message_sent:<wamid>` or `message_sent:message:<id>` fallback)

**Reader endpoint (internal):**  
`GET /api/internal/whatsapp/usage-events?since_id=<id>&limit=<n>&event_type=message_sent`

**Response fields (for consumers and ops):**

- `since_id` / `last_id` — bounds of this page (strictly increasing `id` order).
- `stream_max_id` — `MAX(id)` over the whole `whatsapp_usage_events` table (lag = `stream_max_id − consumer_checkpoint`).
- Each event row includes `event_key` (unique in DB) and `payload`.

**Auth:** `Authorization: Bearer <WHATSAPP_USAGE_EVENTS_SECRET>`  
Fallback to `WHATSAPP_CAPABILITIES_UPSERT_SECRET` if the dedicated secret is not set.

### Consumer contract (monolith / billing)

Treat two different keys differently:

| Key | Role |
|-----|------|
| **`id`** | **Replay cursor and ordering key only.** Poll with `since_id=<last_event_id>`; never use `id` as the billing dedupe key. |
| **`event_key`** | **Accounting idempotency** across retries and replays. Dedupe store is keyed by `event_key` only. |
| **`stream_max_id`** (API) | **Lag in one poll:** `stream_max_id − last_event_id` (checkpoint from consumer DB). |
| **`last_event_id`** (consumer) | **The only progress state** the worker must persist (plus dedupe/quarantine tables as below). |

Do **not** use `id` as the billing dedupe key (retries and replays re-deliver the same logical send with the same `event_key`).

#### Ordering scope (explicit choice for the monolith worker)

1. **Default (most billing consumers):** one **global** `last_event_id` plus **`event_key` dedupe**. Enough when you only need **exactly-once accounting per logical send**, not strict ordering of unrelated events across accounts.

2. **Stricter:** **per-`account_id` checkpoint** (and usually per-account processing) only if **account-local event order** changes billing outcomes. More operational and implementation cost; use only when required.

#### Checkpoint advancement (strict)

Advance `last_event_id` past an event row’s `id` **only** when that row is fully resolved:

- **After successful** apply to quotas/billing (and dedupe insert succeeded if you use that pattern).
- **After dedupe skip** — `event_key` already in `whatsapp_usage_events_processed` (or equivalent): no double charge; still move the cursor past that row’s `id`.
- **After quarantine** — malformed payload parked in quarantine; still move the cursor past that `id` so the tail does not stall (with metrics/alerts).

**Do not** advance `last_event_id` on **HTTP or transport failure** (timeouts, 5xx, connection errors). Retry with backoff and re-fetch from the same `since_id`.

**Malformed payloads:** write to quarantine (`whatsapp/migrations/006_whatsapp_usage_consumer_state_expand.sql`), log/metric, then advance past that `id` per the rule above.

**Consumer-owned tables (same shared DB):** migration `006_whatsapp_usage_consumer_state_expand.sql` — checkpoints, processed keys, quarantine.

#### Rollout sequence (reference)

Apply DB migrations **001 → 006**. For newer WhatsApp service revisions that load full `whatsapp_accounts` rows (safe mode, webhooks, trust), also apply **007 → 011** per `DEPLOYMENT_READINESS.md` §1. Deploy WhatsApp with **`WHATSAPP_CAPABILITIES_STRICT=0`**. Deploy monolith **capability writer**, then **usage consumer**. Enable **`WHATSAPP_CAPABILITIES_STRICT=1`** only when missing projection row rate and consumer lag are both stable.

## Reader behavior (WhatsApp service)

Implemented in `whatsapp/capabilities.py`.

- **No row:** permissive (gates off) so existing deployments keep working until the monolith backfills. Set **`WHATSAPP_CAPABILITIES_STRICT=1`** to require a row for gated paths (AI, automation, broadcast) and strict outbound policy where implemented.
- **Outbound sends:** checked in `WhatsAppService._send_api_request` and in `_enforce_message_limit` on HTTP send routes (`whatsapp/routes.py`) — same `outbound_send_capability_check` rules.
- **Automation:** `send_automation_response` checks `automation_enabled`; `response_type == "ai"` also checks `ai_enabled`.
- **AI merge:** `prepare_automation_ai` checks entitlement `ai_enabled` before bot-settings merge.
- **Broadcast / bulk:** `bulk_routes` create campaign + `trigger_campaign_now` check `broadcast_enabled`.

## HTTP routes pre-check (`whatsapp/routes.py`)

`_enforce_message_limit(phone_number_id)` runs before several `/api/whatsapp/send/*` handlers. It now calls **`outbound_send_capability_check`** only (same snapshot + UTC-day outbound count as `_send_api_request`). It does **not** load `User`, `Workspace`, or plan features, and does **not** call `record_message_sent` (usage accounting belongs to the monolith / events).

Denied responses include `subscription_status`, `daily_message_limit`, `daily_messages_used` where applicable; daily-limit denials also set `error: "message_limit_exceeded"` with `limit` / `current` aliases for older clients.

## Drift checks (ops)

Compare monolith’s last emitted `projection_version` / `updated_at` with the DB row for the same account. Alert if the monolith thinks it wrote but the row is stale (worker lag, failed transaction, wrong DB).

## What not to do

- Do not add Stripe/Razorpay/plan SQL to the WhatsApp service for gating.
- Do not “interpret” billing in WhatsApp — only enforce the snapshot.

**One line:** monolith decides; WhatsApp enforces.
